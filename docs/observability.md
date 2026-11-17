# Observability (P1)

Desk already writes **structured JSON** log lines (`event_log.event`) with
`request_id`, `event` name, and fields. This slice adds **process-local
counters** for the four production signals called out in the readiness roadmap.

## Counters

| Name | When |
|------|------|
| `desk_decide_total` | Successful decide HTTP path (labels: `action`, `http_status`, `replayed`) |
| `desk_create_total` | Successful `/payouts/simulate` create (labels: `http_status`, `payout_status`, `replayed`) |
| `desk_http_503_total` | Any HTTP 503 (labels: `reason`, `endpoint`) |
| `desk_stream_open_total` | Stream slot acquired |
| `desk_stream_refused_total` | Stream slot refused (label: `reason=slot_cap`) |

`reason` values for 503 include `demo_capacity`, `desk_load`, `stream_cap`, and
`http_503` (fallback).

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
  sustained 5xx via log `http_response`, OFAC hold rate from decide/create
  labels / flag events (product alert, not wired here).
