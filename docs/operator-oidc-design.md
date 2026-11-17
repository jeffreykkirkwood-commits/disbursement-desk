# How Desk would bind OIDC operators

This is a design for a later change. Desk does not speak OIDC today.
Current behavior is in [operator-authz.md](operator-authz.md).

The demo cookie `payments_demo` selects the tenant database.
The operator is a separate fact. Today that fact is a minted
`operator.json` in the demo folder, or an entry in `DESK_OPERATOR_TOKENS`.
OIDC would supply the same `Operator` (`operator_id`, `display_name`, `role`)
from an identity provider. Decide, reset, and the audit actor stay as they are.

## What would stay

- `Operator.can_decide` is true for `reviewer` and `admin`.
- `Operator.can_reset` is true only for `admin`.
- `POST /payouts/reset` still returns `403` `OPERATOR_FORBIDDEN` for a reviewer.
- A JSON body that contains `reviewer` still returns `422` `REVIEWER_NOT_ACCEPTED`.
- The audit actor string stays `display_name <operator_id>`.
- Old `payout_events` rows stay as written. A new login does not rewrite them.
- `payments_demo` still selects the tenant database. See [tenancy.md](tenancy.md).
- A present `Authorization: Bearer` token still replaces the session principal.
  An unknown token still returns `401` `OPERATOR_UNAUTHORIZED` and does not fall through.

## What would change

When `DESK_OIDC_ISSUER` is unset, behavior stays as it is now.
The demo mint (`DESK_OPERATOR_ROLE`, `DESK_OPERATOR_NAME`, default `admin`)
and the optional bearer map both remain.

When `DESK_OIDC_ISSUER` is set, a browser user comes from the provider.
Desk does not mint `operator.json` for that request.
Desk does not default a signed-in user to `admin`.

`operator_id` is the OIDC `sub`, trimmed.
The demo id pattern is 2 to 64 characters from `a-z`, `0-9`, `.`, `_`, and `-`.
That pattern rejects many real subjects, including uppercase UUIDs and long ids.
The OIDC path does not use that pattern and does not slug the subject.
Reject an empty `sub`, a `sub` longer than 256 characters, and a `sub`
that contains a newline, `<`, or `>`. The 256-character cap keeps the
audit actor line bounded. SQLite does not enforce it today.
`display_name` is the first non-empty claim among `name`, `email`,
`preferred_username`, and `sub`, trimmed to 80 characters.

## Browser login

Proposed routes, not built here: `GET /auth/oidc/login` and
`GET /auth/oidc/callback`.

1. A demo route has no Bearer token and no operator session.
2. If OIDC is unset, Desk mints `operator.json` as it does today.
3. If OIDC is set and the method is GET or HEAD, Desk redirects to the
   provider authorization endpoint. The flow is authorization code with
   Proof Key for Code Exchange (PKCE). Desk stores `state` and `nonce`
   until the callback.
4. The provider redirects to the callback with a code.
5. Desk exchanges the code and checks the ID token.
   The signature must match the issuer JSON Web Key Set (JWKS).
   `iss` must equal `DESK_OIDC_ISSUER`. `aud` must be the client id, or a list that contains it.
   Desk rejects an expired token. The later slice uses a maintained
   OIDC client for that check.
6. Desk maps claims to an `Operator`. If the claims match no Desk group,
   Desk returns `403` `OPERATOR_FORBIDDEN` and does not mint a demo operator.
   The group names are in Roles from claims below.
7. Desk stores the operator in a server-side session and sets a new
   `HttpOnly` cookie, `desk_operator`. The cookie value is a random session
   id. It is not the ID token, and it is not `payments_demo`.
8. `g.operator` is that session, unless a Bearer token replaced it.
9. An expired session sends the browser through login again.
   A POST with no OIDC session returns `401`. Desk does not redirect
   a POST to the provider. Desk does not mint `operator.json` on that POST.

