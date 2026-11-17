# Live OFAC vendor path (P2)

Desk screening stays behind the same **Clear / Block** UX. `ofac.screen(name)`
is the only call site for create, decide re-screen, and settle.

## Providers

| `OFAC_PROVIDER` | Behavior |
|-----------------|----------|
| `local` (default) | Dated fixture `fixtures/ofac_sdn_snapshot.json` (unchanged #111 path). |
| `http` | POST JSON `{"name": "…"}` to `OFAC_HTTP_URL`. Response must be JSON `null` (no match) or a match object with the same fields the local provider returns (`uid`, `entry_name`, `entry_type`, `matched_name`, `score` 0–100, `list_published`, `snapshot_taken`). |

## Fail-closed

If the HTTP provider is selected and the request fails (timeout, non-2xx, bad
JSON, wrong shape), `screen` returns a **provider-error match** (`uid`
`vendor-unavailable`, score 100). That holds the payout at `FLAGGED` with
`POSSIBLE_SANCTIONS_MATCH` so settle cannot skip OFAC when the vendor is down.
An operator must still **Clear** (or **Block**) before send.

## Env

- `OFAC_PROVIDER` — `local` or `http`
- `OFAC_HTTP_URL` — required when provider is `http`
- `OFAC_HTTP_TIMEOUT_SECONDS` — default `5`

No API keys in the repo; put vendor credentials in the host secret store and
pass them via the vendor URL or reverse-proxy, not committed files.
