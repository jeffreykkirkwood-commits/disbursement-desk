# Rail limit and cutoff enforcement (P2)

`rails.describe` still attaches `rail_timing` on read. Create and settle now call
`rails.enforce_send(rail, amount_cents, submitted_at=…)` so labelled caps and
same-day windows are hard gates.

## Rules

| Check | When | HTTP | Code |
|-------|------|------|------|
| Amount &gt; rail `limit_cents` | create + settle | 422 | `RAIL_OVER_LIMIT` |
| Same-day window closed | create + settle | 409 | `RAIL_WINDOW_CLOSED` |

**Same-day window:** rails with a bank cutoff and `business_days == 0`
(`SAME_DAY_ACH`, `WIRE`). Closed after the ET cutoff, or on a weekend / Fed
holiday. Next-day `ACH` may roll to the next business day, so a late ACH is
not refused. `RTP` / `FedNow` are 24/7.

`rail_timing.window_closed` mirrors the same-day check for API clients.
FedNow’s $100,000 default remains a `limit_note` only; the enforced cap is
still the $10,000,000 network maximum.
