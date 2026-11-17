---
name: verify-decide-exclusivity
description: "Formally check disbursement-desk concurrent decide exclusivity — at most one mutating decision per payout under BEGIN IMMEDIATE + UNIQUE idempotency keys (JSP-17). Use when changing payouts.decide, record_transition, or payout_events.idempotency_key."
---

# Verify decide exclusivity

## Property (plain words)

**Promise:** Overlapping Approve/Clear `decide()` calls on one payout settle at most once when the save runs under `BEGIN IMMEDIATE`. A repeated Idempotency-Key replays the first event without a second mutation. (Hold-then-approve remains allowed sequentially — #41.)

**Harm if broken:** Double-send / conflicting Approve+Return / duplicate settle audit rows → wrong Sent state and a broken action log.

## Tool and why

**TLA+ / TLC** over a small model of `payouts.decide`. Ordering of concurrent decides is the question; a model checker fits better than tests that only sample schedules. Bounds: 3 clients, `MaxSteps = 8`, `Locked = TRUE` for current code.

Read every TLC result with `/statechart` so invariant breaks show as shortest paths in plain words. Do not copy statechart CLI flags from memory — use that skill’s current command.

## Coverage (keep in sync)

| Model name | Code |
|---|---|
| `status` | `payouts.status` after approve settle (`FLAGGED` → `PAID`) |
| `settleCount` | Successful Approve/Clear settles to PAID |
| `DecideLocked` | `decide()` approve path under `BEGIN IMMEDIATE` |
| `See` / `Commit` | Check-then-act **without** the transaction lock |
| `usedKey[c]` | That client’s `idempotency_key` already stored |
| `Locked` | `TRUE` = current code; `FALSE` = unlocked race |

Files: `payouts.py` (`decide`, `record_transition`, SCHEMA `idempotency_key TEXT UNIQUE`).

**Last sync commit:** `ffec688a95c44a6f9f45fa982de3c85e4b92dca9` (confirm before trusting after later edits). Move this forward only after you confirm the model still describes those functions.

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
chmod +x .cursor/skills/verify-decide-exclusivity/scripts/run_check.sh
DESK_FORMAL_OUT=/tmp/disbursement-desk-formal-decide \
  TLA2TOOLS=/tmp/tla/tla2tools.jar JAVA="$(command -v java)" \
  .cursor/skills/verify-decide-exclusivity/scripts/run_check.sh
```

That script:

1. **Structural sync** — fails if `decide` drops `BEGIN IMMEDIATE`, the idempotency lookup/insert, or `record_transition`.
2. **TLC** on `model/DecideExclusivity.cfg` with `Locked = TRUE` (current code). Expect **no** `Inv_AtMostOneSettle` violation.

Optional proof that the property can fail (broken design):

```bash
DESK_FORMAL_OUT=/tmp/disbursement-desk-formal-decide \
  TLA2TOOLS=/tmp/tla/tla2tools.jar JAVA="$(command -v java)" \
  .cursor/skills/verify-decide-exclusivity/scripts/run_check.sh --expect-fail-unlocked
```

Uses `model/DecideExclusivityUnlocked.cfg` (`Locked = FALSE`). Expect a violation of `Inv_AtMostOneSettle`.

To catch a **code** break the way the next agent should: remove `BEGIN IMMEDIATE` from `decide`, re-run `run_check.sh` (structural step must fail). Restore afterward. Do not leave the scratch change in the branch.

## Pass vs modeling error vs bug

- **Pass:** structural OK + locked TLC finds no `Inv_AtMostOneSettle` break.
- **Modeling error:** TLC fails on `TypeOK`, deadlock you did not mean, or a break that cannot happen in `decide` because the model step is coarser/finer than the code. Fix the model; do not weaken `Inv_AtMostOneSettle`.
- **Real bug:** structural check fails, or a revised model that still matches the code lets two mutating decides commit. Report the counterexample in code words. Do not change product code unless the user asks.

## Scope and limits

- Models exclusivity of one mutating decide per payout under concurrency; does not re-prove OFAC Clear gates (`verify-ofac-settle`) or stream caps (`verify-stream-slots`).
- Actions are abstracted (approve → PAID vs non-approve → HELD); Clear/Block/screen hold share the same “one mutation” property.
- Cap of 8 steps; not a proof for unbounded session length.
- Product stress: `test_decide_exclusivity.py` / `ConcurrentDecideTests` in `test_pre_send_gates.py`.

## Maintain

After intentional decide / schema / idempotency changes, update the model and bump **Last sync commit**. Use `/maintain-formal-verification-skill` when coverage drifts.
