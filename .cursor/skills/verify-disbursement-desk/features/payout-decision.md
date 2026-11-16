# Payout decision

A reviewer opens a Needs review payout, chooses Approve, Hold, or Return, confirms in the dialog, and the page reloads showing the saved status. Possible sanctions matches use Clear / Block instead.

## Sub-features

- `decision-open` opens the detail panel with action buttons.
- `decision-confirm` shows the confirm dialog with payee, amount, and payout id.
- `decision-save` POSTs to `/payouts/<id>/<action>` with an Idempotency-Key and reloads.
- `decision-cancel` closes the dialog without changing status.

## How to get to it (user POV)

- From Exceptions, open a Needs review payout → choose **Approve**, **Hold**, or **Return** → confirm in the dialog.
- Choose **Cancel** on the dialog → stay on the same payout with no change.

## Driving it with verify_desk

Preconditions:

- Instance launched for `$RUN_ID`; `doctor` passes.
- Evidence dir `$EVIDENCE` exists.
- Target payout is still actionable (e.g. `PO-1042` on a fresh seed).

- **Open and confirm Hold.** Run `python3 .cursor/skills/verify-disbursement-desk/scripts/verify_desk.py browser --run-id "$RUN_ID" --path /desk --open-payout PO-1042 --click-action hold --confirm --out "$EVIDENCE/payout-decision/hold-po-1042"`. After reload, the row for `PO-1042` shows status word **Held**.
- **HTTP decide.** Mint a cookie with `--with-cookie`, then `http --method POST --path /payouts/PO-1043/hold --json-body '{"reviewer":"Verify Reviewer"}' --idempotency-key verify-hold-1 --expect-status 201`. Re-fetch `/desk` and confirm Held.

## Gotchas

- POST decisions require an existing visitor cookie; a POST never silently creates a demo. Always hit `/desk` first (`--with-cookie`).
- Same Idempotency-Key returns 200 on replay without a second status change.
- Final statuses (Sent / Returned) show no action buttons; do not treat a missing button as a harness failure when the payout is final.
- Sanctions match payouts expose Clear / Block (`data-act`), not Approve / Hold / Return.
