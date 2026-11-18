# Decide exclusivity (P2)

Concurrent Approve/Clear `decide()` calls on one payout must **settle at most once**.
A repeated `Idempotency-Key` replays the first event (200) without a second write.
Conflicting terminal keys (Approve vs Return) racing on the same payout yield one 201
mutation and 409 refusals once the payout is no longer decidable. Hold-then-approve
remains allowed sequentially (issue #41).

## Enforcement

1. **`BEGIN IMMEDIATE`** — `payouts.decide` takes an exclusive SQLite write lock for the
   whole lookup → transition → `payout_events` insert, so overlapping calls serialize.
2. **`idempotency_key TEXT UNIQUE`** — the action-log schema rejects a second insert for
   the same key; the decide path looks up the key first and returns a 200 replay when
   the details match (409 when they do not).
3. **DECIDABLE gate** — only `FLAGGED` / `HELD` accept a new decision; after one approve
   settle or hold/return, peers get 409.

## Proof

| Layer | Where |
|-------|--------|
| Product stress | `test_decide_exclusivity.py` (thread + multi-process fan-in, Clear/Block OFAC races, repeated rounds), `ConcurrentDecideTests` in `test_pre_send_gates.py` |
| Formal model | `.cursor/skills/verify-decide-exclusivity/` (`Inv_AtMostOneSettle`) |

Run the formal check (needs Java + `tla2tools.jar`):

```bash
DESK_FORMAL_OUT=/tmp/disbursement-desk-formal-decide \
  TLA2TOOLS=/tmp/tla/tla2tools.jar JAVA="$(command -v java)" \
  .cursor/skills/verify-decide-exclusivity/scripts/run_check.sh
```

Optional unlocked counterexample: add `--expect-fail-unlocked`.

## Related

Pre-send / OFAC gates: [pre-send-gates.md](pre-send-gates.md). OFAC formal check:
`.cursor/skills/verify-ofac-settle/`. Stream slots: [stream-slots.md](stream-slots.md).
