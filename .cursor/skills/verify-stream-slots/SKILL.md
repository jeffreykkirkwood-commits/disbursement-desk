---
name: verify-stream-slots
description: "Formally check disbursement-desk payout stream slot cap never exceeds MAX_STREAMS under concurrent acquire/release (#83). Use when changing payout_events acquire_stream, release_stream, _stream_lock, stream_slots.py, or hosted_app payout_stream."
---

# Verify stream slots

## Property (plain words)

**Promise:** After any number of overlapping stream acquires and releases, the process never holds more open stream slots than `MAX_STREAMS` (2 in production).

**Harm if broken:** A third (or more) SSE connection can occupy every Gunicorn thread so `/health` and the Desk stop answering under load (#83).

## Tool and why

**TLA+ / TLC** (via pstack `/statechart`) over a small model of `payout_events.acquire_stream` / `release_stream`. Ordering and interleaving of concurrent acquires is the question; a model checker fits better than tests that only sample schedules. Bounds: 3 clients, `MaxSlots = 2`, `MaxSteps = 8`.

Read every TLC result with `/statechart` so invariant breaks show as shortest paths in plain words. Do not copy statechart CLI flags from memory — use that skill’s current command.

## Coverage (keep in sync)

| Model name | Code |
|---|---|
| `openCount` | `payout_events._stream_count` |
| `MaxSlots` | `payout_events.MAX_STREAMS` |
| `AcquireLocked` | `acquire_stream()` body under `with _stream_lock` |
| `ReleaseLocked` | `release_stream()` body under `with _stream_lock` |
| `AcquireSee` / `AcquireBump` | check-then-increment **without** the lock |
| `Held` | a client that successfully acquired and has not yet released |

Files: `payout_events.py` (`acquire_stream`, `release_stream`, `_stream_lock`, `_stream_count`, `MAX_STREAMS`); `hosted_app.py` (`payout_stream` acquire-before-response / release in `finally`).

**Last sync commit:** `d0d94381cb2ccee08c666484e6fb86a7ba83b0dc` (confirm before trusting after later edits). Move this forward only after you confirm the model still describes those functions.

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
chmod +x .cursor/skills/verify-stream-slots/scripts/run_check.sh
DESK_FORMAL_OUT=/tmp/disbursement-desk-formal-stream \
  TLA2TOOLS=/tmp/tla/tla2tools.jar JAVA="$(command -v java)" \
  .cursor/skills/verify-stream-slots/scripts/run_check.sh
```

That script:

1. **Structural sync** — fails if `acquire_stream` / `release_stream` no longer keep the count check / bump / decrement under `with _stream_lock`.
2. **TLC** on `model/StreamSlots.cfg` with `Locked = TRUE` (current code). Expect **no** `Inv_SlotsNeverExceeded` violation.

Optional proof that the property can fail (broken design):

```bash
DESK_FORMAL_OUT=/tmp/disbursement-desk-formal-stream \
  TLA2TOOLS=/tmp/tla/tla2tools.jar JAVA="$(command -v java)" \
  .cursor/skills/verify-stream-slots/scripts/run_check.sh --expect-fail-unlocked
```

Uses `model/StreamSlotsUnlocked.cfg` (`Locked = FALSE`). Expect a violation of `Inv_SlotsNeverExceeded`. Read the counterexample with `/statechart` (do not hard-code that skill’s CLI flags here).

To catch a **code** break the way the next agent should: delete or bypass `with _stream_lock` in `acquire_stream` / `release_stream`, re-run `run_check.sh` (structural step must fail). Restore the lock afterward. Do not leave the scratch change in the branch.

## Pass vs modeling error vs bug

- **Pass:** structural OK + locked TLC finds no `Inv_SlotsNeverExceeded` break.
- **Modeling error:** TLC fails on `TypeOK`, deadlock you did not mean, or a break that cannot happen in `payout_events.py` because the model step is coarser/finer than the code. Fix the model; do not weaken `Inv_SlotsNeverExceeded`.
- **Real bug:** structural check fails, or a revised model that still matches the code violates the cap. Report the counterexample in code words (two acquires both see `count < MAX`, both bump). Do not change product code unless the user asks.

## Scope and limits

- In-process counter only; multi-worker deploy has one counter per process (same as production’s one Gunicorn worker).
- Does not model SSE payload contents, Last-Event-ID resume, or Flask request plumbing beyond acquire/release.
- Cap value in TLC is 2, matching production `MAX_STREAMS`.
- Does not prove the route’s `finally` always runs on every disconnect path beyond “release is available after acquire”.

## Maintain

After intentional stream-slot changes, update the model and bump **Last sync commit**. Use `/maintain-formal-verification-skill` when coverage drifts.
