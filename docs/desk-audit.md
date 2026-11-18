# Desk audit (Activity panel)

Operators see **who / what / when** for each payout’s Desk status and execution
transitions on the detail panel under **Activity**.

## Source

Rows come from `payouts.audit_entries` (oldest first), which merges:

- Desk status moves from `payout_events` via `payouts.list_events` — create,
  route, decide (approve / hold / return / clear / block), and sanctions
  `screen`
- Payment-outcome moves from `execution_events` via
  `payouts.list_execution_events` — submitted / settled / failed, with the
  stored source (demo, replay, reconciliation, stripe)

Status codes use the same Desk words as the queue (`Needs review`, `Held`,
`Sent`, `Returned`). Execution rows say **Execution · Settled** (etc.) and
name the source as the actor line. The panel never invents a transition that
is not stored. Desk HTML shows who/what/when only — it does **not** render
`execution_ref` or decide `evidence` (same rule as the decide JSON vs Desk
split).

## JSON API

`GET /payouts/<payout_id>/events` returns `{payout_id, events}` for the same
trail (same operator/session gate as `/desk`). Missing payout → **404**
`NOT_FOUND`. Evidence and idempotency keys are not included. Execution rows
may include `execution_ref` here for operators querying the API.

## Empty state

A fresh seed payout with no decisions or execution rows yet shows “No activity
yet.”

## Not in this panel

Reconciliation run summaries stay on `POST /reconciliation` / demo scenario
snapshots. They are not duplicated here.

## Create payout (Desk UI)

Queue header **Create payout** opens a dialog that POSTs `/v1/payouts` (same
create handler as `/payouts/simulate`) with a client `Idempotency-Key` and a
minted `payout_id` when the dialog opens. Retries reuse both; Cancel / close
discards them. A successful create reloads the All list so the new payout is on screen.
See `desk.js` (`pendingCreate`).

A failed create keeps the dialog open. The page reads only the stable `code`
and shows a fixed sentence for that code, including the code in parentheses.
It does not copy the response `error` text. Missing fields show a local
sentence and do not send. While the request is in flight, **Create** and
**Cancel** are disabled and the button reads **Creating…**.

## Queue mark and detail callouts

A possible sanctions match that is not final gets a red **OFAC** chip beside
the status pill. Needs review and Held keep their existing pills.

The detail panel adds one callout. A sanctions match says Approve is
unavailable and names Clear and Block. Held says the payout will not send
until someone approves it. Other in-review payouts say to read the reasons
first. Sent and Returned payouts have no callout. Hold stays disabled when
the payout is already Held, with a title that says so. While a decision is
saving, **Confirm** and **Cancel** are disabled, Escape does not close the
dialog, and the button reads **Saving…**.
