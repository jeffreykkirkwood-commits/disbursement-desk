---
name: verify-disbursement-desk
description: "Drive the Disbursement Desk web UI and JSON APIs the way a visitor does — launch an isolated python3 app.py, mint a payments_demo cookie, prove queue/filters/decisions/reset. Use when verifying Desk UI or payout routes."
---

# Verify disbursement-desk

Project-local verification for the Disbursement Desk (Flask `create_app` + Desk HTML/JS). Primary surface is the **visitor Desk UI** at `/desk` (root redirects there): Exceptions / All queue, payout detail, Approve / Hold / Return confirm dialog, Reset demo. Secondary surfaces are `/health`, `/payouts/*` JSON, and `/payouts/stream` SSE. Data is always **synthetic** — no Dwolla credentials.

Agents read this cold mid-task. Follow Launch → Doctor → Drive → Evidence → Cleanup. Never drive an instance you did not start.

## Prerequisites

From the repo root (Flask is in `requirements.txt`; Playwright is verification-only):

```bash
python3 -m pip install -r requirements.txt playwright
python3 -m playwright install chromium   # only needed for browser drives
```

Prefer a venv when `python3 -m venv` works in the environment. System/`--user` installs are fine for a one-off proof.

## Launch

Each run picks a unique `RUN_ID` (e.g. `date +%s`-pid). State lives under `/tmp/disbursement-desk-verify/$RUN_ID/` (override with `DESK_VERIFY_STATE_ROOT`). Evidence lives under a path **outside** that tree (recommended: `/tmp/disbursement-desk-verify-artifacts/$RUN_ID/`) so cleanup cannot delete proofs.

```bash
RUN_ID="verify-$(date +%s)-$$"
HELPER=".cursor/skills/verify-disbursement-desk/scripts/verify_desk.py"
EVIDENCE="/tmp/disbursement-desk-verify-artifacts/$RUN_ID"
mkdir -p "$EVIDENCE"
python3 "$HELPER" launch --run-id "$RUN_ID"
```

What launch does:

- Starts `python3 app.py` with `PORT`, `DEMO_DIRECTORY` (empty run-local folder), and `PUBLIC_ORIGIN`.
- Binds `127.0.0.1` on a free port (or `--port`). First `GET /desk` (doctor/drive) creates the visitor demo DB and seeds payouts via `DemoStore` / `payouts.ensure_seeded` — no separate migrate step.
- Waits until `GET /health` returns `{"status":"ok",...}`.

Ready means `/health` is ok **and** `doctor` can open `/desk`, receive a `payments_demo` cookie, and see brand text plus seeded `PO-*` payout ids. A listening socket alone is not enough. The Desk list shows the current-queue subset (history / replay / recon ids stay hidden); the DB still holds the full seed (~40 rows).

Teardown: `python3 "$HELPER" cleanup --run-id "$RUN_ID"` (see Cleanup).

**Isolate:** each `RUN_ID` gets its own port, pid file, and demo directory. Do not share one instance across agents.

## Doctor

```bash
python3 "$HELPER" doctor --run-id "$RUN_ID"
```

Read-only. Passes only when: recorded pid is alive, `/health` is ok, `/desk` returns 200 with `Disbursement Desk` and `PO-` ids, and a `payments_demo` cookie is set. Run doctor first whenever anything looks off.

## Drive

Helper entry points (literal invocations):

```bash
# Public health (no cookie)
python3 "$HELPER" http --run-id "$RUN_ID" --path /health --no-cookie \
  --out "$EVIDENCE/health" --expect-status 200 --expect-body-contains '"status":"ok"'

# Root redirect
python3 "$HELPER" http --run-id "$RUN_ID" --path / --no-cookie --no-redirect \
  --out "$EVIDENCE/root-redirect" --expect-status 302

# Desk HTML (mints visitor cookie when --with-cookie)
python3 "$HELPER" http --run-id "$RUN_ID" --path /desk --with-cookie \
  --out "$EVIDENCE/desk-html" --expect-status 200 --expect-body-contains 'Disbursement Desk'

# Browser (Playwright Chromium). First navigation to /desk mints the demo cookie.
python3 "$HELPER" browser --run-id "$RUN_ID" --path /desk \
  --out "$EVIDENCE/desk-queue" --expect-filter exceptions --expect-min-rows 1

python3 "$HELPER" browser --run-id "$RUN_ID" --path /desk \
  --click-filter All --out "$EVIDENCE/desk-all" --expect-filter all --expect-min-rows 1

python3 "$HELPER" browser --run-id "$RUN_ID" --path /desk \
  --open-payout PO-1042 --expect-open-payout PO-1042 \
  --out "$EVIDENCE/desk-queue/open-po-1042"
```

Prefer stable handles from `desk.py` / `desk.js`: brand `.brand`, filter chips `role="button"` named `Exceptions` / `All` (`data-f`, `aria-pressed`), list rows `.row[data-id]`, actions `[data-act]`, confirm dialog `#confirm` / `#ok` / `#cancel`, Reset `#reset`.

Use the feature map under `features/` for journey recipes. Drive the user path; do not call internal Python setters or invent test-only endpoints.

Requires Playwright + Chromium for browser drives (see Prerequisites). HTTP-only proofs can skip browser when the feature file says so. A checked-in example proof for `desk-queue` lives under `proof/desk-queue/`.

## Evidence

Proof standards:

- Exercise the real user path (page load + filter / open payout / confirm), not only `/health`.
- Capture the action and the resulting state (ARIA snapshot + screenshot with title `Disbursement Desk · Example data` and brand `Disbursement Desk`).
- For API proofs, keep status, headers, and body; assert synthetic markers such as payout ids `PO-*`, status words, and `"mode":"fictional payments only"` on `/health`.
- Side effects: after Approve / Hold / Return or Reset demo, reload or re-fetch and confirm the new queue/detail state. Cookie possession is the visitor boundary — no login.
- Mocks: none for the Desk itself. Seed data is the product's fictional demo; do not treat it as live payments.

Artifacts for a run stay in `$EVIDENCE` (outside the state dir). Name subfolders by feature id (`desk-queue/`, `payout-decision/`, …).

## Cleanup

```bash
python3 "$HELPER" cleanup --run-id "$RUN_ID"
```

Sends SIGTERM (then SIGKILL if needed) **only** to the pid recorded for this `RUN_ID`. Removes that run's `meta.json`, `server.pid`, `server.log`, and demos folder. Does **not** delete `$EVIDENCE` or anything under `/tmp/disbursement-desk-verify-artifacts/`.

After cleanup, confirm evidence still exists at the path you named before teardown.

## Helpers

| Command | Purpose |
|---|---|
| `python3 .../verify_desk.py launch --run-id ID` | Start isolated server |
| `python3 .../verify_desk.py doctor --run-id ID` | Read-only instance check |
| `python3 .../verify_desk.py http --run-id ID --path PATH --out DIR` | HTTP GET/POST |
| `python3 .../verify_desk.py browser --run-id ID --out DIR [...]` | Playwright Desk drive |
| `python3 .../verify_desk.py cleanup --run-id ID` | Stop only this run's pid |

Script path: `.cursor/skills/verify-disbursement-desk/scripts/verify_desk.py` (executable).

## Feature map

See [`features/README.md`](features/README.md). Keep the map honest with `/maintain-verification-skill` when routes or UI handles change.
