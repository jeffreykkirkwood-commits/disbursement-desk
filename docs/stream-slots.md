# Shared stream slots (P1 deploy posture)

`/payouts/stream` holds a Gunicorn thread for up to `HOLD_SECONDS`. The Desk
caps concurrent open streams at `MAX_STREAMS` (2) so `/health` stays reachable
(#83).

## Backends

| `STREAM_SLOT_BACKEND` | Behavior |
|-----------------------|----------|
| `file` (default) | flock + JSON leases under `DEMO_DIRECTORY/_stream_slots/`. Shared by every worker on the instance that mounts the same directory. |
| `memory` | Process-local counter (unit tests / formal verify-stream-slots fallback). |

`hosted_app.create_app` always calls `payout_events.configure_stream_slots(directory)`.
Acquire returns a lease **token**; release must pass that token (file backend).
Leases expire after `HOLD_SECONDS + STREAM_SLOT_LEASE_GRACE_SECONDS` (default
grace 30s) so a crashed worker cannot permanently exhaust the cap.

## Multi-worker

Raising Gunicorn `--workers` is safe for the stream cap only when workers share
one writable `DEMO_DIRECTORY` (Render disk or equivalent). Metrics counters in
`/metrics` remain per-process; the slot **cap** itself is global via the file
store.

## Env

- `STREAM_SLOT_BACKEND` — `file` or `memory`
- `STREAM_SLOT_LEASE_GRACE_SECONDS` — added to `HOLD_SECONDS` for lease TTL
- `DEMO_DIRECTORY` — parent of `_stream_slots/`
