# Desk audit (Activity panel)

Operators see **who / what / when** for each payout’s Desk status transitions on
the detail panel under **Activity**.

## Source

Rows come from `payout_events` via `payouts.list_events` (oldest first). Create,
route, decide (approve / hold / return / clear / block), and sanctions `screen`
rows all appear. Status codes are shown with the same Desk words as the queue
(`Needs review`, `Held`, `Sent`, `Returned`).

## Empty state

A fresh seed payout with no decisions yet shows “No activity yet.”

## Not in this panel

Execution / reconciliation logs stay on their own APIs. Safe create UI with
client idempotency keys is a separate Desk UX item.
