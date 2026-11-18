# FEATURE — Live smoke check of the Disbursement Desk on its Render service (Lab #24, #123).
# Walks the Desk over HTTP against a running copy of the app and prints PASS or FAIL for
# each step. It opens two brand-new visitor demos of its own, so nobody else's demo is
# read or changed. Fictional DEMO payouts and synthetic smoke payees only; no keys, no
# secrets, no Render calls. Ported from payments-api-simulator's live_smoke.py; the Lab's
# /payments, /accounts and /logs steps are not here because the Desk has no such routes.
"""Live smoke check: /health commit, the Desk page, payout JSON, new-payee rules, reset.

Usage:  python live_smoke.py --base-url URL [--expect-commit SHA]
Exit code 0 means every step passed; 1 means a step failed (the first failure stops
the run) or no URL was given. Each run opens two new visitor demos; they expire after
the demo idle time.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

COOKIE_NAME = 'payments_demo'
ET = ZoneInfo('America/New_York')
# Render's free service sleeps when idle; the first request can take about a minute.
REQUEST_TIMEOUT = 90
HEALTH_ATTEMPTS = 3

# Starting payouts every fresh demo gets (payouts.py seed), with the review reasons that
# GET /payouts/<id>/review-reasons must list for each one. PO-1036 is Sent, so no reasons.
SEED_REASONS = (
    ('PO-1042', {'AMOUNT_OVER_3X_AVG'}),
    ('PO-1043', {'BANK_CHANGED_7D'}),
    ('PO-1044', {'DUPLICATE_24H'}),
    ('PO-1036', set()),
)
MISSING_PAYOUT = 'PO-SMOKE-MISSING'

# Synthetic payees for POST /payouts/simulate. Each run uses a fresh demo, so these
# payees have no paid payout yet and the new-payee rules apply. Reset removes them.
SMOKE_PAYEE = 'Smoke Check Payee'
PAYOUT_CASES = (
    dict(name='new payee over $1,000 is flagged', status='FLAGGED', flags={'NEW_PAYEE_OVER_1000'},
         body=dict(payee_name=SMOKE_PAYEE + ' A', payee_account='DEMO-SMOKE-001',
                   amount_cents=100001)),
    dict(name='new payee at exactly $1,000 is paid', status='PAID', flags=set(),
         body=dict(payee_name=SMOKE_PAYEE + ' B', payee_account='DEMO-SMOKE-002',
                   amount_cents=100000)),
    dict(name='new payee with a same-day bank change gets both flags', status='FLAGGED',
         flags={'NEW_PAYEE_OVER_1000', 'BANK_CHANGED_7D'},
         body=dict(payee_name=SMOKE_PAYEE + ' C', payee_account='DEMO-SMOKE-003',
                   amount_cents=150000, bank_details_changed_at='NOW')),
)


class SmokeFailure(Exception):
    """One smoke step did not see what the live Desk should show."""


class Response:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = {key.lower(): value for key, value in headers.items()}
        self.body = body

    def json(self):
        try:
            return json.loads(self.body.decode('utf-8'))
        except ValueError:
            raise SmokeFailure(f'expected JSON, got HTTP {self.status}: {self.body[:120]!r}')

    @property
    def text(self):
        return self.body.decode('utf-8', 'replace')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Hand 3xx replies back as they are, so the / -> /desk redirect can be checked."""

    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


# The only code that touches the network. Tests replace it or point it at a local server.
def urllib_transport(method, url, headers, body, timeout):
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with _OPENER.open(request, timeout=timeout) as reply:
            return Response(reply.status, dict(reply.headers), reply.read())
    except urllib.error.HTTPError as error:
        return Response(error.code, dict(error.headers), error.read())


class Visitor:
    """One browser-like visitor: keeps only its own demo cookie, never prints it."""

    def __init__(self, base_url, transport):
        self.base_url, self.transport = base_url.rstrip('/'), transport
        self.cookie = None
        self.demo_id = None

    def request(self, method, path, payload=None, headers=None, keep_session=True):
        sent = {'Accept': 'application/json, text/html', 'User-Agent': 'desk-live-smoke/1'}
        sent.update(headers or {})
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            sent['Content-Type'] = 'application/json'
        if self.cookie and keep_session:
            sent['Cookie'] = self.cookie
        if self.demo_id and keep_session and method == 'POST':
            sent['X-Demo-ID'] = self.demo_id
        # Match browser POSTs: Origin is required for cookie submissions.
        if method == 'POST':
            parts = urlsplit(self.base_url)
            sent.setdefault('Origin', f'{parts.scheme}://{parts.netloc}')
        reply = self.transport(method, self.base_url + path, sent, body, REQUEST_TIMEOUT)
        if keep_session:
            set_cookie = reply.headers.get('set-cookie', '')
            if set_cookie.startswith(COOKIE_NAME + '='):
                self.cookie = set_cookie.split(';', 1)[0]
            self.demo_id = reply.headers.get('x-demo-id') or self.demo_id
        return reply


