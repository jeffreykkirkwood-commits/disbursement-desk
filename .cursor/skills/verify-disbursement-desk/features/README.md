# Disbursement Desk verification map

This directory is the maintained source for verifying user-facing behavior of disbursement-desk. Read this index before driving the app, then use the matching feature file as the recipe.

## Baseline preconditions

- Launch with `python3 .cursor/skills/verify-disbursement-desk/scripts/verify_desk.py launch --run-id "$RUN_ID"`.
- Put evidence under `/tmp/disbursement-desk-verify-artifacts/$RUN_ID/` (or another path outside the state dir).
- Run `doctor` and require `ok: true`, brand `Disbursement Desk`, and seeded `PO-*` payouts.
- Never drive an instance that was not started by this verification run.
- Data is always fictional; proofs use the seed in `payouts.py`.

## Driving conventions

- Start every recipe from a freshly launched instance unless its preconditions say otherwise.
- Prefer ARIA roles / accessible names and `data-*` handles over CSS position.
- Treat helper commands as literal; keep quoted paths and flags unchanged.
- HTTP proofs use `verify_desk.py http`; browser proofs use `verify_desk.py browser`.
- Cleanup stops the run's pid only; keep proof artifacts.

## Proof and skip reporting

- Capture the user action and the resulting state, not only the final screen.
- UI proof includes an ARIA snapshot and a screenshot with `Disbursement Desk` and the Example data tag visible.
- HTTP proof includes status, headers, and body on disk.
- Record the feature ID and entry point with every artifact folder.
- Report an unreachable path with the unmet precondition; do not claim it verified via another path.

## Feature entry contract

Each feature file starts with an H1 and one paragraph of user-visible behavior, then exactly these H2s: `Sub-features`, `How to get to it (user POV)`, `Driving it with verify_desk`, `Gotchas`.

## Features

- [Desk queue](./desk-queue.md) — Exceptions / All filter and seeded payout list on first paint.
- [Payout decision](./payout-decision.md) — open a payout, confirm Approve / Hold / Return, see saved status after reload.
- [Reset demo](./reset-demo.md) — Reset demo restores this visitor's synthetic seed.
- [Health and redirect](./health-and-redirect.md) — `/health` is public; `/` redirects to `/desk`.
- [Stream cap](./stream-cap.md) — at most two live `/payouts/stream` connections; a third gets 503.
