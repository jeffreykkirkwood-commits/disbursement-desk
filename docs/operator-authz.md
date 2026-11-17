# Operator identity and authz (P1)

Decisions and audit rows bind to a **server-minted operator**, not a free-text
`reviewer` field in the request body.

## How it works

1. Opening `/desk` (or any session-creating GET) mints `operator.json` in that
   visitor's demo folder when missing.
2. `POST /payouts/<id>/<action>` uses `g.operator.actor` for `payouts.decide`.
   Sending `reviewer` in the JSON body returns **422** with
   `code=REVIEWER_NOT_ACCEPTED`.
3. `POST /payouts/reset` requires role **admin** (`code=OPERATOR_FORBIDDEN` otherwise).
4. `GET /operator` returns the current principal (`operator_id`, `display_name`,
   `role`, `actor`).

## Roles

| Role | Decide | Reset demo |
|------|--------|------------|
| `reviewer` | yes | no |
| `admin` | yes | yes |

Demo sandboxes default to **admin** so Reset demo keeps working for the visitor
who owns that sandbox. Set `DESK_OPERATOR_ROLE=reviewer` to mint reviewers.

## Environment

| Variable | Purpose |
|----------|---------|
| `DESK_OPERATOR_ROLE` | `reviewer` or `admin` for newly minted demo operators (default `admin`) |
| `DESK_OPERATOR_NAME` | Display name for newly minted demo operators (default `Desk operator`) |
| `DESK_OPERATOR_TOKENS` | Optional JSON map of Bearer token → `{id,name,role}` for API clients |

Bearer example:

```json
{"tok-ada": {"id": "op-ada", "name": "Ada Reviewer", "role": "reviewer"}}
```

Send `Authorization: Bearer tok-ada`. Unknown tokens → **401**
`OPERATOR_UNAUTHORIZED`. The demo cookie still selects the tenant database.

## Deploy posture notes (this slice)

- **Liveness:** `GET /health` — process up; does not touch demo storage.
- **Readiness:** `GET /ready` — demo directory is writable; **503** when not.
- Put real operator tokens only in the host secret store (Render env / secret
  group). Never commit tokens.
- Stream-slot caps are shared across workers via the file backend when
  `DEMO_DIRECTORY` is shared; see [docs/stream-slots.md](stream-slots.md).

## Not in this slice

SSO/OIDC login UI is out of scope here. Design spike:
[operator-oidc-design.md](operator-oidc-design.md). OpenAPI / versioned
create-decide: see [docs/api-contract.md](api-contract.md).