def expect(condition, message):
    if not condition:
        raise SmokeFailure(message)


# FEATURE — /health answers ok, and (with --expect-commit) runs the expected commit (#123)
# A deploy can report live while the instance still serves the old build artifact; this
# names that case in plain words instead of passing on an old build.
def check_health(visitor, expect_commit, sleep):
    last = None
    for attempt in range(HEALTH_ATTEMPTS):
        try:
            last = visitor.request('GET', '/health')
            if last.status == 200:
                break
        except OSError as error:  # timeout or connection refused while the service wakes
            last = error
        if attempt + 1 < HEALTH_ATTEMPTS:
            sleep(10)
    expect(isinstance(last, Response) and last.status == 200,
           f'GET /health did not return 200 after {HEALTH_ATTEMPTS} tries: {last}')
    health = last.json()
    expect(health.get('status') == 'ok', f'health status is {health.get("status")!r}')
    commit = (health.get('commit') or '').strip().lower()
    if expect_commit:
        wanted = expect_commit.strip().lower()
        expect(commit, f'/health reports no commit, so it cannot be checked against {wanted}. '
                       'RENDER_GIT_COMMIT is missing on the service.')
        expect(commit.startswith(wanted) or wanted.startswith(commit),
               f'STALE BUILD: the live Desk runs commit {commit}, not the expected {wanted}. '
               'Either the deploy has not finished yet, or the service is serving an old build '
               '(payments-api-simulator #123). Wait for the deploy to finish and rerun; if it '
               'still differs, the build cache may need clearing (ask Jeffrey).')
        return f'200 ok, commit {commit} matches {wanted}'
    return f'200 ok, commit {commit or "unknown"}'


def open_desk(visitor):
    reply = visitor.request('GET', '/desk')
    expect(reply.status == 200, f'GET /desk returned HTTP {reply.status}')
    expect(reply.headers.get('content-type', '').startswith('text/html'),
           f'GET /desk is {reply.headers.get("content-type")!r}, not HTML')
    expect('Disbursement Desk' in reply.text, 'GET /desk does not show "Disbursement Desk"')
    expect(visitor.cookie and visitor.demo_id, 'no demo cookie or X-Demo-ID on the Desk page')
    return reply


def review_reasons(visitor, payout_id):
    reply = visitor.request('GET', f'/payouts/{payout_id}/review-reasons')
    expect(reply.status == 200,
           f'GET /payouts/{payout_id}/review-reasons returned HTTP {reply.status}: {reply.text[:200]}')
    data = reply.json()
    expect(data.get('payout_id') == payout_id and isinstance(data.get('reasons'), list),
           f'GET /payouts/{payout_id}/review-reasons has no payout_id / reasons list: {reply.text[:200]}')
    return {item.get('code') for item in data['reasons']}


