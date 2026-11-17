# Desk safe create (New payout)

Operators can create a synthetic payout from the Desk **New payout** panel without
leaving the queue. This closes the remaining P2 Desk UX item next to Activity
([desk-audit.md](desk-audit.md)): create is no longer API-only.

## Idempotency

`desk.js` mints one `Idempotency-Key` per create attempt (`desk-` + UUID) and
**reuses that key** while the request is in flight or retrying after a failure.
`POST /payouts/simulate` (same contract as `POST /v1/payouts`) requires the header:

| Result | Meaning |
|--------|---------|
| **201** | First create for that key; automation routed the payout |
| **200** | Same key + same body replayed; no second disbursement |
| **409** | Same key used for a different create fingerprint |
| **422** | Missing/invalid key or body |

A double-click or network retry therefore cannot insert two rows for one operator
action. Server create/route still runs mandatory pre-send gates
([pre-send-gates.md](pre-send-gates.md)).

## Fields

| Field | Required | Notes |
|-------|----------|-------|
| Payee name | yes | 1–80 characters |
| DEMO- account | yes | Must start with `DEMO-` |
| Amount (USD) | yes | Converted to cents in the browser |
| Rail | yes | ACH (default), SAME_DAY_ACH, RTP, FEDNOW, WIRE |
| Payout id | no | Server assigns `PO-{n}` when blank |

## After create

The page reloads on **All** with the new payout open (`#f=all&id=…`). Flagged
creates land in Exceptions as usual; clean creates may already be Sent after
automation.
