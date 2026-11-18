# Observability (P1)

Desk already writes **structured JSON** log lines (`event_log.event`) with
`request_id`, `event` name, and fields. This slice adds **process-local
counters** for decide, create, HTTP 503, stream slots, and OFAC holds.

## Sentry (Wave O1)

When `SENTRY_DSN` is set, `hosted_app.create_app` initializes `sentry-sdk` with
Flask integration and sets `release` from `RENDER_GIT_COMMIT`. The
`internal_error` handler reports the exception to Sentry (Flask's errorhandler
swallows it before the SDK's signal). When the DSN is unset or blank, the SDK
stays idle (CI and local demos need no secret). Set the DSN only in the Render
Dashboard (`sync: false` in `render.yaml`).

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
- `GET /health` and `GET /ready` — see [Probe roles](#probe-roles) below.
- `POST /internal/render-events`. Accepted deploy notices only. The gate is in [deploy-posture.md](deploy-posture.md).

## Probe roles

| Path | Role | What it checks | Use for |
|------|------|----------------|---------|
| `GET /health` | Liveness | Process is up. Does not touch `DEMO_DIRECTORY`. | Render **Health Check Path**. External keep-warm pings on free tier. |
| `GET /ready` | Readiness | `DEMO_DIRECTORY` is writable. Returns **503** when it is not. | Traffic admission before you send work that needs demo storage. Not the Render health check today. |

Do not point Render's health check at `/ready`. A full disk or a missing demo
directory would mark the instance unhealthy and can loop restarts while `/health`
still proves the process is alive. Keep `/health` on the load balancer. Use
`/ready` when an orchestrator or smoke script must know storage can accept
demos. More deploy context is in [deploy-posture.md](deploy-posture.md).

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

## Jeffrey checklist: Render failure notifications (human)

Agents cannot finish this step. Jeffrey owns it in the Render Dashboard. No
Desk secret and no Sentry or OTLP wiring is required.

- [ ] Open the workspace home in the Render Dashboard.
- [ ] Open **Integrations**, then **Notifications**.
- [ ] Set **Notification Destination** to Email, Slack, or both.
- [ ] Set **Default Service Notifications** to **Only failure notifications**.
- [ ] If you want Slack, connect Slack on that same page before you choose it.
- [ ] Optional: open the `disbursement-desk` service **Settings** →
  **Notifications** and confirm it inherits or matches the workspace default.

**Only failure notifications** covers a failed build or deploy, and a running
service that becomes unhealthy. The same steps live in
[deploy-posture.md](deploy-posture.md).

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

## Runbooks

Short how-tos for the page signals above. Confirm the signal, then act. These
do not wait on Sentry DSN or OTLP.

### HTTP 5xx

1. Confirm. In Render Logs, find `event` `http_response` with `http_status`
   ≥ 500, or find `event` `internal_error`. Note `request_id` and `error_type`.
2. Check whether a deploy is in progress or just finished. A bad deploy is a
   failed-deploy case, not a random 5xx.
3. Hit `GET /health`. If it fails, the process is down. Use the failed-deploy
   or capacity runbook next.
4. Hit `GET /ready`. If `/health` is ok and `/ready` is 503, demo storage is
   the problem. Use the ephemeral demo directory runbook.
5. Retry the caller with the same idempotency key when the response says to.
6. If 5xx continues on a healthy, ready instance, capture the `request_id`
   lines and open an incident against the change that landed last.

### HTTP 503 capacity

1. Confirm. Read `desk_http_503_total` on `GET /metrics` (send
   `X-Metrics-Secret` when `DESK_METRICS_SECRET` is set). Check the `reason`
   label: `demo_capacity`, `desk_load`, `stream_cap`, or `http_503`.
2. For `stream_cap`, open streams are at the shared slot cap. Wait for streams
   to close, or stop opening new ones. See [stream-slots.md](stream-slots.md).
3. For `demo_capacity`, the service is refusing new demos. Check free-tier
   spin-down and disk pressure under `DEMO_DIRECTORY`.
4. For `desk_load` or bare `http_503`, check Render Metrics for CPU and memory,
   then check recent deploys.
5. Confirm `GET /health` still answers so capacity pressure has not taken the
   process down.
6. Do not raise Gunicorn `--workers` on free ephemeral storage without a shared
   writable `DEMO_DIRECTORY`. See [deploy-posture.md](deploy-posture.md).

### OFAC hold burst

1. Confirm. Watch `desk_ofac_hold_total`. Sum `source=create` and
   `source=screen`. A burst means many new sanctions holds, not Approve or
   Clear traffic.
2. Sample recent FLAGGED payouts with `POSSIBLE_SANCTIONS_MATCH`. Note whether
   holds came from create (`/payouts/simulate` or `POST /v1/payouts`) or from
   pre-send re-screen.
3. If a live vendor is configured (`OFAC_HTTP_URL`), check vendor errors and
   latency in logs. A vendor outage can force holds.
4. If the fixture or demo seed looks wrong after a reset, re-seed and compare
   hold rate to the last known good demo.
5. Leave holds in place until an operator Clears or Blocks. Do not settle
   OFAC-matching payouts to PAID.

### Failed deploy

1. Confirm. Use the Render failure email or Slack notice, or an accepted
   `desk_render_deploy_event_total` with `event_type` `deploy_ended` and
   `status` `failed` (or `event_type` `server_failed`).
2. Open the failed deploy in the Render Dashboard. Read the build or runtime
   log for the first hard error.
3. Check whether required Dashboard secrets are missing after a Blueprint sync.
   Secret keys are listed in [deploy-posture.md](deploy-posture.md).
4. Confirm Jeffrey's notification checklist above is done so the next failure
   still pages.
5. Fix forward on `main` or roll back to the last known good deploy. Re-check
   `GET /health` after the service is live.

### Ephemeral demo directory

1. Confirm. Find log event `deploy_posture_ephemeral_demo_directory` on boot
   when `RENDER` is set and `DEMO_DIRECTORY` is under `/tmp` or `/var/tmp`.
2. Treat demo state as disposable. Expect loss on deploy, restart, and free
   spin-down.
3. After a wipe, mint a new demo session. Do not chase files under
   `/tmp/payments-demos`.
4. If you need durable demos, pick Turso tenancy or a paid disk mount. Steps
   are in [deploy-posture.md](deploy-posture.md).
5. After you move `DEMO_DIRECTORY` off ephemeral storage, confirm `GET /ready`
   returns 200 and that the boot warning no longer appears.
