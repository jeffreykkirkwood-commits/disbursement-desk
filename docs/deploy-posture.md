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
`/var/tmp`), the app emits a structured log event
`deploy_posture_ephemeral_demo_directory` so aggregators can alert.

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
| `OFAC_HTTP_URL` / `OFAC_HTTP_TOKEN` | Live OFAC vendor |
| `TURSO_DATABASE_URL` / `TURSO_AUTH_TOKEN` | Durable SQLite |
| `PUBLIC_ORIGIN` | Explicit HTTPS origin (else `RENDER_EXTERNAL_URL`) |

Code inventory of secret keys: `deploy_posture.SECRET_ENV_KEYS`.

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
