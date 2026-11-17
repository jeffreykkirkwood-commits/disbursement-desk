# Health and redirect

`/health` (liveness) and `/ready` (demo storage writable) answer without creating a visitor demo. Visiting `/` redirects to `/desk`, which is the Desk entry point for this repo.

## Sub-features

- `health-ok` returns JSON `status: ok` and fictional mode.
- `root-redirect` sends browsers from `/` to `/desk`.
- `desk-entry` serves the Desk HTML at `/desk`.

## How to get to it (user POV)

- Open `/health` → see a small JSON status.
- Open `/` → land on `/desk`.

## Driving it with verify_desk

Preconditions:

- Instance launched for `$RUN_ID`; `doctor` passes.
- Evidence dir `$EVIDENCE` exists.

- **Health.** Run `python3 .cursor/skills/verify-disbursement-desk/scripts/verify_desk.py http --run-id "$RUN_ID" --path /health --no-cookie --out "$EVIDENCE/health-and-redirect/health" --expect-status 200 --expect-body-contains '"status":"ok"'`. Body also includes `fictional payments only`.
- **Root redirect.** Run `--path / --no-cookie --no-redirect --out "$EVIDENCE/health-and-redirect/root" --expect-status 302`. `Location` ends with `/desk`.
- **Desk.** Run `--path /desk --with-cookie --out "$EVIDENCE/health-and-redirect/desk" --expect-status 200 --expect-body-contains 'Disbursement Desk'`.

## Gotchas

- `/health` must not create a demo folder; doctor already proves `/desk` seeding separately.
- Follow-redirects hide the 302 — use `--no-redirect` when proving the redirect itself.
