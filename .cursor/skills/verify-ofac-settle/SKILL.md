---
name: verify-ofac-settle
description: "Formally check disbursement-desk never settles an OFAC-matching payout to PAID/SETTLED unless the operator used Clear (#111). Use when changing payouts.decide, _settle, explain_flag, ofac.screen, or payout_rules.route_one."
---

# Verify OFAC settle gate

## Property (plain words)

**Promise:** A payout whose payee still matches the local OFAC SDN snapshot never reaches `status=PAID` / `execution_status=SETTLED` unless the operator decision was `clear`.

**Harm if broken:** A possible sanctions match is marked Sent without an explicit Clear — compliance failure and a silent send on the Desk (#111).

## Tool and why

**TLA+ / TLC** over a small model of `payouts.decide`, `payouts._settle`, `payouts.explain_flag`, and `payout_rules.route_one`. The question is ordering and branching across Approve / Clear / Block / automation route and the settle re-screen — a model checker fits better than sequential unit tests that only hit one path each. Python has no strong in-tree code verifier for these cross-function gates.

Bounds: `Match = TRUE`, `MaxSteps = 6`, `GateCorrect = TRUE` for the current code.

Read every TLC result with `/statechart` so invariant breaks show as shortest paths in plain words. Do not copy statechart CLI flags from memory — use that skill’s current command.

## Coverage (keep in sync)

| Model name | Code |
|---|---|
| `Match` | `ofac.screen(payee_name)` is not `None` |
| `flaggedSanctions` | `POSSIBLE_SANCTIONS_MATCH` in `flag_code` (`split_flag_codes`) |
| `cleared` | `decide` called `_settle(..., allow_sanctions_clear=True)` via `action == CLEAR` |
| `settled` / `status=PAID` | `_settle` write of `PAID` + `SETTLED`, or `route_one` clean path via `review_and_outcome(PAID)` |
| `RouteOne` | `payout_rules.route_one` + `explain_flag` (OFAC included) |
| `DecideApprove` | `decide` Approve refusal when flagged; else pre-screen / settle |
| `DecideClear` | `decide` Clear → settle with `allow_sanctions_clear=True` |
| `DecideBlock` | `decide` Block → `RETURNED` when flagged |
| `SettleScreen` | `_settle` `ofac.screen` when `not allow_sanctions_clear` |
| `GateCorrect` | `TRUE` = current gates; `FALSE` = approve settles match without Clear / route skips OFAC |

Files: `payouts.py` (`decide`, `_settle`, `_apply_sanctions_hold`, `explain_flag`, `POSSIBLE_SANCTIONS_MATCH`); `ofac.py` (`screen`, `FLAG_CODE`); `payout_rules.py` (`route_one`).

**Last sync commit:** `aa9b3a118371f13335680fa747b91881f16a6072` (#16 create idempotency + transition audit; decide/_settle/route_one gates re-checked — Inv_NoSettleWithoutClear still holds; structural sync also requires settle-hold `if held:` and `record_transition` screen audit). Confirm before trusting after later edits. Move this forward only after you confirm the model still describes those functions.

## When to run

Run before merging any change that touches the rows above, or when `/maintain-formal-verification-skill` says this check is stale.

## How to run

Needs Java 11+ and `tla2tools.jar` (TLC). One-time:

```bash
mkdir -p /tmp/tla
curl -fsSL -o /tmp/tla/tla2tools.jar \
  https://github.com/tlaplus/tlaplus/releases/download/v1.8.0/tla2tools.jar
export TLA2TOOLS=/tmp/tla/tla2tools.jar
export JAVA="$(command -v java)"
```

From the repo root:

```bash
chmod +x .cursor/skills/verify-ofac-settle/scripts/run_check.sh
DESK_FORMAL_OUT=/tmp/disbursement-desk-formal-ofac \
  TLA2TOOLS=/tmp/tla/tla2tools.jar JAVA="$(command -v java)" \
  .cursor/skills/verify-ofac-settle/scripts/run_check.sh
```

That script:

1. **Structural sync** — fails if `decide` no longer refuses Approve on a sanctions flag, no longer passes `allow_sanctions_clear=(action == CLEAR)`, if `_settle` no longer re-screens when not cleared, if `explain_flag` drops `ofac.screen`, or if `route_one` settles without the `flag_code` / PAID gate.
2. **TLC** on `model/OfacSettle.cfg` with `GateCorrect = TRUE` (current code). Expect **no** `Inv_NoSettleWithoutClear` violation.

Optional proof that the property can fail (broken design):

```bash
DESK_FORMAL_OUT=/tmp/disbursement-desk-formal-ofac \
  TLA2TOOLS=/tmp/tla/tla2tools.jar JAVA="$(command -v java)" \
  .cursor/skills/verify-ofac-settle/scripts/run_check.sh --expect-fail-broken
```

Uses `model/OfacSettleBroken.cfg` (`GateCorrect = FALSE`). Expect a violation of `Inv_NoSettleWithoutClear`. Read the counterexample with `/statechart` (do not hard-code that skill’s CLI flags here).

To catch a **code** break the way the next agent should: remove the Approve+`flagged_sanctions` refusal in `decide` and change `allow_sanctions_clear=(action == CLEAR)` to `True`, then re-run `run_check.sh` (structural step must fail). Restore both afterward. Do not leave the scratch change in the branch.

## Pass vs modeling error vs bug

- **Pass:** structural OK + correct TLC finds no `Inv_NoSettleWithoutClear` break.
- **Modeling error:** TLC fails on `TypeOK`, deadlock you did not mean, or a break that cannot happen in `decide` / `_settle` / `route_one` because the model step is coarser/finer than the code. Fix the model; do not weaken `Inv_NoSettleWithoutClear`.
- **Real bug:** structural check fails, or a revised model that still matches the code lets a Match settle without Clear. Report the counterexample in code words (Approve on a flagged match reaches PAID, or route treats a match as clean). Do not change product code unless the user asks.

## Scope and limits

- Abstract `Match` bit only; does not prove `ofac.screen` fuzzy threshold / alias scoring (`MATCH_THRESHOLD`, short-name exactness).
- Single payout; no concurrent `decide` races or idempotency keys (see a future decide-exclusivity check).
- Does not model Desk UI button swapping (`desk.SANCTIONS_ACTIONS`) beyond the decide gates those buttons call.
- `route_one` already-SETTLED short-circuit in `_settle` is covered only as “clean path already wrote PAID+SETTLED after `explain_flag`”; a future change that writes PAID before screening is what structural + TLC guard.
- Cap of 6 steps; not a proof for unbounded session length.

## Maintain

After intentional OFAC / decide / settle / route changes, update the model and bump **Last sync commit**. Use `/maintain-formal-verification-skill` when coverage drifts.
