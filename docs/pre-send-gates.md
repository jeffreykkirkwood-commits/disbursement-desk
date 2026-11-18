# Pre-send gates (P0)

Mandatory checks before a Disbursement Desk payout may be marked Sent
(`status=PAID` / `execution_status=SETTLED`) without an explicit operator decision
that is allowed for that flag. Skipping any of these on the create/route/decide/settle
path is a **release blocker**.

Code source of truth: `payouts.MANDATORY_PRE_SEND_RULES`, `payouts.explain_flag`,
`payout_rules.route_one`, `payouts.decide`, `payouts._settle`. Product tests:
`test_pre_send_gates.py`. OFAC formal check: `.cursor/skills/verify-ofac-settle/`.

## Mandatory rules

| Code | When it fires | Auto-route | Operator send |
|------|---------------|------------|---------------|
| `AMOUNT_OVER_3X_AVG` | Amount &gt; 3× payee usual (`payee_usual_cents`) | Stops at `FLAGGED` | Approve / Hold / Return |
| `BANK_CHANGED_7D` | Bank details changed within 7 days of create | Stops at `FLAGGED` | Approve / Hold / Return |
| `DUPLICATE_24H` | Same payee + amount within 24 hours | Stops at `FLAGGED` | Approve / Hold / Return |
| `NEW_PAYEE_OVER_1000` | No prior SETTLED payout for payee; amount &gt; $1,000 | Stops at `FLAGGED` | Approve / Hold / Return |
| `POSSIBLE_SANCTIONS_MATCH` | `ofac.screen(payee_name)` matches (local snapshot or HTTP vendor; vendor outage fails closed) | Stops at `FLAGGED` | **Clear** or **Block** only (Approve refused) |

All five are evaluated together in `explain_flag()`; multiple codes may be stored on one payout.

## Enforcement points

1. **Create / automation** — `payout_rules.create` → `route_one` always recomputes flags via `explain_flag`. Any `flag_code` → `FLAGGED` only (never Approve/Hold/Return/PAID). Clean path may settle only after that recompute.
2. **Decide** — Only `FLAGGED` / `HELD`. Non-sanctions Approve may settle after review. Sanctions: Approve → 409; Clear → settle with `allow_sanctions_clear=True`; Block → `RETURNED`.
3. **Settle re-screen** — `_settle` calls `ofac.screen` again unless Clear allowed the send; a match holds at `FLAGGED` and does not write PAID/SETTLED.
4. **Bypass** — Calling settle while skipping `explain_flag`, or settling a sanctions match without Clear, must fail tests / formal `Inv_NoSettleWithoutClear`.

## Not in this gate set

Rail cutoff/limit enforcement: [rail-enforcement.md](rail-enforcement.md). Live OFAC vendor path: [ofac-vendor.md](ofac-vendor.md). Authenticated operator identity is P1 — see [operator-authz.md](operator-authz.md). Concurrent decide exclusivity (`BEGIN IMMEDIATE` + UNIQUE keys, stress + formal `Inv_AtMostOneSettle`): [decide-exclusivity.md](decide-exclusivity.md). Stripe Connect money movement is P3: entry criteria are met and test-mode send/recon code is on main (#41–#43), but live pilot items (secrets, test clocks, compliance) stay open — see [stripe-connect-go-live.md](stripe-connect-go-live.md).
