# Stream cap

Live payout update streams (`GET /payouts/stream`) are capped at two concurrent connections in the process so `/health` and the Desk keep a free Gunicorn thread. A third open stream gets HTTP 503 with `Retry-After`.

## Sub-features

- `stream-open` serves `text/event-stream` for this visitor's demo when a slot is free.
- `stream-cap-503` rejects a third concurrent stream with 503 and Retry-After.
- `stream-release` frees the slot when the connection ends so a later request can acquire.

## How to get to it (user POV)

- A client that already has a Desk cookie opens `/payouts/stream` (the Desk page does not subscribe yet; this is the API surface for live updates).
- Opening a third stream while two are held shows a short plain-text retry message.

## Driving it with verify_desk

Preconditions:

- Instance launched for `$RUN_ID`; `doctor` passes.
- Evidence dir `$EVIDENCE` exists.
- Prefer holding streams with long-lived HTTP clients (see `test_payout_events.py` third-stream scenarios) rather than the short helper `http` command, which reads the body to completion and releases the slot.

- **Single stream snapshot.** With a minted cookie, `http --path /payouts/stream --with-cookie --out "$EVIDENCE/stream-cap/one" --expect-status 200`. Body is SSE (`event: created` / `status-changed` or keepalive comments). The default hold is short enough that the helper can finish.
- **Cap proof.** Reproduce with three overlapping clients as in `test_payout_events.StreamTests.test_third_stream_returns_503`, capturing the third response status `503` and `Retry-After` under `$EVIDENCE/stream-cap/third`. Formal check: `/verify-stream-slots`.

## Gotchas

- The helper's `http` command drains the response; it does not hold a slot for concurrency proofs. Use overlapping clients or the formal skill for the cap invariant.
- Streams require a visitor cookie; POSTs and streams never create demos.
- `MAX_STREAMS` is 2 in `payout_events.py`; do not weaken the journey if a third stream incorrectly succeeds.
