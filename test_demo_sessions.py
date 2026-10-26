# START HERE — Synthetic regression scenarios: each assertion protects the behavior described below.
"""HTTP-level isolation tests using separate browser cookies and temporary data."""
import hashlib
import http.client
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from werkzeug.serving import make_server
from pathlib import Path

from payments import Payments
from hosted_app import create_app
from demo_sessions import DemoStore


class BrowserDemoTests(unittest.TestCase):
    # FEATURE 01 — Test support — setUp
    # Create isolated temporary storage and test clients so scenarios cannot touch real
    # accounts or production data.
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.start()
        self.body = dict(debtor_account='DEMO-CHECKING-001', creditor_account='DEMO-MERCHANT-002',
                         amount='25.00', currency='USD', rail='ACH')

    # FEATURE 02 — Test support — start
    # Start the test server on a local ephemeral port with the fixture directory.
    def start(self):
        self.application = create_app(self.folder / 'demos')
        self.store = self.application.extensions['demos']
        self.server = make_server('127.0.0.1', 0, self.application, threaded=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    # FEATURE 03 — Test support — stop
    # Shut down the local test server before restart or cleanup.
    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.application.extensions['close']()

    # FEATURE 04 — Test support — tearDown
    # Close test resources and remove only the temporary fixture created by this test.
    def tearDown(self):
        self.stop()
        self.application.extensions['close']()
        self.temp.cleanup()

    # FEATURE 05 — Test support — call
    # Send a synthetic browser request to the local test server, preserving the supplied
    # cookie.
    def call(self, path='/accounts', cookie=None, method='GET', data=None, headers=None):
        values = {'Content-Type': 'application/json', 'Idempotency-Key': 'same-retry-key'}
        if cookie:
            values['Cookie'] = cookie
        values.update(headers or {})
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=json.dumps(data) if data else None, headers=values)
            response = connection.getresponse()
            body = response.read().decode()
            return response.status, dict(response.getheaders()), body
        finally:
            connection.close()

    # FEATURE 06 — Test support — browser
    # Open a fresh demo and capture its cookie for subsequent requests.
    def browser(self):
        status, headers, body = self.call()
        self.assertEqual(status, 200)
        self.assertEqual([a['balance'] for a in json.loads(body)], ['100.00', '100.00'])
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])
        self.assertIn('Max-Age=', headers['Set-Cookie'])
        return headers['Set-Cookie'].split(';')[0], headers['X-Demo-ID']

    # FEATURE 07 — Scenario — browsers isolate balances payments lookup processing and logs
    # Give two browser cookies separate demos. Transfers, identifiers, retry keys and
    # logs must not cross between their workspaces.
    def test_browsers_isolate_balances_payments_lookup_processing_and_logs(self):
        a, aid = self.browser()
        b, bid = self.browser()
        self.assertNotEqual(aid, bid)
        status, headers, text = self.call('/payments', a, 'POST', self.body)
        self.assertEqual(status, 201)
        payment = json.loads(text)
        pid = payment['payment_id']
        self.assertEqual([v['balance'] for v in json.loads(self.call(cookie=a)[2])], ['75.00', '125.00'])
        self.assertEqual([v['balance'] for v in json.loads(self.call(cookie=b)[2])], ['100.00', '100.00'])
        self.assertEqual(json.loads(self.call('/payments', b)[2]), [])
        self.assertEqual(self.call('/payments/' + pid, b)[0], 404)
        self.assertEqual(self.call('/payments/' + pid + '/process', b, 'POST')[0], 404)
        self.assertNotIn(pid, self.call('/dashboard', b)[2])
        self.assertIn(pid, self.call('/dashboard', a)[2])
        self.assertIn('content="' + aid + '"', self.call('/dashboard', a)[2])
        # B's attempted lookup is normalized in logs, never recording A's payment ID.
        self.assertNotIn(pid, self.call('/logs?viewer=1', b)[2])
        self.assertIn(pid, self.call('/logs', a)[2])
        self.assertNotIn(headers['X-Request-ID'], self.call('/logs?viewer=1', b)[2])
        # The same idempotency key is independent across demos.
        other = self.call('/payments', b, 'POST', self.body)
        self.assertEqual(other[0], 201)
        self.assertNotEqual(json.loads(other[2])['payment_id'], pid)
        self.assertEqual(self.call('/payments', a, 'POST', self.body)[0], 200)

    # FEATURE 08 — Scenario — restart restores demo and does not touch legacy database
    # Restart the server with the original cookie. Its saved balance must return while
    # an unrelated older database remains untouched.
    def test_restart_restores_demo_and_does_not_touch_legacy_database(self):
        legacy = self.folder / 'payments.sqlite3'
        legacy.write_bytes(b'legacy data must stay untouched')
        a, aid = self.browser()
        self.call('/payments', a, 'POST', self.body)
        self.stop()
        self.start()
        self.assertEqual(self.call(cookie=a)[1]['X-Demo-ID'], aid)
        self.assertEqual([v['balance'] for v in json.loads(self.call(cookie=a)[2])], ['75.00', '125.00'])
        self.assertEqual(legacy.read_bytes(), b'legacy data must stay untouched')

    # FEATURE 09 — Scenario — missing forged and stale sessions cannot submit
    # Submit without a valid cookie or with a stale tab identifier. The server must
    # reject the request instead of creating or selecting the wrong demo.
    def test_missing_forged_and_stale_sessions_cannot_submit(self):
        for cookie in (None, 'payments_demo=' + 'a' * 64, 'payments_demo=../../payments.sqlite3'):
            self.assertEqual(self.call('/payments', cookie, 'POST', self.body)[0], 409)
        self.assertEqual(list((self.folder / 'demos').rglob('payments.sqlite3')), [])
        a, aid = self.browser()
        b, bid = self.browser()
        self.assertEqual(self.call('/payments', b, 'POST', self.body, {'X-Demo-ID': aid})[0], 409)
        self.assertEqual(json.loads(self.call('/payments', b)[2]), [])
        # Knowing the public demo ID is insufficient to forge its bearer cookie.
        forged = self.call(cookie='payments_demo=' + aid)
        self.assertNotEqual(forged[1]['X-Demo-ID'], aid)

    # FEATURE 10 — Scenario — cross site post rejected and same origin allowed
    # Compare submissions from another website and the configured website. Only the
    # allowed origin may proceed.
    def test_cross_site_post_rejected_and_same_origin_allowed(self):
        a, aid = self.browser()
        self.assertEqual(self.call('/payments', a, 'POST', self.body, {'Origin': 'https://example.com'})[0], 403)
        self.assertEqual(self.call('/payments', a, 'POST', self.body, {'Sec-Fetch-Site': 'cross-site'})[0], 403)
        origin = 'http://127.0.0.1:8001'
        self.assertEqual(self.call('/payments', a, 'POST', self.body, {'Origin': origin, 'X-Demo-ID': aid})[0], 201)

    # FEATURE 11 — Scenario — same cookie concurrent retries debit once
    # Send four simultaneous requests using one browser cookie and retry key. The shared
    # demo must debit only once.
    def test_same_cookie_concurrent_retries_debit_once(self):
        a, aid = self.browser()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.call('/payments', a, 'POST', self.body), range(4)))
        self.assertEqual(sorted(v[0] for v in results), [200, 200, 200, 201])
        self.assertEqual(len({json.loads(v[2])['payment_id'] for v in results}), 1)
        self.assertEqual([v['balance'] for v in json.loads(self.call(cookie=a)[2])], ['75.00', '125.00'])

    # FEATURE 12 — Scenario — cookie not saved in logs and new browser starts fresh
    # Check that raw browser tokens do not appear in logs and a fresh cookie receives
    # fresh fictional balances.
    def test_cookie_not_saved_in_logs_and_new_browser_starts_fresh(self):
        a, aid = self.browser()
        self.call('/payments', a, 'POST', self.body)
        b, bid = self.browser()
        self.assertNotEqual(aid, bid)
        token = a.split('=', 1)[1]
        self.assertEqual(hashlib.sha256(token.encode()).hexdigest(), aid)
        for path in (self.folder / 'demos').rglob('*.log'):
            self.assertNotIn(token, path.read_text())
        self.assertEqual(self.call('/dwolla-credentials.local.json', a)[0], 404)
        self.assertEqual(self.call('/demos/' + aid + '/payments.sqlite3', a)[0], 404)


if __name__ == '__main__':
    unittest.main()