The session record is `session_id`, `operator_id`, `display_name`, `role`,
and `expires_at`. It does not hold the ID token or the client secret.
It does not live in process memory. It does not live in `payments.sqlite3`.
That file is the tenant ledger, and Reset demo rewrites it.
Demo cleanup only deletes a 64-hex folder that contains `payments.sqlite3`.
A directory named `DEMO_DIRECTORY/_operator_sessions` is outside that rule,
as `DEMO_DIRECTORY/_stream_slots` is ([stream-slots.md](stream-slots.md)).
The free Render disk is ephemeral, so `_operator_sessions` dies on restart,
as the sandbox does.
A single-tenant production deploy that must keep sign-in across restarts
stores the same record in its durable database, still outside any
visitor `payments.sqlite3`. See [tenancy.md](tenancy.md) and
[deploy-posture.md](deploy-posture.md).

## Roles from claims

The claim name defaults to `groups` (`DESK_OIDC_GROUPS_CLAIM`).
The admin group defaults to `desk-admin`.
The reviewer group defaults to `desk-reviewer`.

| Groups claim contains | Role |
|-----------------------|------|
| Admin group | `admin` |
| Reviewer group, and not the admin group | `reviewer` |
| Neither group | No role. Login returns `403`. |

A user in both groups is `admin`, because `admin` can already do everything
`reviewer` can do. There is no third role.

## Machine clients and DESK_OPERATOR_TOKENS

`DESK_OPERATOR_TOKENS` stays the machine-client map while humans move to OIDC.
Bearer precedence does not change. A browser session never overrides a bearer
token sent on the same request.

Migration order:

1. The provider app and the two groups exist. Each human who is in the token
   map has a provider user in one of those groups.
2. Desk runs with OIDC set and the token map still set.
3. Browsers sign in through the provider. API clients keep their bearer tokens.
4. New decisions show `display_name <sub>` in `payout_events.actor`.
5. A human token leaves the map only after that person has signed in.
6. A machine token stays until that client sends an OIDC access token.
   The access token uses the same issuer, audience, and group checks as the
   ID token. That client path is part of the same later slice.
7. `DESK_OPERATOR_TOKENS` is removed when the map is empty.
   An unknown bearer still returns `401`.

## Choices rejected

Calling the static bearer map single sign-on was rejected.
The map is a shared secret. Revocation means editing the environment.
Roles do not follow provider groups.

Putting the ID token in `payments_demo` was rejected.
That cookie selects the tenant database. A token there mixes identity
with the sandbox.

Writing OIDC claims into `operator.json` was rejected.
The file is one principal for the whole demo folder.
The next person to open that demo would act as the previous operator,
or would overwrite them.

Defaulting a missing group to `admin` was rejected.
The demo mint defaults to `admin` so the visitor who owns a sandbox can
reset it. A provider user with no Desk group must not become `admin`.

## Configuration to add later

Desk does not read these variables in this change.
Secrets stay in the host secret store. Do not commit them.

| Variable | Purpose |
|----------|---------|
| `DESK_OIDC_ISSUER` | Issuer URL. Unset means today's mint and bearer map. |
| `DESK_OIDC_CLIENT_ID` | Client id. Also the expected audience. |
| `DESK_OIDC_CLIENT_SECRET` | Client secret for the code exchange. |
| `DESK_OIDC_REDIRECT_URI` | Callback URL registered at the provider. |
| `DESK_OIDC_GROUPS_CLAIM` | Claim that lists groups. Default `groups`. |
| `DESK_OIDC_ADMIN_GROUP` | Group that maps to `admin`. Default `desk-admin`. |
| `DESK_OIDC_REVIEWER_GROUP` | Group that maps to `reviewer`. Default `desk-reviewer`. |

## Out of scope

- No OIDC routes, client, or session code in this change.
- No change to the Stripe send path.
- No SCIM provisioning.
- No payout-level permissions beyond `reviewer` and `admin`.
- No change to how `payments_demo` selects a tenant.
- The login screen is the provider's. Desk only redirects.
