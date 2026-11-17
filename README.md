# Disbursement Desk

The Disbursement Desk, moving here from [payments-api-simulator](https://github.com/jeffreykkirkwood-commits/payments-api-simulator) (issue [#122](https://github.com/jeffreykkirkwood-commits/payments-api-simulator/issues/122)).

This first commit holds only the repo's checks, copied unchanged from payments-api-simulator at `d272119`:
CI, Card format, Secret scan (gitleaks), Inventory updated, Live smoke check (manual), Dependabot, and the PR and issue templates.
The Desk's code arrives in later pull requests, with its git history kept.

Synthetic data only. No secrets in this repo.

Pre-send exception and OFAC gates (mandatory before Sent): see [docs/pre-send-gates.md](docs/pre-send-gates.md).

Operator identity (session-bound actor, reviewer vs admin, `/ready`): see
[docs/operator-authz.md](docs/operator-authz.md). Set `DESK_OIDC_ISSUER` to
sign browser operators in with OIDC. Tests use `DESK_OIDC_MOCK=1` and do not
call an identity provider. See [docs/operator-oidc-design.md](docs/operator-oidc-design.md).

Turso / multi-visitor tenancy: see [docs/tenancy.md](docs/tenancy.md). A shared
`TURSO_DATABASE_URL` across visitors is refused unless tenancy is configured.

Observability (structured logs + decide/create/503/stream counters): see
[docs/observability.md](docs/observability.md). Snapshot: `GET /metrics`.

Deploy posture (Render Blueprint, ephemeral `/tmp`, secrets, free spin-down):
see [docs/deploy-posture.md](docs/deploy-posture.md). Blueprint: `render.yaml`.

Render failure notices use Dashboard email or Slack (failed deploy, unhealthy
service). `POST /internal/render-events` stays closed until
`RENDER_EVENTS_SECRET` is set in the Dashboard (`sync: false`). See
[docs/deploy-posture.md](docs/deploy-posture.md) and
[docs/observability.md](docs/observability.md).

Multi-worker stream-slot cap (`STREAM_SLOT_BACKEND=file` under
`DEMO_DIRECTORY/_stream_slots`): see [docs/stream-slots.md](docs/stream-slots.md).

Versioned create/decide (`/v1/payouts`, OpenAPI, stable error `code`): see
[docs/api-contract.md](docs/api-contract.md). Document: `GET /openapi.json`.

Optional live OFAC HTTP vendor (`OFAC_PROVIDER=http`, same Clear/Block UX): see
[docs/ofac-vendor.md](docs/ofac-vendor.md).

Rail per-payment limits and same-day cutoff windows are enforced on create/settle:
see [docs/rail-enforcement.md](docs/rail-enforcement.md).

Concurrent decide exclusivity (stress + formal `Inv_AtMostOneSettle`): see
[docs/decide-exclusivity.md](docs/decide-exclusivity.md).

Desk detail Activity (who/what/when from `payout_events`): see
[docs/desk-audit.md](docs/desk-audit.md).

Stripe Connect test-mode Transfers stay off unless `STRIPE_PAYOUTS_ENABLED`
is exactly `1`. Any other value keeps the demo settle. See
[Stripe Connect go-live](docs/stripe-connect-go-live.md).
The account-type recommendation is Express, and Jeff has not signed it. See
[Account ownership](docs/stripe-connect-account-ownership.md).
To disarm Transfers, see
[Disarm Stripe payouts](docs/stripe-connect-kill-switch.md).

## Stripe environment

Set these in the environment. Do not commit them.

- `STRIPE_PAYOUTS_ENABLED`: `1` arms test-mode Transfers. Any other value keeps the demo settle.
- `STRIPE_SECRET_KEY`: test key. Prefer `rk_test_`. `sk_test_` is allowed. `sk_live_` and `rk_live_` are refused.
- `STRIPE_WEBHOOK_SECRET`: signing secret for `POST /stripe/webhook`. It must start with `whsec_`.
- `STRIPE_DESTINATION_MAP`: JSON object mapping a `payee_account` to an `acct_` id. A payout column `stripe_connected_account_id` wins when it is set.
- `STRIPE_DEMO_ID`: the one 64-hex demo folder allowed to create Transfers. Other folders keep the demo settle.

## Render events

`RENDER_EVENTS_SECRET` is the shared secret for `POST /internal/render-events`.
Leave it unset and that path returns 404. Do not commit the value. Render's
webhook signing secret starts with `whsec_` and belongs on a Render webhook,
not in this variable. See [docs/deploy-posture.md](docs/deploy-posture.md).

## Run the app shell locally

The app shell (payments-api-simulator #122, step 2) answers `/health` (liveness),
`/ready` (storage writable), and `/metrics`. Decisions bind to the signed-in
operator — do not send `reviewer` in the JSON body.
The payout engine (step 3) gives each visitor's demo the payout tables and 40 fictional
starting payouts. Step 4 brings the Desk page at `/desk` (the root address redirects there)
and the payout routes, unchanged from the Lab.

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py                                   # http://127.0.0.1:8000/desk
.venv/bin/python -m unittest                              # tests
STORAGE_TEST_BACKEND=libsql-local .venv/bin/python -m unittest
```

To see a moved file's history from the Lab, run for example `git log --follow -- storage.py`.

## Pre-push guard (contributors)

The repo ships a git hook, [`.githooks/pre-push`](.githooks/pre-push), that stops three risky pushes before they leave your computer: a push to `main`, a force push, and deleting a remote branch. Turn it on once per clone, from the repository root:

```sh
git config core.hooksPath .githooks
```

Check it with `git config core.hooksPath` (it prints `.githooks`). `tests/test_pre_push_hook.py` runs the hook against sample pushes in a throwaway repository.
