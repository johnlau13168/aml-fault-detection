<!--
GitHub Flow PR template -- aml-fault-detection
All sections are mandatory. Incomplete PRs will be closed by the risk gate review.
-->

## Summary

<!-- What changed and why (1-3 bullet points). -->

-

## Linked Jira Ticket (required)

- **Jira Ticket ID:** `KAN-X` (e.g. `KAN-2`, `KAN-3`, `KAN-4`)
- **Board:** [KAN](https://johnlau13168.atlassian.net/jira/software/projects/KAN/boards/1)
- **Status:** To Do / In Progress / In Review

## Confluence BRS / FS Verification (required)

- [ ] I have verified this change against **[BRS-AML-104](https://johnlau13168.atlassian.net/wiki/spaces/AFD)** (Business Requirements Specification)
- [ ] I have verified this change against **[FS-AML-104](https://johnlau13168.atlassian.net/wiki/spaces/AFD)** (Functional Specification)
- Affected rule IDs: `FS-01 (KAN-2)` / `FS-02 (KAN-3)` / `FS-03 (KAN-4)` (tick all that apply)
  - [ ] FS-01 -- settled outward MT103, `amt_hkd >= 80000.00` (strict boundary), active high-risk countries `IR`/`KP`/`MM`, T-1 FX fallback, `swift_uetr` idempotency
  - [ ] FS-02 -- NULL / blank / non-ISO `dest_ctry_cd` regardless of amount
  - [ ] FS-03 -- 24h `Asia/Hong_Kong` rolling Parent Case grouping + DLQ `dlq.swift.mt103.err` after 3 retries
- Requirements traceability: <!-- BRS/FS section numbers -->

## HKSAR S17 Security Checklist Sign-off (required)

- [ ] `gitleaks` secret scan passes locally (`pre-commit run gitleaks --all-files`) -- **no hardcoded API keys, DB passwords or SWIFT credentials**
- [ ] No personal data (HKID / account numbers) committed; PDPO-aligned handling confirmed
- [ ] No credentials, tokens or `.env` files added; secrets referenced only via GitHub Environments/Secrets
- [ ] Input validation and least-privilege changes reviewed for the touched components
- [ ] Audit logging / alert trail for this change reviewed (alert IDs, Parent Case IDs, DLQ payload fields)
- [ ] Security reviewer sign-off (if the change touches auth, crypto, SWIFT connectivity or data retention):

  **Sign-off by:** <!-- name / handle -->

## Test Evidence

- [ ] `pytest` passes locally (unit + boundary tests) -- paste the summary line:

  ```
  43 passed
  ```

- [ ] Boundary cases covered: `80000.00` alerts / `79999.99` does not
- [ ] Blank/NULL destination country below HKD 80,000 covered
- [ ] Midnight HKT crossover grouping (`23:50` + `00:10`) covered
- [ ] DLQ fault routing after 3 retries covered

## Risk Gate

- [ ] `ci-risk-gate.yml` is green (secret-scan, ruff-lint, risk-gate-tests, merge-gate)
- [ ] Branch protection: merge to `main` requires the **Merge Risk Gate** check

## Rollback / Risk

<!-- How to roll back, blast radius, feature flag if any. -->
