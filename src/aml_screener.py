"""AML & Fault Detection engine for SWIFT MT103 outward wires.

Implements the detection logic specified in:

* BRS-AML-104  -- Business Requirements Specification (HKMA / HKSAR aligned)
* FS-AML-104   -- Functional Specification

Detection rules
---------------
FS-01 (KAN-2)  Settled outward MT103 with ``amt_hkd >= 80000.00`` routed to an
               active high-risk jurisdiction (IR, KP, MM).  Strict boundary:
               ``79999.99`` must NOT alert.  FX conversion uses the T-1 rate as
               a fallback when the T-day rate is unavailable.  ``swift_uetr``
               provides idempotency (a UETR is alerted at most once).

FS-02 (KAN-3)  Settled outward wires whose ``dest_ctry_cd`` is NULL, blank or
               not a 2-letter ISO-3166-1 alpha-2 code, regardless of amount.

FS-03 (KAN-4)  Repeat alerts for the same ``(acct_id, bene_iban)`` inside a
               rolling 24-hour ``Asia/Hong_Kong`` window are grouped under one
               Parent Case ID.  Simulated database lock/timeout faults are
               retried 3 times and then routed to the dead-letter queue
               ``dlq.swift.mt103.err``.

All timestamps are handled in the ``Asia/Hong_Kong`` (HKT, UTC+8, no DST)
timezone so that day cutover and rolling-window arithmetic are unambiguous.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants derived from BRS-AML-104 / FS-AML-104
# ---------------------------------------------------------------------------

#: HKT has no daylight saving time; fixed UTC+8.
HKT = timezone(timedelta(hours=8), name="HKT")

#: FS-01 amount threshold (HKD), strict lower bound is inclusive at 80000.00.
FS01_MIN_AMOUNT_HKD = 80000.00

#: Active high-risk country list (FS-01 / KAN-2).
ACTIVE_HIGH_RISK_COUNTRIES: frozenset[str] = frozenset({"IR", "KP", "MM"})

#: Rolling grouping window for FS-03 (KAN-4).
FS03_GROUPING_WINDOW = timedelta(hours=24)

#: Maximum transient DB retries before dead-lettering (FS-03).
FS03_MAX_DB_RETRIES = 3

#: Dead-letter queue for MT103 persistence faults (FS-03 / KAN-4).
DLQ_TOPIC_SWIFT_MT103_ERR = "dlq.swift.mt103.err"

#: ISO-3166-1 alpha-2 pattern used by FS-02.
_ISO2_RE = re.compile(r"^[A-Z]{2}$")


# ---------------------------------------------------------------------------
# Fault simulation
# ---------------------------------------------------------------------------


class TransientDBFault(Exception):
    """Simulated transient database fault (lock wait / statement timeout)."""


class PermanentDBFault(Exception):
    """Simulated non-retryable database fault."""


# ---------------------------------------------------------------------------
# Domain model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WireTxn:
    """A single settled SWIFT payment instruction (MT103 outward)."""

    txn_id: str
    swift_uetr: str
    swift_msg_type: str = "MT103"
    direction: str = "outward"
    status: str = "settled"
    amt: float = 0.0
    ccy: str = "HKD"
    #: May be ``None`` (SQL NULL), ``""`` / whitespace (blank) or a bogus code.
    dest_ctry_cd: str | None = None
    acct_id: str = ""
    bene_iban: str = ""
    #: Transaction value timestamp; naive input is assumed to be HKT.
    value_ts: datetime = field(default_factory=lambda: datetime.now(HKT))

    @property
    def value_ts_hkt(self) -> datetime:
        """Value timestamp normalised to HKT."""
        ts = self.value_ts
        if ts.tzinfo is None:
            return ts.replace(tzinfo=HKT)
        return ts.astimezone(HKT)


# ---------------------------------------------------------------------------
# FX service with T-1 fallback
# ---------------------------------------------------------------------------


class FXRateService:
    """T-day FX lookup with T-1 fallback (FS-01 / KAN-2).

    Rates are expressed as ``HKD per 1 unit of foreign currency``, e.g. a
    ``USD`` rate of ``7.8`` means 1 USD == 7.8 HKD.
    """

    def __init__(self, rates: dict[tuple[str, datetime.date], float]) -> None:
        self._rates = dict(rates)

    def rate_hkd(self, ccy: str, on_date) -> float | None:
        """Return the HKD rate for ``ccy`` on ``on_date``.

        Falls back to the T-1 (previous calendar day) rate when the T-day rate
        is unavailable.  Returns ``None`` when neither exists.
        """
        if ccy.upper() == "HKD":
            return 1.0
        day = on_date
        t0 = self._rates.get((ccy.upper(), day))
        if t0 is not None:
            return t0
        t1 = self._rates.get((ccy.upper(), day - timedelta(days=1)))
        if t1 is not None:
            logger.debug(
                "FX T-day rate missing for %s on %s; using T-1 fallback %s",
                ccy,
                day,
                t1,
            )
            return t1
        return None

    def to_hkd(self, amt: float, ccy: str, on_date) -> float | None:
        rate = self.rate_hkd(ccy, on_date)
        if rate is None:
            return None
        return round(float(amt) * float(rate), 2)


# ---------------------------------------------------------------------------
# Dead-letter queue
# ---------------------------------------------------------------------------


@dataclass
class DLQMessage:
    topic: str
    payload: dict
    error: str
    attempts: int


class DeadLetterQueue:
    """In-memory dead-letter queue (mirrors the Kafka topic in production)."""

    def __init__(self) -> None:
        self.messages: list[DLQMessage] = []

    def publish(self, topic: str, payload: dict, error: str, attempts: int) -> None:
        msg = DLQMessage(topic=topic, payload=dict(payload), error=error, attempts=attempts)
        self.messages.append(msg)
        logger.error(
            "DLQ publish topic=%s attempts=%s error=%s payload=%s",
            topic,
            attempts,
            error,
            payload,
        )

    def for_topic(self, topic: str) -> list[DLQMessage]:
        return [m for m in self.messages if m.topic == topic]


# ---------------------------------------------------------------------------
# FS-03 parent case registry (rolling 24h HKT window)
# ---------------------------------------------------------------------------


@dataclass
class ParentCase:
    parent_case_id: str
    acct_id: str
    bene_iban: str
    first_alert_ts: datetime
    last_alert_ts: datetime
    alert_ids: list[str] = field(default_factory=list)

    @property
    def age_hours(self) -> float:
        return (self.last_alert_ts - self.first_alert_ts).total_seconds() / 3600.0


class ParentCaseRegistry:
    """Groups repeat alerts per ``(acct_id, bene_iban)`` in a rolling 24h HKT
    window under one Parent Case ID (FS-03 / KAN-4)."""

    def __init__(self, window: timedelta = FS03_GROUPING_WINDOW) -> None:
        self._window = window
        self._cases: dict[tuple[str, str], ParentCase] = {}
        self._seq = 0

    def _next_case_id(self) -> str:
        self._seq += 1
        return f"PC-HKT-{self._seq:06d}"

    def assign(
        self, alert_id: str, acct_id: str, bene_iban: str, alert_ts: datetime
    ) -> tuple[ParentCase, bool]:
        """Return ``(parent_case, created)`` for a new alert.

        ``created`` is True when a brand-new parent case was opened (i.e. no
        prior alert for the same key inside the rolling window).
        """
        if alert_ts.tzinfo is None:
            alert_ts = alert_ts.replace(tzinfo=HKT)
        else:
            alert_ts = alert_ts.astimezone(HKT)

        key = (acct_id, bene_iban)
        existing = self._cases.get(key)
        if existing is None or (alert_ts - existing.last_alert_ts > self._window):
            case = ParentCase(
                parent_case_id=self._next_case_id(),
                acct_id=acct_id,
                bene_iban=bene_iban,
                first_alert_ts=alert_ts,
                last_alert_ts=alert_ts,
                alert_ids=[alert_id],
            )
            self._cases[key] = case
            return case, True

        existing.last_alert_ts = max(existing.last_alert_ts, alert_ts)
        existing.first_alert_ts = min(existing.first_alert_ts, alert_ts)
        existing.alert_ids.append(alert_id)
        return existing, False

    def get(self, acct_id: str, bene_iban: str) -> ParentCase | None:
        return self._cases.get((acct_id, bene_iban))


# ---------------------------------------------------------------------------
# Alert model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Alert:
    alert_id: str
    rule_id: str
    kan_ticket: str
    txn_id: str
    swift_uetr: str
    acct_id: str
    bene_iban: str
    dest_ctry_cd: str | None
    amt_hkd: float
    raw_amt: float
    ccy: str
    alert_ts: datetime
    parent_case_id: str | None = None
    grouped: bool = False

    def as_dict(self) -> dict:
        return {
            "alert_id": self.alert_id,
            "rule_id": self.rule_id,
            "kan_ticket": self.kan_ticket,
            "txn_id": self.txn_id,
            "swift_uetr": self.swift_uetr,
            "acct_id": self.acct_id,
            "bene_iban": self.bene_iban,
            "dest_ctry_cd": self.dest_ctry_cd,
            "amt_hkd": self.amt_hkd,
            "raw_amt": self.raw_amt,
            "ccy": self.ccy,
            "alert_ts": self.alert_ts.isoformat(),
            "parent_case_id": self.parent_case_id,
            "grouped": self.grouped,
        }


# ---------------------------------------------------------------------------
# Screener
# ---------------------------------------------------------------------------


class AMLScreener:
    """Stateful AML screener implementing FS-01, FS-02 and FS-03."""

    def __init__(
        self,
        fx: FXRateService | None = None,
        dlq: DeadLetterQueue | None = None,
        registry: ParentCaseRegistry | None = None,
        persist: Callable[[dict], None] | None = None,
    ) -> None:
        self.fx = fx or FXRateService({})
        self.dlq = dlq or DeadLetterQueue()
        self.registry = registry or ParentCaseRegistry()
        #: Idempotency ledger keyed by ``swift_uetr``.
        self._seen_uetrs: set[str] = set()
        self.alerts: list[Alert] = []
        self._alert_seq = 0
        #: Optional persistence hook; may raise :class:`TransientDBFault`.
        self._persist = persist

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _is_outward_mt103(txn: WireTxn) -> bool:
        return txn.swift_msg_type.upper() == "MT103" and txn.direction.lower() == "outward"

    @staticmethod
    def _is_settled(txn: WireTxn) -> bool:
        return txn.status.lower() == "settled"

    @staticmethod
    def _is_valid_iso2(code: str | None) -> bool:
        """True only for a non-null, non-blank 2-letter ISO country code."""
        if code is None:
            return False
        return bool(_ISO2_RE.match(code.strip().upper()))

    def _already_alerted(self, uetr: str) -> bool:
        """``swift_uetr`` idempotency check (FS-01 / KAN-2)."""
        return uetr in self._seen_uetrs

    def _mark_alerted(self, uetr: str) -> None:
        self._seen_uetrs.add(uetr)

    def _next_alert_id(self) -> str:
        self._alert_seq += 1
        return f"ALRT-{self._alert_seq:06d}"

    def _amount_hkd(self, txn: WireTxn) -> float | None:
        """T-day conversion with T-1 FX fallback; ``None`` if no rate at all."""
        return self.fx.to_hkd(txn.amt, txn.ccy, txn.value_ts_hkt.date())

    # -- FS-01 (KAN-2) -----------------------------------------------------

    def check_fs01(self, txn: WireTxn) -> Alert | None:
        """Flag settled outward MT103 wires >= 80,000.00 HKD to active
        high-risk jurisdictions."""
        if not (self._is_outward_mt103(txn) and self._is_settled(txn)):
            return None

        dest = (txn.dest_ctry_cd or "").strip().upper()
        if dest not in ACTIVE_HIGH_RISK_COUNTRIES:
            return None

        amt_hkd = self._amount_hkd(txn)
        if amt_hkd is None:
            logger.warning("FS-01 skipped txn=%s: no FX rate for %s", txn.txn_id, txn.ccy)
            return None

        # Strict boundary: 79999.99 must NOT alert, 80000.00 MUST alert.
        if amt_hkd < FS01_MIN_AMOUNT_HKD:
            return None

        if self._already_alerted(txn.swift_uetr):
            logger.info("FS-01 idempotent skip uetr=%s", txn.swift_uetr)
            return None

        return self._emit(txn, "FS-01", "KAN-2", amt_hkd)

    # -- FS-02 (KAN-3) -----------------------------------------------------

    def check_fs02(self, txn: WireTxn) -> Alert | None:
        """Flag settled outward wires with NULL/blank/non-ISO destination
        country regardless of amount."""
        if not (self._is_outward_mt103(txn) and self._is_settled(txn)):
            return None

        if self._is_valid_iso2(txn.dest_ctry_cd):
            return None

        if self._already_alerted(txn.swift_uetr):
            logger.info("FS-02 idempotent skip uetr=%s", txn.swift_uetr)
            return None

        amt_hkd = self._amount_hkd(txn) or 0.0
        return self._emit(txn, "FS-02", "KAN-3", amt_hkd)

    # -- FS-03 (KAN-4) -----------------------------------------------------

    def check_fs03_grouping(self, alert: Alert) -> Alert:
        """Attach the Parent Case ID for repeat alerts inside the rolling
        24h HKT window."""
        case, created = self.registry.assign(
            alert_id=alert.alert_id,
            acct_id=alert.acct_id,
            bene_iban=alert.bene_iban,
            alert_ts=alert.alert_ts,
        )
        return Alert(
            **{
                **alert.__dict__,
                "parent_case_id": case.parent_case_id,
                "grouped": not created,
            }
        )

    # -- persistence with retry + DLQ (FS-03 / KAN-4) ----------------------

    def persist_with_retry(self, alert: Alert, max_retries: int = FS03_MAX_DB_RETRIES) -> bool:
        """Persist an alert, retrying transient lock/timeout faults.

        Returns True on success.  After ``max_retries`` failed attempts the
        alert is routed to ``dlq.swift.mt103.err`` and ``False`` is returned.
        """
        if self._persist is None:
            return True

        attempts = 0
        last_error: Exception | None = None
        while attempts < max_retries:
            attempts += 1
            try:
                self._persist(alert.as_dict())
                return True
            except PermanentDBFault as exc:  # non-retryable -> straight to DLQ
                last_error = exc
                break
            except TransientDBFault as exc:
                last_error = exc
                logger.warning(
                    "DB fault on alert=%s attempt=%s/%s: %s",
                    alert.alert_id,
                    attempts,
                    max_retries,
                    exc,
                )

        self.dlq.publish(
            topic=DLQ_TOPIC_SWIFT_MT103_ERR,
            payload=alert.as_dict(),
            error=f"{type(last_error).__name__}: {last_error}",
            attempts=attempts,
        )
        return False

    # -- orchestration -----------------------------------------------------

    def _emit(self, txn: WireTxn, rule_id: str, kan: str, amt_hkd: float) -> Alert:
        alert = Alert(
            alert_id=self._next_alert_id(),
            rule_id=rule_id,
            kan_ticket=kan,
            txn_id=txn.txn_id,
            swift_uetr=txn.swift_uetr,
            acct_id=txn.acct_id,
            bene_iban=txn.bene_iban,
            dest_ctry_cd=txn.dest_ctry_cd,
            amt_hkd=amt_hkd,
            raw_amt=txn.amt,
            ccy=txn.ccy,
            alert_ts=txn.value_ts_hkt,
        )
        alert = self.check_fs03_grouping(alert)
        self._mark_alerted(txn.swift_uetr)
        self.alerts.append(alert)
        self.persist_with_retry(alert)
        return alert

    def screen(self, txn: WireTxn) -> list[Alert]:
        """Run FS-01 then FS-02 over a single transaction."""
        found: list[Alert] = []
        a1 = self.check_fs01(txn)
        if a1 is not None:
            found.append(a1)
        # FS-02 is only meaningful when FS-01 did not consume the UETR
        # (idempotency: one alert per UETR per run).
        a2 = self.check_fs02(txn)
        if a2 is not None:
            found.append(a2)
        return found

    def screen_batch(self, txns: list[WireTxn]) -> list[Alert]:
        out: list[Alert] = []
        for txn in txns:
            out.extend(self.screen(txn))
        return out


def run_rules(txns: list[WireTxn], screener: AMLScreener | None = None) -> list[Alert]:
    """Convenience entry point used by the batch job and the tests."""
    s = screener or AMLScreener()
    return s.screen_batch(txns)
