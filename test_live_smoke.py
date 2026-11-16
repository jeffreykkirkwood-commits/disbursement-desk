# Automated checks for the Desk live smoke script (live_smoke.py, Lab #24 and #123). Each
# test starts its own HTTP server on 127.0.0.1 with a random port, so the real urllib
# transport is used, no outside network call is made and no Render service is touched.
# A tiny stub Desk covers the routes now; once the Desk routes are on main (#122 step 4)
# the same run also goes against the real app with a temporary fictional demo directory.
"""Run live_smoke.py against local HTTP servers: a stub Desk and, when present, the app."""
import io
import json
import tempfile
import threading
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import live_smoke

SEED = {'PO-1036': [], 'PO-1042': ['AMOUNT_OVER_3X_AVG'], 'PO-1043': ['BANK_CHANGED_7D'],
        'PO-1044': ['DUPLICATE_24H']}


# FEATURE 00 — Test support — a stub Desk that answers like hosted_app.py
# One fictional payout list per demo cookie. `faults` lets a test break one behaviour.
class StubDesk:
    def __init__(self, commit='abc1234def', faults=()):
        self.commit, self.faults = commit, set(faults)
        self.demos = {}
        self.sent = []

    def handler(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, body=b'', headers=None, content_type='application/json'):
                if isinstance(body, (dict, list)):
                    body = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def demo(self, create):
                cookie = self.headers.get('Cookie', '')
                token = cookie.split('=', 1)[1] if cookie.startswith('payments_demo=') else None
                if token in stub.demos:
                    return token, {}
                if not create:
                    return None, {}
                token = f'demo{len(stub.demos) + 1:060d}'
                stub.demos[token] = []
                return token, {'Set-Cookie': f'payments_demo={token}; Path=/; HttpOnly',
                               'X-Demo-ID': token}

            def do_GET(self):
                path = urlsplit(self.path).path
                stub.sent.append(('GET', path, {k.lower(): v for k, v in self.headers.items()}))
                if path == '/health':
                    if 'health_down' in stub.faults:
                        return self.reply(503, {'status': 'starting'})
                    return self.reply(200, {'status': 'ok', 'commit': stub.commit})
                if path == '/':
                    if 'no_redirect' in stub.faults:
                        return self.reply(200, b'<html>home</html>', content_type='text/html')
                    return self.reply(302, b'', {'Location': '/desk'}, 'text/html')
                token, headers = self.demo(create=True)
                headers.setdefault('X-Demo-ID', token)
                if path == '/desk':
                    rows = ''.join(f'<li>{p}</li>' for p in SEED)
                    rows += ''.join(f'<li>{name}</li>' for name in stub.demos[token])
                    page = f'<title>Disbursement Desk</title><ul>{rows}</ul>'.encode()
                    return self.reply(200, page, headers, 'text/html; charset=utf-8')
                parts = path.split('/')
                if len(parts) == 4 and parts[1] == 'payouts' and parts[3] == 'review-reasons':
                    if parts[2] not in SEED:
                        return self.reply(404, {'error': 'Payout not found.'}, headers)
                    codes = SEED[parts[2]]
                    if 'wrong_reasons' in stub.faults and parts[2] == 'PO-1042':
                        codes = []
                    return self.reply(200, {'payout_id': parts[2], 'reasons': [
                        {'code': code, 'label': code, 'details': {}} for code in codes]}, headers)
                return self.reply(404, {'error': 'Not found'})

            def do_POST(self):
                path = urlsplit(self.path).path
                length = int(self.headers.get('Content-Length') or 0)
                body = json.loads(self.rfile.read(length) or b'null')
                stub.sent.append(('POST', path, {k.lower(): v for k, v in self.headers.items()}))
                token, _ = self.demo(create=False)
                if not token or self.headers.get('X-Demo-ID') not in (None, token):
                    return self.reply(409, {'error': 'Demo session missing.',
                                            'code': 'DEMO_SESSION_CHANGED'})
                if path == '/payouts/simulate':
                    stub.demos[token].append(body['payee_name'])
                    flags = []
                    if body['amount_cents'] > 100000:
                        flags.append('NEW_PAYEE_OVER_1000')
                    if body.get('bank_details_changed_at'):
                        flags.append('BANK_CHANGED_7D')
                    payout_id = f'PO-{1045 + len(stub.demos[token])}'
                    return self.reply(201, {'status': 'FLAGGED' if flags else 'PAID', 'changed': True,
                                            'event': None, 'payout': {
                                                'payout_id': payout_id,
                                                'flag_code': ','.join(flags) or None}})
                if path == '/payouts/reset':
                    if 'reset_keeps_smoke' not in stub.faults:
                        stub.demos[token] = []
                    return self.reply(200, {'restored': 40})
                return self.reply(404, {'error': 'Not found'})

        return Handler


def serve(handler):
    """Start an HTTP server on a free loopback port; return (base_url, stop)."""
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def stop():
        server.shutdown()
        server.server_close()
        thread.join(5)
    return f'http://127.0.0.1:{server.server_address[1]}', stop


def run_smoke(base_url, *extra, sleep=lambda seconds: None):
    out = io.StringIO()
    code = live_smoke.main(['--base-url', base_url, *extra], out=out, sleep=sleep)
    return code, out.getvalue()


STEPS = ('health', '/ redirects to /desk', '/desk returns the Desk page',
         'review-reasons JSON matches', 'a POST without its own demo is refused',
         'payout rule: new payee over $1,000 is flagged',
         'payout rule: new payee at exactly $1,000 is paid',
         'payout rule: new payee with a same-day bank change gets both flags',
         'Reset demo restores', 'a second visitor gets its own demo')


