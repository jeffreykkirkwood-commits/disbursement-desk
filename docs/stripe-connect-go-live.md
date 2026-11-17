# Stripe Connect go-live (P3) — gated checklist

**Frame: checklist and design only.** Do **not** merge send-path Connect code
(Transfers / Payouts / PaymentIntents that move money) until every **entry
criterion** below is true and the go-live items are signed off.

This Desk remains fictional money until that gate opens. Related gates:
[pre-send-gates.md](pre-send-gates.md), [ofac-vendor.md](ofac-vendor.md),
[decide-exclusivity.md](decide-exclusivity.md), [desk-audit.md](desk-audit.md).

## Entry criteria (must all be true before Connect send-path)

| # | Criterion | Evidence in this repo (as of #26) | Status |
|---|-----------|-----------------------------------|--------|
| 1 | **OFAC gate** — no settle of a possible match without Clear | `verify-ofac-settle` (`Inv_NoSettleWithoutClear`); product tests; optional HTTP vendor behind Clear/Block (#23) | Met |
| 2 | **Fraud / exception screening** — mandatory rules cannot be skipped on create/route/decide/settle | `docs/pre-send-gates.md`, `MANDATORY_PRE_SEND_RULES`, `test_pre_send_gates.py` | Met |
| 3 | **Create + decide idempotency** — no double disbursement / double settle | Create `Idempotency-Key` (#16); decide `BEGIN IMMEDIATE` + UNIQUE keys; stress + formal `Inv_AtMostOneSettle` (#25) | Met |
| 4 | **Audit trail** — who/what/when for Desk transitions; queryable per payout | `payout_events` / `record_transition`; Desk **Activity** panel (#26) | Met |

Re-verify these on any change to `decide`, `_settle`, `route_one`, or OFAC screening before starting Connect money movement.

## Go-live checklist (open only after entry criteria)

Product / compliance / platform decisions — still unchecked until a real Connect pilot:

- [ ] Connected account type chosen (Express vs Custom); KYC ownership documented
- [ ] Platform vs connected-account fund flow decided (destination charges vs separate charges + transfers)
- [ ] Desk `Idempotency-Key` maps 1:1 to Stripe `Transfer` / `PaymentIntent` / `Payout` ids in `payout_events` evidence
- [ ] OFAC **Clear** is a hard precondition in code **before** any Stripe money-movement API call
- [ ] Failure / retry: Stripe webhooks update `execution_status` without inventing Desk `status`
- [ ] Reconciliation treats Stripe as source of truth for SETTLED / FAILED
- [ ] Secrets only via env / secret manager; no keys in repo
- [ ] Test-mode end-to-end on Connect test clocks; then limited live pilot with a kill switch
- [ ] Compliance review (sanctions, money transmission, ToS) signed off outside this repo

## Explicit non-goals until the checklist opens

- No Stripe SDK send path in `payouts._settle` / approve / clear
- No live Connect account onboarding UI in this repo
- Spikes and design notes are fine; they must not call money-movement APIs in merged code

## When entry criteria regress

If a PR weakens OFAC, pre-send rules, exclusivity, or audit coverage, treat Connect as **blocked** again even if some go-live boxes were checked.
