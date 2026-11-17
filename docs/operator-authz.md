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

## OIDC sign-in

Leave `DESK_OIDC_ISSUER` unset and the mint above is unchanged.

When it is set, Desk does not write `operator.json` for that process.
A browser GET or HEAD with no `desk_operator` session redirects to
`GET /auth/oidc/login`. A POST with no session returns **401**
`OPERATOR_UNAUTHORIZED`. Desk does not redirect a POST, and it does not
mint a demo operator on that POST.

A Bearer token still replaces the session. An unknown Bearer token still
returns **401** and does not fall through to the cookie.

The admin group defaults to `desk-admin`. The reviewer group defaults to
`desk-reviewer`. An account in neither group gets **403**
`OPERATOR_FORBIDDEN`. It is not given `admin`.

The session file lives in `DEMO_DIRECTORY/_operator_sessions`.
It is not a row in `payments.sqlite3`. The `desk_operator` cookie is a
random session id. It is not the `payments_demo` cookie.

`DESK_OIDC_MOCK=1` is the in-process issuer used by tests. The process
refuses to start when that flag is set together with `RENDER`.

The variable list, the `HS256` check, and the choices Desk rejected are in
[How Desk binds OIDC operators](operator-oidc-design.md).

## Deploy posture notes (this slice)

- **Liveness:** `GET /health` — process up; does not touch demo storage.
- **Readiness:** `GET /ready` — demo directory is writable; **503** when not.
- Put real operator tokens only in the host secret store (Render env / secret
  group). Never commit tokens.
- Stream-slot caps are shared across workers via the file backend when
  `DEMO_DIRECTORY` is shared; see [docs/stream-slots.md](stream-slots.md).

## Not in this slice

See the versioned create and decide contract in
[docs/api-contract.md](api-contract.md).
