# Reset demo

Reset demo restores this visitor's synthetic starting payouts and clears their action log, then reloads on Exceptions with no payout open. There is no confirm step.

## Sub-features

- `reset-click` posts to `/payouts/reset` from the footer link.
- `reset-restore` restores the full seed in this visitor's DB (~40 rows) and reloads the Desk queue subset (Exceptions 4 / All 10 on a fresh seed).
- `reset-error` keeps the page unchanged and shows the approved error line if reset fails.

## How to get to it (user POV)

- On `/desk`, choose **Reset demo** under the list → page reloads on Exceptions.

## Driving it with verify_desk

Preconditions:

- Instance launched for `$RUN_ID`; `doctor` passes.
- Optionally mutate one payout first so restore is observable.

- **Browser reset.** Run `python3 .cursor/skills/verify-disbursement-desk/scripts/verify_desk.py browser --run-id "$RUN_ID" --path /desk --reset-demo --out "$EVIDENCE/reset-demo/browser" --expect-filter exceptions --expect-min-rows 1`. After reload, Exceptions is pressed and seeded `PO-*` ids are present again.
- **HTTP reset.** `http --method POST --path /payouts/reset --with-cookie --out "$EVIDENCE/reset-demo/http" --expect-status 200`. Body includes `restored`.

## Gotchas

- Reset has no confirm dialog; the click is immediate.
- A POST without a cookie cannot create a session — mint via `/desk` first.
- Reset only affects this visitor's demo directory, never other runs.
- Assert restore via Desk chips / `PO-*` ids (and optional HTTP `restored`), not by counting 40 list rows — the Desk hides history/replay/recon ids.