class LiveSmokeAgainstStubDeskTests(unittest.TestCase):
    def start(self, **options):
        self.stub = StubDesk(**options)
        base_url, stop = serve(self.stub.handler())
        self.addCleanup(stop)
        return base_url

    def test_every_step_passes_on_a_healthy_desk(self):
        code, text = run_smoke(self.start(), '--expect-commit', 'abc1234')
        self.assertEqual(code, 0, text)
        self.assertIn('RESULT: PASS', text)
        self.assertIn('commit abc1234def matches abc1234', text)
        for step in STEPS:
            self.assertIn('PASS  ' + step, text)

    def test_stale_build_fails_loudly_with_both_commits(self):
        code, text = run_smoke(self.start(commit='f18c425aaaa'), '--expect-commit', '4fc3686')
        self.assertEqual(code, 1)
        self.assertIn('FAIL  STALE BUILD: the live Desk runs commit f18c425aaaa, '
                      'not the expected 4fc3686', text)
        self.assertIn('RESULT: FAIL', text)
        self.assertNotIn('PASS  health', text)

    def test_missing_commit_fails_when_one_is_expected(self):
        code, text = run_smoke(self.start(commit=None), '--expect-commit', 'abc1234')
        self.assertEqual(code, 1)
        self.assertIn('/health reports no commit', text)

    def test_no_expected_commit_passes_with_any_commit(self):
        code, text = run_smoke(self.start(commit=None))
        self.assertEqual(code, 0, text)
        self.assertIn('commit unknown', text)

    def test_expected_commit_comes_from_the_environment(self):
        base_url = self.start()
        with mock.patch.dict('os.environ', {'SMOKE_BASE_URL': base_url,
                                                     'SMOKE_EXPECT_COMMIT': 'deadbee'}):
            out = io.StringIO()
            code = live_smoke.main([], out=out, sleep=lambda seconds: None)
        self.assertEqual(code, 1)
        self.assertIn('not the expected deadbee', out.getvalue())

    def test_health_gives_up_after_three_tries(self):
        waits = []
        code, text = run_smoke(self.start(faults={'health_down'}), sleep=waits.append)
        self.assertEqual(code, 1)
        self.assertIn('did not return 200 after 3 tries', text)
        self.assertEqual(waits, [10, 10])

    def test_root_without_redirect_fails(self):
        code, text = run_smoke(self.start(faults={'no_redirect'}))
        self.assertEqual(code, 1)
        self.assertIn('expected a redirect to /desk', text)

    def test_wrong_review_reasons_fail(self):
        code, text = run_smoke(self.start(faults={'wrong_reasons'}))
        self.assertEqual(code, 1)
        self.assertIn("PO-1042 review reasons [], expected ['AMOUNT_OVER_3X_AVG']", text)

    def test_reset_that_leaves_smoke_payees_fails(self):
        code, text = run_smoke(self.start(faults={'reset_keeps_smoke'}))
        self.assertEqual(code, 1)
        self.assertIn('still shows a smoke payee after Reset demo', text)

    def test_never_prints_the_demo_cookie_and_posts_only_to_safe_routes(self):
        code, text = run_smoke(self.start())
        self.assertEqual(code, 0, text)
        self.assertEqual(len(self.stub.demos), 2)  # exactly two fresh demos per run
        for token in self.stub.demos:
            self.assertNotIn(token, text)
        posts = [(path, headers) for method, path, headers in self.stub.sent if method == 'POST']
        self.assertEqual({path for path, _ in posts}, {'/payouts/simulate', '/payouts/reset'})
        with_demo = [path for path, headers in posts if 'cookie' in headers]
        self.assertEqual(len(with_demo), len(posts) - 1)  # only the "refused" probe has no demo
        for path, headers in posts:
            if 'cookie' in headers:
                self.assertIn('x-demo-id', headers, path)

    def test_connection_refused_is_a_fail(self):
        base_url = self.start()
        port = urlsplit(base_url).port
        self.doCleanups()  # server stopped; the port now refuses connections
        code, text = run_smoke(f'http://127.0.0.1:{port}')
        self.assertEqual(code, 1)
        self.assertIn('RESULT: FAIL', text)

    def test_no_url_fails_without_any_request(self):
        with mock.patch.dict('os.environ', {}, clear=True):
            out = io.StringIO()
            code = live_smoke.main([], out=out)
        self.assertEqual(code, 1)
        self.assertIn('no Desk URL', out.getvalue())


# FEATURE 01 — The same run against the real app, once the Desk routes are on main
class LiveSmokeAgainstLocalAppTests(unittest.TestCase):
    def test_every_step_passes_on_the_local_app(self):
        from werkzeug.serving import make_server
        from hosted_app import create_app
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        server = make_server('127.0.0.1', 0, None, threaded=True)
        origin = f'http://127.0.0.1:{server.server_port}'
        app = create_app(self.temp.name, origin)
        self.addCleanup(app.extensions['close'])
        if '/desk' not in {rule.rule for rule in app.url_map.iter_rules()}:
            server.server_close()
            self.skipTest('the Desk routes are not on this branch yet (#122 step 4)')
        server.app = app
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        code, text = run_smoke(origin)
        self.assertEqual(code, 0, text)
        for step in STEPS:
            self.assertIn('PASS  ' + step, text)



if __name__ == '__main__':
    unittest.main()
