#!/usr/bin/env python3
"""Verification helper for disbursement-desk. Drive the real Flask app the way a visitor does.

Verification scaffolding only: starts an isolated python3 app.py with its own DEMO_DIRECTORY,
waits for /health, and performs doctor / HTTP / Playwright drives. Never kill by process name;
only the PID recorded for this RUN_ID.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# scripts/ → verify-disbursement-desk/ → skills/ → .cursor/ → repo root
ROOT = Path(__file__).resolve().parents[4]
STATE_ROOT = Path(os.environ.get('DESK_VERIFY_STATE_ROOT', '/tmp/disbursement-desk-verify'))


def run_dir(run_id: str) -> Path:
    return STATE_ROOT / run_id


def meta_path(run_id: str) -> Path:
    return run_dir(run_id) / 'meta.json'


def pid_path(run_id: str) -> Path:
    return run_dir(run_id) / 'server.pid'


def demos_path(run_id: str) -> Path:
    return run_dir(run_id) / 'demos'


def load_meta(run_id: str) -> dict:
    path = meta_path(run_id)
    if not path.is_file():
        raise SystemExit(f'no meta for run {run_id}: expected {path}')
    return json.loads(path.read_text())


def free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def wait_ready(base: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + '/health', timeout=1) as r:
                body = json.loads(r.read().decode())
                if r.status == 200 and body.get('status') == 'ok':
                    return
                last = body
        except Exception as e:  # noqa: BLE001
            last = str(e)
        time.sleep(0.1)
    raise SystemExit(f'server not ready at {base}/health: {last}')


def cmd_launch(args: argparse.Namespace) -> None:
    run_id = args.run_id
    d = run_dir(run_id)
    demos = demos_path(run_id)
    d.mkdir(parents=True, exist_ok=True)
    demos.mkdir(parents=True, exist_ok=True)
    if pid_path(run_id).is_file():
        raise SystemExit(f'run {run_id} already has a pid file; cleanup first or pick a new RUN_ID')
    port = args.port or free_port()
    base = f'http://127.0.0.1:{port}'
    log = d / 'server.log'
    env = os.environ.copy()
    env.update({
        'PORT': str(port),
        'DEMO_DIRECTORY': str(demos),
        'PUBLIC_ORIGIN': base,
    })
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / 'app.py')],
        cwd=str(ROOT),
        env=env,
        stdout=log.open('w'),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    pid_path(run_id).write_text(str(proc.pid))
    try:
        wait_ready(base)
    except SystemExit:
        cmd_cleanup(argparse.Namespace(run_id=run_id))
        raise
    meta = {
        'run_id': run_id,
        'pid': proc.pid,
        'port': port,
        'base_url': base,
        'demo_directory': str(demos),
        'root': str(ROOT),
        'started_at': time.time(),
    }
    meta_path(run_id).write_text(json.dumps(meta, indent=2) + '\n')
    print(json.dumps({'ok': True, **meta}, indent=2))


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def cmd_doctor(args: argparse.Namespace) -> None:
    meta = load_meta(args.run_id)
    pid = int(meta['pid'])
    base = meta['base_url']
    problems = []
    if not alive(pid):
        problems.append(f'pid {pid} is not running')
    try:
        with urllib.request.urlopen(base + '/health', timeout=3) as r:
            health = json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        problems.append(f'/health failed: {e}')
        health = None
    if health is not None and health.get('status') != 'ok':
        problems.append(f'/health status={health!r}')
    # Usable: /desk seeds a visitor demo and returns HTML with brand + payout rows.
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        with opener.open(base + '/desk', timeout=10) as r:
            desk_status = r.status
            desk_body = r.read().decode(errors='replace')
    except Exception as e:  # noqa: BLE001
        problems.append(f'/desk failed: {e}')
        desk_status = None
        desk_body = ''
    if desk_status != 200:
        problems.append(f'/desk status {desk_status}')
    if 'Disbursement Desk' not in desk_body:
        problems.append('/desk missing brand text')
    if 'PO-' not in desk_body:
        problems.append('/desk missing seeded payout ids')
    if not any(c.name == 'payments_demo' for c in jar):
        problems.append('payments_demo cookie not set after /desk')
    out = {
        'ok': not problems,
        'run_id': args.run_id,
        'pid': pid,
        'pid_alive': alive(pid),
        'base_url': base,
        'health': health,
        'desk_status': desk_status,
        'problems': problems,
    }
    print(json.dumps(out, indent=2))
    if problems:
        raise SystemExit(1)


def http_exchange(base: str, method: str, path: str, *, cookie: str | None = None,
                  body: bytes | None = None, headers: dict | None = None,
                  follow_redirects: bool = True) -> tuple[int, dict, bytes]:
    hdrs = dict(headers or {})
    if cookie:
        hdrs['Cookie'] = cookie
    req = urllib.request.Request(base + path, data=body, headers=hdrs, method=method)
    opener = urllib.request.build_opener() if follow_redirects else urllib.request.build_opener(
        urllib.request.HTTPHandler(),
        urllib.request.HTTPSHandler(),
    )
    if not follow_redirects:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
                return None
        opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=15) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def cookie_from_set_cookie(headers: dict) -> str | None:
    # urllib folds Set-Cookie oddly; prefer CookieJar via a GET /desk when needed.
    raw = headers.get('Set-Cookie') or headers.get('set-cookie')
    if not raw:
        return None
    # Keep only name=value for payments_demo
    for part in raw.split(','):
        if 'payments_demo=' in part:
            token = part.split('payments_demo=', 1)[1].split(';', 1)[0].strip()
            return f'payments_demo={token}'
    return None


def mint_visitor_cookie(base: str) -> str:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    with opener.open(base + '/desk', timeout=10) as r:
        r.read()
    for c in jar:
        if c.name == 'payments_demo':
            return f'{c.name}={c.value}'
    raise SystemExit('failed to mint payments_demo cookie from /desk')


def cmd_http(args: argparse.Namespace) -> None:
    meta = load_meta(args.run_id)
    cookie = None
    if args.with_cookie or (not args.no_cookie and args.method.upper() != 'GET'):
        cookie = mint_visitor_cookie(meta['base_url'])
    elif not args.no_cookie and args.cookie:
        cookie = args.cookie
    body = None
    headers = {}
    if args.json_body is not None:
        body = args.json_body.encode()
        headers['Content-Type'] = 'application/json'
    if args.idempotency_key:
        headers['Idempotency-Key'] = args.idempotency_key
    if args.origin:
        headers['Origin'] = args.origin
    elif cookie and args.method.upper() == 'POST':
        headers['Origin'] = meta['base_url']
    status, resp_headers, raw = http_exchange(
        meta['base_url'], args.method.upper(), args.path,
        cookie=cookie, body=body, headers=headers,
        follow_redirects=not args.no_redirect,
    )
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / 'status.txt').write_text(str(status) + '\n')
    (out_dir / 'headers.json').write_text(json.dumps(dict(resp_headers), indent=2) + '\n')
    (out_dir / 'body.bin').write_bytes(raw)
    try:
        (out_dir / 'body.txt').write_text(raw.decode())
    except UnicodeDecodeError:
        (out_dir / 'body.txt').write_text(f'<binary {len(raw)} bytes>\n')
    if cookie:
        (out_dir / 'cookie.txt').write_text(cookie + '\n')
    print(json.dumps({'ok': True, 'status': status, 'bytes': len(raw), 'out': str(out_dir)}, indent=2))
    if args.expect_status is not None and status != args.expect_status:
        raise SystemExit(f'expected status {args.expect_status}, got {status}')
    if args.expect_body_contains and args.expect_body_contains not in raw.decode(errors='replace'):
        raise SystemExit(f'expected body to contain {args.expect_body_contains!r}')


def cmd_browser(args: argparse.Namespace) -> None:
    meta = load_meta(args.run_id)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise SystemExit('playwright is required for browser drives: pip install playwright') from e

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width': 1280, 'height': 900})
        page = context.new_page()
        page.goto(meta['base_url'] + args.path, wait_until='networkidle', timeout=30_000)
        if args.click_filter:
            page.get_by_role('button', name=args.click_filter).click()
            page.wait_for_timeout(300)
        if args.open_payout:
            page.locator(f'.row[data-id="{args.open_payout}"]').click()
            page.wait_for_timeout(300)
        if args.click_action:
            page.locator(f'[data-act="{args.click_action}"]').click()
            page.wait_for_timeout(300)
        if args.confirm:
            page.locator('#ok').click()
            page.wait_for_load_state('networkidle')
        if args.reset_demo:
            page.locator('#reset').click()
            page.wait_for_load_state('networkidle')
        aria = page.locator('body').aria_snapshot()
        (out_dir / 'aria.txt').write_text(aria if isinstance(aria, str) else str(aria))
        page.screenshot(path=str(out_dir / 'screenshot.png'), full_page=True)
        title = page.title()
        # .brand includes an aria-hidden "D" mark; normalize whitespace for asserts.
        brand_raw = page.locator('.brand').inner_text() if page.locator('.brand').count() else ''
        brand = ' '.join(brand_raw.split())
        tag = page.locator('.tag').inner_text() if page.locator('.tag').count() else ''
        exceptions_pressed = page.locator('.chip[data-f="exceptions"]').get_attribute('aria-pressed')
        all_pressed = page.locator('.chip[data-f="all"]').get_attribute('aria-pressed')
        visible_rows = page.locator('#list li[data-ex]:not([hidden]) .row').count()
        payout_ids = page.locator('#list .row').evaluate_all(
            'els => els.map(e => e.getAttribute("data-id"))')
        open_id = args.open_payout
        open_current = None
        detail_visible = None
        if open_id:
            open_current = page.locator(f'.row[data-id="{open_id}"]').get_attribute('aria-current')
            detail_visible = page.locator(f'.panel[data-id="{open_id}"]:not([hidden])').count() > 0
        browser.close()
    summary = {
        'ok': True,
        'title': title,
        'brand': brand,
        'tag': tag.strip(),
        'exceptions_pressed': exceptions_pressed,
        'all_pressed': all_pressed,
        'visible_rows': visible_rows,
        'payout_ids_sample': payout_ids[:5],
        'payout_id_count': len(payout_ids),
        'open_payout': open_id,
        'open_aria_current': open_current,
        'detail_visible': detail_visible,
        'out': str(out_dir),
    }
    (out_dir / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))
    if args.expect_title_contains and args.expect_title_contains not in title:
        raise SystemExit(f'expected title to contain {args.expect_title_contains!r}, got {title!r}')
    if args.expect_brand and args.expect_brand not in brand:
        raise SystemExit(f'expected brand {args.expect_brand!r}, got {brand!r}')
    if args.expect_min_rows is not None and visible_rows < args.expect_min_rows:
        raise SystemExit(f'expected at least {args.expect_min_rows} visible rows, got {visible_rows}')
    if args.expect_filter == 'exceptions' and exceptions_pressed != 'true':
        raise SystemExit('expected Exceptions filter pressed')
    if args.expect_filter == 'all' and all_pressed != 'true':
        raise SystemExit('expected All filter pressed')
    if args.expect_open_payout:
        want = args.expect_open_payout
        if open_current != 'true':
            raise SystemExit(f'expected aria-current=true on {want}, got {open_current!r}')
        if not detail_visible:
            raise SystemExit(f'expected visible detail panel for {want}')


def cmd_cleanup(args: argparse.Namespace) -> None:
    run_id = args.run_id
    d = run_dir(run_id)
    pid_file = pid_path(run_id)
    if pid_file.is_file():
        pid = int(pid_file.read_text().strip())
        if alive(pid):
            os.kill(pid, signal.SIGTERM)
            for _ in range(50):
                if not alive(pid):
                    break
                time.sleep(0.1)
            if alive(pid):
                os.kill(pid, signal.SIGKILL)
        pid_file.unlink(missing_ok=True)
    for name in ('meta.json', 'server.log', 'server.pid'):
        (d / name).unlink(missing_ok=True)
    # Remove this run's demo directory tree (scratch only).
    demos = demos_path(run_id)
    if demos.is_dir():
        import shutil
        shutil.rmtree(demos, ignore_errors=True)
    if d.is_dir() and not any(d.iterdir()):
        d.rmdir()
    print(json.dumps({'ok': True, 'run_id': run_id, 'cleaned': True}, indent=2))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog='verify_desk.py')
    sub = p.add_subparsers(dest='cmd', required=True)

    launch = sub.add_parser('launch', help='start an isolated Desk instance')
    launch.add_argument('--run-id', required=True)
    launch.add_argument('--port', type=int, default=0)
    launch.set_defaults(func=cmd_launch)

    doctor = sub.add_parser('doctor', help='read-only health of a launched instance')
    doctor.add_argument('--run-id', required=True)
    doctor.set_defaults(func=cmd_doctor)

    http = sub.add_parser('http', help='HTTP request against the instance')
    http.add_argument('--run-id', required=True)
    http.add_argument('--path', required=True)
    http.add_argument('--out', required=True)
    http.add_argument('--method', default='GET')
    http.add_argument('--expect-status', type=int, default=None)
    http.add_argument('--expect-body-contains', default=None)
    http.add_argument('--no-cookie', action='store_true')
    http.add_argument('--with-cookie', action='store_true',
                      help='mint a visitor cookie via GET /desk first')
    http.add_argument('--cookie', default=None)
    http.add_argument('--json-body', default=None)
    http.add_argument('--idempotency-key', default=None)
    http.add_argument('--origin', default=None)
    http.add_argument('--no-redirect', action='store_true')
    http.set_defaults(func=cmd_http)

    browser = sub.add_parser('browser', help='Playwright drive of the Desk UI')
    browser.add_argument('--run-id', required=True)
    browser.add_argument('--path', default='/desk')
    browser.add_argument('--out', required=True)
    browser.add_argument('--click-filter', default=None,
                         help='filter chip accessible name prefix, e.g. Exceptions or All')
    browser.add_argument('--open-payout', default=None, help='payout id to open, e.g. PO-1042')
    browser.add_argument('--click-action', default=None, help='data-act value: approve|hold|return|…')
    browser.add_argument('--confirm', action='store_true')
    browser.add_argument('--reset-demo', action='store_true')
    browser.add_argument('--expect-title-contains', default='Disbursement Desk')
    browser.add_argument('--expect-brand', default='Disbursement Desk')
    browser.add_argument('--expect-min-rows', type=int, default=None)
    browser.add_argument('--expect-filter', choices=['exceptions', 'all'], default=None)
    browser.add_argument('--expect-open-payout', default=None,
                         help='require this payout id open (aria-current + visible panel)')
    browser.set_defaults(func=cmd_browser)

    cleanup = sub.add_parser('cleanup', help='stop the instance started for this RUN_ID')
    cleanup.add_argument('--run-id', required=True)
    cleanup.set_defaults(func=cmd_cleanup)
    return p


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == '__main__':
    main()
