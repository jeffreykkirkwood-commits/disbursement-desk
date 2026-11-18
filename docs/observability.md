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
| `desk_render_deploy_event_total` | Accepted `POST /internal/render-events` (labels: `event_type`, and `status` when it is `succeeded`, `failed`, or `canceled`) |

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
  Send `X-Metrics-Secret` matching `DESK_METRICS_SECRET` when that env is set.
  On Render with the secret unset, the path answers **404**. Local / unit tests
  leave the secret unset and keep the route open.
- `GET /health` — liveness only (unchanged).
- `POST /internal/render-events`. Accepted deploy notices only. The gate is in [deploy-posture.md](deploy-posture.md).

## Abuse limits (Wave C)

Mutating POSTs (`decide`, `/v1` create, reset, simulate, demo-scenario,
fixture reconciliation) share an in-process sliding window keyed by route
bucket + client IP + demo session (`rate_limits.py`). Over the cap → **429**
with `code=RATE_LIMITED`. Defaults: 60 hits / 60s (`DESK_RATE_LIMIT_MAX`,
`DESK_RATE_LIMIT_WINDOW_SECONDS`). Limits are per Gunicorn worker.

## Deploy notes

- Counters are **per Gunicorn worker**. With one worker (current Desk posture)
  `/metrics` is the whole service. Multi-worker needs a shared backend or
  scrape-all-instances before alerting on absolute counts.
- Stream **slot cap** is shared across workers via the file backend
  (`docs/stream-slots.md`). Refuse/open counters on `/metrics` are still
  per-process, so sum across workers when alerting.

## Turn on Render failure notifications

Use the Render Dashboard when a deploy fails or a service becomes unhealthy.
This path needs no Desk secret.

1. Open the workspace home.
2. Open **Integrations**, then **Notifications**.
3. Set **Notification Destination** to Email, Slack, or both.
4. Set **Default Service Notifications** to **Only failure notifications**.

**Only failure notifications** covers a failed build or deploy, and a running
service that becomes unhealthy. Connect Slack on that same page before you
choose Slack. To override one service, open that service's **Settings** page
and find **Notifications**.

## Signals to page on

Page when one of these moves. Sum Gunicorn workers before you compare an
absolute count. With the current one-worker start command, `/metrics` is the
whole service.

- `desk_http_503_total` increases. Treat `reason` `stream_cap` and
  `demo_capacity` as capacity problems.
- `desk_ofac_hold_total` increases. Add `source=create` and `source=screen`.
- A log line has `event` `http_response` and `http_status` of 500 or higher.
- A log line has `event` `deploy_posture_ephemeral_demo_directory`. The demo
  directory is ephemeral. See [deploy-posture.md](deploy-posture.md).
- `desk_render_deploy_event_total` increases with `event_type` `deploy_ended`
  and `status` `failed`, or with `event_type` `server_failed`. The counter
  moves only for an accepted `POST /internal/render-events`. The gate is in
  [deploy-posture.md](deploy-posture.md).
