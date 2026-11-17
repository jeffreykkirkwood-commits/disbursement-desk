# Desk queue

The Desk opens on Outgoing payouts with the Exceptions filter pressed, listing every seeded payout that still needs review (or is Held). Switching to All shows every payout in this visitor's demo, including Sent and Returned.

## Sub-features

- `queue-first-paint` renders the list on first paint with brand and Example data tag (no loading spinner).
- `queue-exceptions` shows only exception rows when Exceptions is pressed.
- `queue-all` shows every row when All is pressed.
- `queue-open-row` opens one payout's detail panel from a list row.

## How to get to it (user POV)

- Open `/desk` (or `/`, which redirects there) in a browser → see Exceptions queue.
- Choose the **All** chip → see the full list.
- Choose a payout row → see its detail panel.

## Driving it with verify_desk

Preconditions:

- Instance launched for `$RUN_ID`; `doctor` passes.
- Evidence dir `$EVIDENCE` exists.

- **Exceptions first paint.** Run `python3 .cursor/skills/verify-disbursement-desk/scripts/verify_desk.py browser --run-id "$RUN_ID" --path /desk --out "$EVIDENCE/desk-queue/exceptions" --expect-filter exceptions --expect-min-rows 1 --expect-brand "Disbursement Desk"`. Screenshot and ARIA show brand `Disbursement Desk`, tag `Example data · fake money`, Exceptions pressed, and at least one `.row[data-id]`.
- **All filter.** Run the same helper with `--click-filter All --out "$EVIDENCE/desk-queue/all" --expect-filter all --expect-min-rows 1`. Visible row count is greater than or equal to the Exceptions count; summary lists more `PO-*` ids.
- **Open a row.** Run with `--open-payout PO-1042 --expect-open-payout PO-1042 --out "$EVIDENCE/desk-queue/open-po-1042"`. Detail panel for `PO-1042` is visible (`aria-current="true"` on that row); summary has `detail_visible: true`.

## Gotchas

- Hash `#f=all` opens on All without clicking; prefer the chip click for the user-path proof unless testing the hash entry.
- Rows that are not exceptions start with `hidden` in the HTML; assert via `aria-pressed` and visible row count, not raw DOM length alone.
- Fresh seed shows **Exceptions 4** / **All 10** on the Desk (history, replay, and recon payout ids are hidden from `current_queue`). Do not treat "All 10" as a seed failure when the DB still has ~40 rows.
- A `/health` 200 alone does not prove the queue. Always open `/desk`.
