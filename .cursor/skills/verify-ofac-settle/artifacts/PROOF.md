# Proof that verify-ofac-settle can fail and recovers

## Baseline (GateCorrect=TRUE)

`run_check.sh` structural sync OK; TLC finds no `Inv_NoSettleWithoutClear` violation
(48 states / 22 distinct). See `tlc-gate-correct.out`.

## Broken model (GateCorrect=FALSE)

`run_check.sh --expect-fail-broken` — TLC reports
`Invariant Inv_NoSettleWithoutClear is violated` on
`RouteOne` settling a Match without Clear. See `tlc-gate-broken.out`.
Read counterexamples with `/statechart`.

## Scratch break in product code

In `payouts.decide`: commented out the Approve+`flagged_sanctions` 409, and changed
`_settle(..., allow_sanctions_clear=(action == CLEAR))` to `allow_sanctions_clear=True`.

- Structural check failed (exit 1) on the Clear-only `allow_sanctions_clear` pattern.
- Behavioral: create OFAC demo payee → Approve returned 201 with `status=PAID` /
  `execution_status=SETTLED` (property broken).

Scratch restored via `git checkout -- payouts.py`. Baseline `run_check.sh` exit 0 again;
Approve on match returns 409; Clear still settles.
