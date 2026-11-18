# Stripe Connect go-live (P3) — gated checklist

**Maturity:** test-mode Connect send/recon code is on main behind the kill
switch. Until Render Stripe secrets, Connect test clocks, a limited live
pilot, and compliance sign-off are done, this is **not** full production
money software. Express / KYC owner sign-off is already checked
([stripe-connect-account-ownership.md](stripe-connect-account-ownership.md)).

Test-mode Stripe send runs only when `STRIPE_PAYOUTS_ENABLED` is exactly `1`,
and only for the demo folder named by `STRIPE_DEMO_ID`. Any other flag value
keeps the demo settle, even when Stripe secrets are present. Armed send
creates a destination charge unless `STRIPE_SEND_PATH` is `transfer`
([#41](https://github.com/jeffreykkirkwood-commits/disbursement-desk/pull/41)).
Live mode stays closed until the unchecked items below are signed off.

Shipped on main and reflected below: destination charges default (#41),
idempotency ↔ Stripe object id mapping
([#42](https://github.com/jeffreykkirkwood-commits/disbursement-desk/pull/42)),
and the Stripe reconciliation job with dry-run default
([#43](https://github.com/jeffreykkirkwood-commits/disbursement-desk/pull/43)).

Adjacent operator control-plane (not Connect pilot blockers): gate-stop
narrative
([#45](https://github.com/jeffreykkirkwood-commits/disbursement-desk/pull/45)),
multi-flag rules notes
([#47](https://github.com/jeffreykkirkwood-commits/disbursement-desk/pull/47)),
Desk Why held panel
([#48](https://github.com/jeffreykkirkwood-commits/disbursement-desk/pull/48)),
card-format / issue-link commit-status overwrite
([#49](https://github.com/jeffreykkirkwood-commits/disbursement-desk/pull/49)),
optional AI paraphrase of stored gate notes behind `DESK_AI_NOTES`
([#50](https://github.com/jeffreykkirkwood-commits/disbursement-desk/pull/50),
`7089654`).

Related gates:
[pre-send-gates.md](pre-send-gates.md), [ofac-vendor.md](ofac-vendor.md),
[decide-exclusivity.md](decide-exclusivity.md), [desk-audit.md](desk-audit.md).
The account-type recommendation is in
[stripe-connect-account-ownership.md](stripe-connect-account-ownership.md).
The owner sign-off there is checked (2026-11-17).
Kill-switch steps are in
[stripe-connect-kill-switch.md](stripe-connect-kill-switch.md).

## Entry criteria (must all be true before Connect send-path)

| # | Criterion | Evidence in this repo (as of #26) | Status |
|---|-----------|-----------------------------------|--------|
| 1 | **OFAC gate** — no settle of a possible match without Clear | `verify-ofac-settle` (`Inv_NoSettleWithoutClear`); product tests; optional HTTP vendor behind Clear/Block (#23) | Met |
| 2 | **Fraud / exception screening** — mandatory rules cannot be skipped on create/route/decide/settle | `docs/pre-send-gates.md`, `MANDATORY_PRE_SEND_RULES`, `test_pre_send_gates.py` | Met |
| 3 | **Create + decide idempotency** — no double disbursement / double settle | Create `Idempotency-Key` (#16); decide `BEGIN IMMEDIATE` + UNIQUE keys; stress + formal `Inv_AtMostOneSettle` (#25) | Met |
| 4 | **Audit trail** — who/what/when for Desk transitions; queryable per payout | `payout_events` / `record_transition`; Desk **Activity** panel (#26) | Met |

Re-verify these on any change to `decide`, `_settle`, `route_one`, or OFAC screening before starting Connect money movement.

## Go-live checklist (open only after entry criteria)

Checked items are enforced by the test-mode send path or shipped jobs. Unchecked
items still block a live pilot:

- [x] Connected account type and KYC ownership signed off. The recommendation is Express. See [Account ownership](stripe-connect-account-ownership.md). Jeff signed 2026-11-17.
- [x] Platform → connected-account fund flow: test-mode destination charge (`PaymentIntent` with `transfer_data[destination]`). `STRIPE_SEND_PATH=transfer` keeps Transfers (#41).
- [x] Desk Stripe-call idempotency keys map 1:1 to Stripe `Transfer` / `PaymentIntent` / `Payout` ids in `payout_events` evidence (#42; see [Idempotency to Stripe id mapping](#idempotency-to-stripe-id-mapping))
- [x] OFAC **Clear** is a hard precondition in code **before** any Stripe money-movement API call
- [x] Failure / retry: Stripe webhooks update `execution_status` without inventing Desk `status`
- [x] Reconciliation treats Stripe as source of truth for SETTLED / FAILED (#43; see [Stripe reconciliation job](#stripe-reconciliation-job))
- [x] Secrets only via env / secret manager; no keys in repo (Render secret values are still unconfigured — see blockers below)
- [x] Kill switch. New Stripe sends run only when `STRIPE_PAYOUTS_ENABLED` is exactly `1`. Steps are in [Disarm Stripe payouts](stripe-connect-kill-switch.md).
- [ ] Test-mode end-to-end on Connect test clocks, then a limited live pilot that can be disarmed with the kill switch
- [ ] Compliance review (sanctions, money transmission, ToS) signed off outside this repo

## Idempotency to Stripe id mapping

Each money-movement call uses a Stripe `Idempotency-Key`:

- Destination charge (default): `desk-charge:{demo_id}:{payout_id}` (`charge_idempotency_key`)
- Legacy Transfer (`STRIPE_SEND_PATH=transfer`): `desk-transfer:{demo_id}:{payout_id}` (`transfer_idempotency_key`)

On submit, `decide` and `route` write both sides into `payout_events.evidence`:

| Field | Meaning |
|-------|---------|
| `stripe_idempotency_key` | Key sent to Stripe |
| `stripe_object_id` | Returned Stripe object id (`pi_…`, `tr_…`, or `po_…`) |
| `stripe_object_type` | `payment_intent`, `transfer`, or `payout` |
| `stripe_payment_intent_id` | Alias when the object is a PaymentIntent |
| `stripe_transfer_id` | Alias when the object is a Transfer |

Look up either direction without live Stripe:

- Key → object: `payouts.stripe_mapping_by_idempotency_key(database, key)`
- Object → key: `payouts.stripe_mapping_by_object_id(database, object_id)`

`execution_ref` on the payout row also stores the PaymentIntent or Transfer id after submit.

## Stripe reconciliation job

`python3 -m stripe_reconciliation DATABASE` polls Stripe for in-scope
`execution_ref` values (`pi_…`, `tr_…`, `po_…`) and compares Desk
`execution_status` to Stripe as source of truth for SETTLED / FAILED.

- **Dry-run is the default.** It reports `matched` / `would_update` /
  `mismatch` / `missing_on_stripe` / `skipped` / `error` and does not write.
- **Apply** (`--apply`) reuses `payouts.apply_stripe_event` with stable
  `recon:stripe:{object_id}:{outcome}` event ids. Illegal moves stay mismatches.
- The CSV fixture job in `reconciliation.py` is a separate path and is not used here.
- Unit coverage is in `test_stripe_reconciliation.py` (mocked Stripe; no live secrets).

### Operator surface (Desk)

Admins see a **Stripe recon** strip on the Desk (below Reset demo): last
mismatch summary, **Run dry-run**, and **Apply…** (confirm required). Reviewers
do not see it.

| Path | Who | Behavior |
|------|-----|----------|
| `GET /stripe/reconciliation` | admin | Last summary JSON (`summary` may be `null`) |
| `POST /stripe/reconciliation` | admin | Dry-run unless body includes `"apply": true` |

Dry-run needs no Stripe secrets on a seed demo (no Stripe refs → empty report).
Apply still calls Stripe retrieve; without secrets, in-scope rows report `error`.
CLI `--apply` remains available for operators who prefer the module entrypoint.

## Still blocked (outside this checklist's checked boxes)

- Render Stripe secrets (`rk_test_` / `whsec_` / destination map / `STRIPE_DEMO_ID`)
- Connect test-clocks end-to-end
- Limited live pilot (disarmable with the kill switch)
- Compliance / ToS / money-transmission sign-off outside this repo

## Still out of scope

- Connected-account onboarding UI
- Live `sk_live_` and `rk_live_` keys (the send path refuses them)

## When entry criteria regress

If a PR weakens OFAC, pre-send rules, exclusivity, or audit coverage, treat Connect as **blocked** again even if some go-live boxes were checked.
