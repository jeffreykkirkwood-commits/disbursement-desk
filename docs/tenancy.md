# Durable multi-tenant storage (P1)

Each Desk visitor already gets a private local SQLite file under
`DEMO_DIRECTORY/<demo-id>/payments.sqlite3`. That path **is** the tenant
boundary for the default deploy.

## Turso risk (fixed)

If `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` are both set, older builds sent
**every visitor** to that one remote database (the local file was only a
session marker). That is a cross-tenant data leak.

`storage.connect()` now refuses that configuration unless tenancy is explicit.

## Modes (`TURSO_TENANT_MODE`)

| Mode | Behavior |
|------|----------|
| unset / `refuse` (default) | Turso env set → **raise** `StorageConfigError`. Unset Turso to keep per-visitor local SQLite. |
| `template` | `TURSO_DATABASE_URL` must contain `{tenant}`. Tenant id is the 64-hex demo folder name (or `tenant_id=`). Each visitor opens a different remote DB URL. |
| `shared` | One Turso DB for the whole service. Requires `TURSO_ALLOW_SHARED=1`. Use only for a deliberate **single-tenant** production deploy. |

## Tests

`test_storage.py` proves:

1. Bare Turso URL is refused (no silent share).
2. Shared mode needs `TURSO_ALLOW_SHARED=1`.
3. Template mode yields different URLs for two demo folders.

## Secrets

Store Turso URL/token only in the host secret store. Prefer a URL template
secret when running multi-visitor demos against Turso. Never commit tokens.
