# Observability (P1)

Desk already writes **structured JSON** log lines (`event_log.event`) with
`request_id`, `event` name, and fields. This slice adds **process-local
counters** for decide, create, HTTP 503, stream slots, and OFAC holds.

## Counters

| Name | When |
|------|------|
| `desk_decide_total` | Successful decide HTTP path (labels: `action`, `http_status`, `replayed`) |
| `desk_create_total` | Successful `/payouts/simulate` create (labels: `http_status`, `payout_status`, `replayed`) |
| `desk_http_503_total` | Any HTTP 503 (labels: `reason`, `endpoint`) |
| `desk_stream_open_total` | Stream slot acquired |
| `desk_stream_refused_total` | Stream slot refused (label: `reason=slot_cap`) |
| `desk_ofac_hold_total` | New OFAC hold (label: `source=create` or `source=screen`) |

`reason` values for 503 include `demo_capacity`, `desk_load`, `stream_cap`, and
`http_503` (fallback).

`source=create` means `/payouts/simulate` or `POST /v1/payouts` left the payout
`FLAGGED` with `POSSIBLE_SANCTIONS_MATCH`. `source=screen` means a pre-send
re-screen on decide or settle recorded action `screen` and left the payout
`FLAGGED`. Legacy routes and `/v1` share these counters. A replay of that
create or decide does not increment again. Operator Approve, Hold, Clear, and
Block do not increment this counter. A non-sanctions flag does not increment it.

Each `metrics.inc` also emits a structured `metric_recorded` log line for log
aggregators (Datadog / CloudWatch / etc. that scrape JSON stdout).

## Endpoints

- `GET /metrics` — JSON snapshot for this process. Does **not** mint a demo.
- `GET /health` — liveness only (unchanged).

## Deploy notes

- Counters are **per Gunicorn worker**. With one worker (current Desk posture)
  `/metrics` is the whole service. Multi-worker needs a shared backend or
  scrape-all-instances before alerting on absolute counts.
- Stream **slot cap** is shared across workers via the file backend
  (`docs/stream-slots.md`). Refuse/open counters on `/metrics` are still
  per-process, so sum across workers when alerting.
- Suggested alerts: spike in `desk_http_503_total{reason=stream_cap|demo_capacity}`,
  sustained 5xx via log `http_response`, and any increase in `desk_ofac_hold_total`.
  Sum `source=create` and `source=screen`, and sum workers, before paging on
  the absolute value.
