# API contract (P1)

Versioned create/decide under `/v1`, OpenAPI at `/openapi.json`, and stable
`code` values on JSON errors so clients need not scrape free-text `error`.

## Endpoints

| Method | Path | Notes |
|--------|------|-------|
| `GET` | `/openapi.json` | OpenAPI 3 document; **no** demo cookie |
| `POST` | `/v1/payouts` | Create (same body as `/payouts/simulate`); requires `Idempotency-Key` |
| `POST` | `/v1/payouts/{payout_id}/decisions` | Decide; JSON `{ "action": "hold", "reason": "…" }`; no `reviewer` |

Legacy Desk UI routes (`/payouts/simulate`, `/payouts/<id>/<action>`, …) stay.

## Error envelope

```json
{ "error": "human-readable message", "code": "VALIDATION_ERROR" }
```

`code` is present on contract-covered failures. Catalog (see `api_contract.ERROR_CODES`):

| Code | Typical HTTP |
|------|----------------|
| `DEMO_SESSION_CHANGED` | 409 |
| `OPERATOR_UNAUTHORIZED` | 401 |
| `OPERATOR_FORBIDDEN` | 403 |
| `REVIEWER_NOT_ACCEPTED` | 422 |
| `CONTENT_TYPE_REQUIRED` | 415 |
| `INVALID_JSON_BODY` | 422 |
| `VALIDATION_ERROR` | 422 |
| `NOT_FOUND` | 404 |
| `CONFLICT` | 409 |
| `IDEMPOTENCY_KEY_REQUIRED` | 422 |
| `IDEMPOTENCY_KEY_REUSED` | 409 |
| `INTERNAL_ERROR` | 500 |
| `RAIL_OVER_LIMIT` | 422 |
| `RAIL_WINDOW_CLOSED` | 409 |

## Semantics

- Create: **201** first write, **200** idempotent replay.
- Decide: **201** recorded, **200** same key replay; actor is the signed-in operator.