# FEATURE — The steps, in order. Only this run's own fresh demos are read or changed.
def run(base_url, expect_commit=None, transport=urllib_transport, sleep=time.sleep, now=None):
    """Yield (step, detail) per passing step; raise SmokeFailure at the first failure."""
    first = Visitor(base_url, transport)
    yield 'health', check_health(first, expect_commit, sleep)

    home = first.request('GET', '/', keep_session=False)
    location = home.headers.get('location', '')
    expect(home.status in (301, 302, 303, 307, 308) and urlsplit(location).path == '/desk',
           f'GET / returned HTTP {home.status} to {location!r}, expected a redirect to /desk')
    yield '/ redirects to /desk', f'HTTP {home.status} -> {location}'

    page = open_desk(first)
    missing = [payout_id for payout_id, _ in SEED_REASONS if payout_id not in page.text]
    expect(not missing, f'the Desk does not list the starting payouts {", ".join(missing)}')
    yield '/desk returns the Desk page with the starting payouts', \
        f'HTTP 200 HTML, {len(SEED_REASONS)} seed payouts listed'

    for payout_id, codes in SEED_REASONS:
        seen = review_reasons(first, payout_id)
        expect(seen == codes, f'{payout_id} review reasons {sorted(seen)}, expected {sorted(codes)}')
    reply = first.request('GET', f'/payouts/{MISSING_PAYOUT}/review-reasons')
    expect(reply.status == 404, f'an unknown payout returned HTTP {reply.status}, expected 404')
    yield 'review-reasons JSON matches the starting payouts', \
        '; '.join(f'{p} {",".join(sorted(c)) or "none"}' for p, c in SEED_REASONS) + '; unknown 404'

    outsider = Visitor(base_url, transport)
    reply = outsider.request('POST', '/payouts/reset')
    expect(reply.status == 409, f'POST /payouts/reset without a demo returned HTTP {reply.status}, expected 409')
    yield 'a POST without its own demo is refused', 'HTTP 409, no demo created'

    stamp = (now or datetime.now(timezone.utc)).isoformat()
    for index, case in enumerate(PAYOUT_CASES):
        body = {key: (stamp if value == 'NOW' else value) for key, value in case['body'].items()}
        reply = first.request(
            'POST', '/payouts/simulate', body,
            headers={'Idempotency-Key': f'smoke-simulate-{index}-{stamp}'})
        expect(reply.status == 201, f'simulate ({case["name"]}) returned HTTP {reply.status}: {reply.text[:200]}')
        result = reply.json()
        flags = set(filter(None, ((result.get('payout') or {}).get('flag_code') or '').split(',')))
        expect(result.get('status') == case['status'] and flags == case['flags'],
               f'{case["name"]}: got {result.get("status")} {sorted(flags)}, '
               f'expected {case["status"]} {sorted(case["flags"])}')
        yield f'payout rule: {case["name"]}', \
            f'{result["payout"]["payout_id"]} {case["status"]} {",".join(sorted(flags)) or "no flags"}'

    reply = first.request('POST', '/payouts/reset')
    expect(reply.status == 200, f'POST /payouts/reset returned HTTP {reply.status}: {reply.text[:200]}')
    restored = reply.json().get('restored')
    expect(isinstance(restored, int) and restored > 0, f'POST /payouts/reset restored {restored!r}')
    page = open_desk(first)
    expect(SMOKE_PAYEE not in page.text, 'the Desk still shows a smoke payee after Reset demo')
    yield 'Reset demo restores the starting payouts', f'restored {restored}'

    second = Visitor(base_url, transport)
    page = open_desk(second)
    expect(second.demo_id != first.demo_id, 'second visitor got the same demo')
    expect(SMOKE_PAYEE not in page.text, "a second visitor sees the first visitor's smoke payee")
    yield 'a second visitor gets its own demo', 'different demo, no smoke payees'


def main(argv=None, transport=urllib_transport, out=sys.stdout, sleep=time.sleep):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--base-url', default=os.environ.get('SMOKE_BASE_URL') or None,
                        help='Desk URL to check (or SMOKE_BASE_URL); there is no default')
    parser.add_argument('--expect-commit', default=os.environ.get('SMOKE_EXPECT_COMMIT') or None,
                        help='fail unless /health reports this commit (full or short SHA)')
    args = parser.parse_args(argv)
    if not args.base_url:
        print('FAIL  no Desk URL: pass --base-url or set SMOKE_BASE_URL', file=out)
        print('RESULT: FAIL', file=out)
        return 1
    started = datetime.now(ET).strftime('%b %d, %Y %I:%M %p ET')
    lines = [f'Live smoke check of {args.base_url} started {started}']
    print(lines[0], file=out)
    ok = True
    try:
        for step, detail in run(args.base_url, args.expect_commit, transport, sleep):
            lines.append(f'PASS  {step}: {detail}')
            print(lines[-1], file=out, flush=True)
    except (SmokeFailure, OSError) as error:  # OSError: timeout or connection failure
        ok = False
        lines.append(f'FAIL  {error}')
        print(lines[-1], file=out)
    lines.append('RESULT: PASS' if ok else 'RESULT: FAIL')
    print(lines[-1], file=out)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as handle:
            handle.write('```\n' + '\n'.join(lines) + '\n```\n')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
