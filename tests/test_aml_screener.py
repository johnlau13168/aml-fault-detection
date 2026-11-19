"""Boundary, grouping and fault-routing tests for the AML screener.

Covers BRS-AML-104 / FS-AML-104 acceptance criteria for FS-01 (KAN-2),
FS-02 (KAN-3) and FS-03 (KAN-4).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from aml_screener import (
    ACTIVE_HIGH_RISK_COUNTRIES,
    DLQ_TOPIC_SWIFT_MT103_ERR,
    FS01_MIN_AMOUNT_HKD,
    HKT,
    AMLScreener,
    FXRateService,
    TransientDBFault,
    WireTxn,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_txn(**overrides) -> WireTxn:
    """Build a settled outward MT103 wire with sensible defaults."""
    defaults = dict(
        txn_id="TXN-0001",
        swift_uetr="UETR-0001",
        swift_msg_type="MT103",
        direction="outward",
        status="settled",
        amt=100000.00,
        ccy="HKD",
        dest_ctry_cd="IR",
        acct_id="ACC-1001",
        bene_iban="HK88AAAA1234567890",
        value_ts=datetime(2026, 11, 20, 10, 0, tzinfo=HKT),
    )
    defaults.update(overrides)
    return WireTxn(**defaults)


def hkt(y, m, d, hh, mm) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=HKT)


# ---------------------------------------------------------------------------
# FS-01 (KAN-2) -- exact boundary
# ---------------------------------------------------------------------------


class TestFS01Boundary:
    @pytest.mark.parametrize(
        "amount,should_alert",
        [
            (80000.00, True),  # exact threshold -> MUST alert
            (79999.99, False),  # one cent below -> MUST NOT alert
            (79999.98, False),
            (125000.00, True),
            (0.00, False),
        ],
    )
    def test_strict_boundary(self, amount, should_alert):
        screener = AMLScreener()
        alerts = screener.screen(make_txn(txn_id=f"TXN-{amount}", amt=amount))
        assert bool(alerts) is should_alert
        if should_alert:
            assert alerts[0].rule_id == "FS-01"
            assert alerts[0].kan_ticket == "KAN-2"
            assert alerts[0].amt_hkd >= FS01_MIN_AMOUNT_HKD

    @pytest.mark.parametrize("ccy_code", sorted(ACTIVE_HIGH_RISK_COUNTRIES))
    def test_active_high_risk_countries_alert(self, ccy_code):
        screener = AMLScreener()
        alerts = screener.screen(make_txn(dest_ctry_cd=ccy_code, amt=80000.00))
        assert len(alerts) == 1
        assert alerts[0].rule_id == "FS-01"

    @pytest.mark.parametrize("ccy_code", ["US", "GB", "SG", "JP"])
    def test_non_high_risk_country_does_not_alert(self, ccy_code):
        screener = AMLScreener()
        assert screener.screen(make_txn(dest_ctry_cd=ccy_code, amt=500000.00)) == []

    def test_case_insensitive_country_and_amount_in_foreign_ccy(self):
        # 10,000 USD @ 8.00 T-day rate = 80,000.00 HKD -> alert
        fx = FXRateService({("USD", date(2026, 11, 20)): 8.00})
        screener = AMLScreener(fx=fx)
        alerts = screener.screen(make_txn(ccy="USD", amt=10000.0, dest_ctry_cd="ir"))
        assert len(alerts) == 1
        assert alerts[0].amt_hkd == 80000.00

    def test_t_minus_1_fx_fallback(self):
        # T-day (2026-11-20) rate missing; only T-1 (2026-11-19) rate present.
        fx = FXRateService({("USD", date(2026, 11, 19)): 8.00})
        screener = AMLScreener(fx=fx)
        txn = make_txn(
            ccy="USD",
            amt=10000.0,
            value_ts=datetime(2026, 11, 20, 15, 30, tzinfo=HKT),
        )
        alerts = screener.screen(txn)
        assert len(alerts) == 1
        assert alerts[0].amt_hkd == 80000.00  # via T-1 rate

    def test_t_minus_1_fallback_does_not_cross_threshold_when_rate_lower(self):
        # T-1 rate makes the wire land below threshold -> no alert.
        fx = FXRateService({("USD", date(2026, 11, 19)): 7.00})
        screener = AMLScreener(fx=fx)
        txn = make_txn(ccy="USD", amt=11000.0)  # 11,000 * 7.00 = 77,000 HKD
        assert screener.screen(txn) == []

    def test_uetr_idempotency(self):
        screener = AMLScreener()
        first = screener.screen(make_txn(swift_uetr="UETR-DUP", amt=90000.00))
        second = screener.screen(make_txn(swift_uetr="UETR-DUP", amt=90000.00))
        assert len(first) == 1
        assert second == []
        assert len(screener.alerts) == 1

    def test_only_settled_outward_mt103_alerts(self):
        assert AMLScreener().screen(make_txn(status="pending")) == []
        assert AMLScreener().screen(make_txn(direction="inward")) == []
        assert AMLScreener().screen(make_txn(swift_msg_type="MT202")) == []


# ---------------------------------------------------------------------------
# FS-02 (KAN-3) -- blank / NULL destination country below HKD 80,000
# ---------------------------------------------------------------------------


class TestFS02DestinationCountry:
    @pytest.mark.parametrize("bad_code", [None, "", "   ", "1", "HKG", "I!", "H.K."])
    def test_blank_or_invalid_country_below_threshold_alerts(self, bad_code):
        """FS-02 fires regardless of amount -- 1,000.00 HKD still alerts."""
        screener = AMLScreener()
        alerts = screener.screen(
            make_txn(dest_ctry_cd=bad_code, amt=1000.00, txn_id=f"T-{bad_code}")
        )
        assert len(alerts) == 1
        assert alerts[0].rule_id == "FS-02"
        assert alerts[0].kan_ticket == "KAN-3"
        assert alerts[0].amt_hkd < FS01_MIN_AMOUNT_HKD

    def test_null_country_below_hkd_80k_exact(self):
        screener = AMLScreener()
        alerts = screener.screen(make_txn(dest_ctry_cd=None, amt=79999.99))
        assert len(alerts) == 1
        assert alerts[0].rule_id == "FS-02"
        assert alerts[0].amt_hkd == 79999.99

    @pytest.mark.parametrize("code", ["IR", "KP", "MM", "US", "HK", "hk", " gb "])
    def test_valid_iso2_country_suppresses_fs02(self, code):
        screener = AMLScreener()
        alerts = screener.screen(make_txn(dest_ctry_cd=code, amt=1000.00))
        assert alerts == []

    def test_null_country_above_threshold_does_not_double_alert(self):
        """NULL is not in the active high-risk list, so FS-01 does not fire;
        exactly one FS-02 alert is produced for the UETR (no double counting)."""
        screener = AMLScreener()
        alerts = screener.screen(make_txn(dest_ctry_cd=None, amt=90000.00))
        assert [a.rule_id for a in alerts] == ["FS-02"]


# ---------------------------------------------------------------------------
# FS-03 (KAN-4) -- midnight HKT crossover grouping
# ---------------------------------------------------------------------------


class TestFS03Grouping:
    def test_midnight_hkt_crossover_same_parent_case(self):
        """23:50 HKT (day 1) + 00:10 HKT (day 2) -> one Parent Case ID."""
        screener = AMLScreener()
        a1 = screener.screen(
            make_txn(
                txn_id="TXN-LATE",
                swift_uetr="UETR-LATE",
                acct_id="ACC-77",
                bene_iban="HK88GRP0001",
                value_ts=hkt(2026, 11, 20, 23, 50),
            )
        )[0]
        a2 = screener.screen(
            make_txn(
                txn_id="TXN-EARLY",
                swift_uetr="UETR-EARLY",
                acct_id="ACC-77",
                bene_iban="HK88GRP0001",
                value_ts=hkt(2026, 11, 21, 0, 10),
            )
        )[0]

        assert a1.parent_case_id is not None
        assert a2.parent_case_id == a1.parent_case_id
        assert a2.grouped is True
        assert a1.grouped is False

        case = screener.registry.get("ACC-77", "HK88GRP0001")
        assert case is not None
        assert case.alert_ids == [a1.alert_id, a2.alert_id]

    def test_grouping_is_utc_naive_safe(self):
        """Naive timestamps are interpreted as HKT, not UTC."""
        screener = AMLScreener()
        a1 = screener.screen(
            make_txn(
                swift_uetr="U1", acct_id="A", bene_iban="B", value_ts=datetime(2026, 11, 20, 23, 50)
            )  # naive
        )[0]
        a2 = screener.screen(
            make_txn(
                swift_uetr="U2", acct_id="A", bene_iban="B", value_ts=datetime(2026, 11, 21, 0, 10)
            )  # naive
        )[0]
        assert a1.parent_case_id == a2.parent_case_id

    def test_different_account_or_beneficiary_gets_new_parent_case(self):
        screener = AMLScreener()
        a1 = screener.screen(
            make_txn(
                swift_uetr="U-A",
                acct_id="ACC-A",
                bene_iban="IBAN-A",
                value_ts=hkt(2026, 11, 20, 23, 50),
            )
        )[0]
        a2 = screener.screen(
            make_txn(
                swift_uetr="U-B",
                acct_id="ACC-B",
                bene_iban="IBAN-A",
                value_ts=hkt(2026, 11, 21, 0, 10),
            )
        )[0]
        assert a1.parent_case_id != a2.parent_case_id

    def test_outside_rolling_24h_window_opens_new_parent_case(self):
        screener = AMLScreener()
        a1 = screener.screen(
            make_txn(
                swift_uetr="U-W1",
                acct_id="ACC-W",
                bene_iban="IBAN-W",
                value_ts=hkt(2026, 11, 20, 23, 50),
            )
        )[0]
        a2 = screener.screen(
            make_txn(
                swift_uetr="U-W2",
                acct_id="ACC-W",
                bene_iban="IBAN-W",
                value_ts=hkt(2026, 11, 21, 23, 51),
            )  # 24h01m later
        )[0]
        assert a1.parent_case_id != a2.parent_case_id

    def test_three_crossover_alerts_one_case(self):
        screener = AMLScreener()
        ts = [hkt(2026, 11, 20, 23, 50), hkt(2026, 11, 21, 0, 10), hkt(2026, 11, 21, 0, 40)]
        alerts = [
            screener.screen(
                make_txn(swift_uetr=f"U-C{i}", acct_id="ACC-C", bene_iban="IBAN-C", value_ts=t)
            )[0]
            for i, t in enumerate(ts)
        ]
        assert len({a.parent_case_id for a in alerts}) == 1
        assert sum(1 for a in alerts if a.grouped) == 2


# ---------------------------------------------------------------------------
# FS-03 (KAN-4) -- DB lock/timeout fault -> retries -> DLQ
# ---------------------------------------------------------------------------


class TestFS03FaultRouting:
    def _flaky_persist(self, failures: int, counter: dict):
        def persist(payload: dict) -> None:
            counter["calls"] += 1
            if counter["calls"] <= failures:
                raise TransientDBFault("PI001 lock wait timeout exceeded")

        return persist

    def test_transient_fault_retried_then_recovered(self):
        counter = {"calls": 0}
        screener = AMLScreener(persist=self._flaky_persist(2, counter))
        alerts = screener.screen(make_txn(swift_uetr="U-FLAKY"))
        assert len(alerts) == 1
        assert counter["calls"] == 3  # 2 failures + 1 success
        assert screener.dlq.messages == []

    def test_three_failures_route_to_dlq_topic(self):
        counter = {"calls": 0}
        screener = AMLScreener(persist=self._flaky_persist(99, counter))
        alerts = screener.screen(make_txn(swift_uetr="U-DLQ"))
        assert len(alerts) == 1

        dlq_msgs = screener.dlq.for_topic(DLQ_TOPIC_SWIFT_MT103_ERR)
        assert DLQ_TOPIC_SWIFT_MT103_ERR == "dlq.swift.mt103.err"
        assert len(dlq_msgs) == 1
        assert dlq_msgs[0].attempts == 3
        assert counter["calls"] == 3  # exactly 3 retries, no more
        assert "TransientDBFault" in dlq_msgs[0].error
        assert dlq_msgs[0].payload["txn_id"] == "TXN-0001"
        assert dlq_msgs[0].payload["rule_id"] == "FS-01"

    def test_dlq_payload_retains_parent_case_id(self):
        counter = {"calls": 0}
        screener = AMLScreener(persist=self._flaky_persist(99, counter))
        screener.screen(
            make_txn(
                swift_uetr="U-P1",
                acct_id="ACC-P",
                bene_iban="IBAN-P",
                value_ts=hkt(2026, 11, 20, 23, 50),
            )
        )
        screener.screen(
            make_txn(
                swift_uetr="U-P2",
                acct_id="ACC-P",
                bene_iban="IBAN-P",
                value_ts=hkt(2026, 11, 21, 0, 10),
            )
        )
        msgs = screener.dlq.for_topic(DLQ_TOPIC_SWIFT_MT103_ERR)
        assert len(msgs) == 2
        assert msgs[0].payload["parent_case_id"] == msgs[1].payload["parent_case_id"]

    def test_dlq_message_does_not_block_screening(self):
        counter = {"calls": 0}
        screener = AMLScreener(persist=self._flaky_persist(99, counter))
        screener.screen(make_txn(swift_uetr="U-X"))
        alerts = screener.screen(make_txn(swift_uetr="U-Y", txn_id="TXN-0002"))
        assert len(alerts) == 1
        assert len(screener.alerts) == 2


def test_hkt_offset_is_utc_plus_8():
    assert HKT.utcoffset(datetime(2026, 11, 20)) == timedelta(hours=8)
