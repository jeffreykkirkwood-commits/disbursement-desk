# Deploy posture (P1)

Render hosts the Desk as a **free** Python web service today. This slice checks
in a Blueprint (`render.yaml`) and documents what survives a redeploy, where
secrets live, and how free-tier spin-down behaves.

## Blueprint

[`render.yaml`](../render.yaml) declares:

| Field | Value |
|-------|--------|
| Service | `disbursement-desk` (web, Python) |
| Region / plan | `ohio` / `free` |
| Build | `pip install -r requirements.txt` |
| Start | Gunicorn, **1 worker / 4 threads**, `hosted_app:create_app()` |
| Health check | `GET /health` |
| `DEMO_DIRECTORY` | `/tmp/payments-demos` (ephemeral on free) |

Link the Blueprint in the Render Dashboard (or create from this file) so future
infra drifts are visible. Linking does not require recreating the live service
when the `name` matches.

## Health vs ready

| Path | Meaning |
|------|---------|
| `GET /health` | Process is up; does **not** touch demo storage. Use for Render health checks. |
| `GET /ready` | `DEMO_DIRECTORY` is writable; **503** when not. |
| `GET /metrics` | Process-local counters (see [observability.md](observability.md)). |

## Ephemeral filesystem

Free web services have **no persistent disk**. Everything under
`DEMO_DIRECTORY` (default `/tmp/payments-demos`) is lost on deploy, restart, or
spin-down.

On boot, when `RENDER` is set and the demo directory is under `/tmp` (or
`/var/tmp`), the app emits one structured log event,
`deploy_posture_ephemeral_demo_directory`. Alert on that event name. It means
this demo directory will be removed on deploy, restart, or spin-down.

**Durable options (pick one):**

1. **Turso tenancy** (preferred for multi-visitor) — see [tenancy.md](tenancy.md).
2. **Paid plan + disk** — upgrade the web service, then add to `render.yaml`:

```yaml
    plan: starter   # disks require a paid plan
    disk:
      name: desk-demos
      mountPath: /var/data
      sizeGB: 1
    envVars:
      - key: DEMO_DIRECTORY
        value: /var/data/payments-demos
```

Disk-backed services stay **single-instance** (no autoscaling / zero-downtime
deploys). Shared stream slots still need one writable `DEMO_DIRECTORY`; see
[stream-slots.md](stream-slots.md).

## Secrets

Never commit credentials. Set these in the Render Dashboard (or a secret env
group). The Blueprint lists them as `sync: false` so sync does not overwrite
Dashboard values after first create:

| Key | Purpose |
|-----|---------|
| `OPERATOR_BEARER_TOKENS` | API Bearer → operator map |
| `DESK_OIDC_CLIENT_SECRET` | OIDC client secret. Omit `DESK_OIDC_ISSUER` to keep demo mint. |
| `STRIPE_SECRET_KEY` | Connect / Stripe SDK (test mode) |
| `STRIPE_WEBHOOK_SECRET` | Webhook signature check |
| `RENDER_EVENTS_SECRET` | Shared secret for `POST /internal/render-events` |
| `OFAC_HTTP_URL` / `OFAC_HTTP_TOKEN` | Live OFAC vendor |
| `TURSO_DATABASE_URL` / `TURSO_AUTH_TOKEN` | Durable SQLite |
| `PUBLIC_ORIGIN` | Explicit HTTPS origin (else `RENDER_EXTERNAL_URL`) |

Code inventory of secret keys: `deploy_posture.SECRET_ENV_KEYS`.

## Deploy failure notifications

Use the Render Dashboard for a failed deploy or an unhealthy service. No Desk
secret is required.

1. Open the workspace home in the Render Dashboard.
2. Open **Integrations**, then **Notifications**.
3. Set **Notification Destination** to Email, Slack, or both.
4. Set **Default Service Notifications** to **Only failure notifications**.

That level sends a notice when a build or deploy fails, and when a running
service becomes unhealthy. Connect Slack on that page before you choose Slack.
Per-service overrides are on the service **Settings** page, under
**Notifications**.

Metric and log signals are in [observability.md](observability.md).

## Inbound deploy events

`POST /internal/render-events` records a deploy notification from a caller you
control. Send the thin JSON object Render uses for webhooks. Include `type`
and `timestamp`. Include `data.id`, `data.serviceId`, and `data.serviceName`
when you have them. Include `data.status` for events that report a result,
such as `deploy_ended`.

Render's webhook product signs requests with `webhook-signature`. This route
checks `X-Render-Events-Secret` only. Pointing a Render webhook at this URL
makes Render retry, then disable that webhook.

Set `RENDER_EVENTS_SECRET` in the Dashboard. The Blueprint marks it
`sync: false`. When the variable is unset or blank, every method on this path
returns 404. The body matches an unknown path. The process does not count the
call, and it does not write `render_deploy_event`.

When the variable is set, send that value in `X-Render-Events-Secret`. Leading
and trailing whitespace on the variable and on the header is ignored. A missing
or wrong header is also 404. A matching header on a method other than POST
returns 405.

A matching POST with a JSON object returns 204. The process writes
`render_deploy_event` and increments `desk_render_deploy_event_total`. The log
line can include `event_type`, `status`, `service_id`, `service_name`, and
`render_event_id`. The secret stays out of the log. The raw body stays out of
the log. An unknown `type` is counted as `event_type` `other`. `status` is
counted only for `succeeded`, `failed`, or `canceled`. A body that is not a
JSON object returns 400 and does not count.

Each accepted POST increments the counter once. A retry is another increment.
The counter is process-local, same as the other Desk counters.

## Free-tier spin-down

Free web services **spin down after ~15 minutes** of inactivity. The next
request pays a cold start. Options:

- Accept cold starts for the demo.
- External uptime ping against `/health` on an interval under 15 minutes.
- Upgrade to a paid instance that stays warm.

Do **not** rely on in-process timers; the instance is gone while spun down.

## Multi-worker

Current start command uses **one** Gunicorn worker. Raising `--workers` is only
safe for the stream-slot cap when every worker shares one writable
`DEMO_DIRECTORY` (disk or equivalent). Metrics on `/metrics` stay per-process.

## Related

- [operator-authz.md](operator-authz.md) — `/health` / `/ready`, bearer tokens
- [tenancy.md](tenancy.md) — Turso / visitor isolation
- [stream-slots.md](stream-slots.md) — file-backed slot cap
- [observability.md](observability.md) — counters and suggested alerts
