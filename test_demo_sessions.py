# Automated checks that each browser (cookie) gets its own separate demo, using a real local server.
# The app shell (#122 step 2) has no payout routes yet, so the server gets the same probe
# routes as test_hosted_app.py. They exist only inside the tests.
"""HTTP-level isolation tests using separate browser cookies and temporary data."""
import hashlib
import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock
from werkzeug.serving import make_server
from pathlib import Path

from demo_sessions import DemoCapacityError, DemoStore
from hosted_app import DeskDemo
from test_hosted_app import probe_app

LOCAL_ORIGIN = 'http://127.0.0.1:8001'


class BrowserDemoTests(unittest.TestCase):
    # FEATURE 01 — Test support — setUp
    # Create isolated temporary storage so scenarios cannot touch real data.
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.start()

    # FEATURE 02 — Test support — start
    # Start the test server on a local ephemeral port with the fixture directory.
    def start(self):
        self.application = probe_app(self.folder / 'demos', LOCAL_ORIGIN)
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
        self.temp.cleanup()

    # FEATURE 05 — Test support — call
    # Send a synthetic browser request to the local test server, preserving the supplied
    # cookie.
    def call(self, path='/probe', cookie=None, method='GET', headers=None):
        values = {'Content-Type': 'application/json'}
        if cookie:
            values['Cookie'] = cookie
        values.update(headers or {})
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body='{}' if method == 'POST' else None, headers=values)
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
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])
        self.assertIn('Max-Age=', headers['Set-Cookie'])
        self.assertEqual(json.loads(body)['demo_id'], headers['X-Demo-ID'])
        return headers['Set-Cookie'].split(';')[0], headers['X-Demo-ID']

    # FEATURE 07 — Scenario — browsers get separate demos and logs
    # Two browser cookies get separate demo folders, each with its own log.
    def test_browsers_get_separate_demos_and_logs(self):
        a, aid = self.browser()
        b, bid = self.browser()
        self.assertNotEqual(aid, bid)
        self.assertEqual(self.call(cookie=a)[1]['X-Demo-ID'], aid)
        self.assertEqual(self.call(cookie=b)[1]['X-Demo-ID'], bid)
        for demo_id in (aid, bid):
            self.assertTrue((self.folder / 'demos' / demo_id / 'payments.sqlite3').is_file())
            self.assertTrue((self.folder / 'demos' / demo_id / 'logs' / 'api.log').is_file())

    # FEATURE 08 — Scenario — restart restores demo and does not touch legacy database
    # Restart the server with the original cookie. The same demo must return while an
    # unrelated older database remains untouched.
    def test_restart_restores_demo_and_does_not_touch_legacy_database(self):
        legacy = self.folder / 'payments.sqlite3'
        legacy.write_bytes(b'legacy data must stay untouched')
        a, aid = self.browser()
        self.stop()
        self.start()
        self.assertEqual(self.call(cookie=a)[1]['X-Demo-ID'], aid)
        self.assertEqual(self.call(cookie=a, method='POST', headers={'X-Demo-ID': aid})[0], 201)
        self.assertEqual(legacy.read_bytes(), b'legacy data must stay untouched')

    # FEATURE 09 — Scenario — missing forged and stale sessions cannot submit
    # Submit without a valid cookie or with a stale tab identifier. The server must
    # reject the request instead of creating or selecting the wrong demo.
    def test_missing_forged_and_stale_sessions_cannot_submit(self):
        for cookie in (None, 'payments_demo=' + 'a' * 64, 'payments_demo=../../payments.sqlite3'):
            status, _, body = self.call(cookie=cookie, method='POST')
            self.assertEqual((status, json.loads(body)['code']), (409, 'DEMO_SESSION_CHANGED'))
        self.assertEqual(list((self.folder / 'demos').rglob('payments.sqlite3')), [])
        a, aid = self.browser()
        b, bid = self.browser()
        self.assertEqual(self.call(cookie=b, method='POST', headers={'X-Demo-ID': aid})[0], 409)
        # Knowing the public demo ID is insufficient to forge its bearer cookie.
        forged = self.call(cookie='payments_demo=' + aid)
        self.assertNotEqual(forged[1]['X-Demo-ID'], aid)

    # FEATURE 10 — Scenario — cross site post rejected and same origin allowed
    # Compare submissions from another website and the configured website. Only the
    # allowed origin may proceed.
    def test_cross_site_post_rejected_and_same_origin_allowed(self):
        a, aid = self.browser()
        self.assertEqual(self.call(cookie=a, method='POST', headers={'Origin': 'https://example.com'})[0], 403)
        self.assertEqual(self.call(cookie=a, method='POST', headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)
        self.assertEqual(self.call(cookie=a, method='POST',
                                   headers={'Origin': LOCAL_ORIGIN, 'X-Demo-ID': aid})[0], 201)

    # FEATURE 11 — Scenario — same cookie concurrent requests share one demo
    # Four simultaneous requests with one browser cookie all land in the same demo and
    # do not start a second one.
    def test_same_cookie_concurrent_requests_share_one_demo(self):
        a, aid = self.browser()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.call(cookie=a, method='POST'), range(4)))
        self.assertEqual([v[0] for v in results], [201] * 4)
        self.assertEqual({json.loads(v[2])['demo_id'] for v in results}, {aid})
        self.assertEqual(len(list((self.folder / 'demos').rglob('payments.sqlite3'))), 1)

    # FEATURE 12 — Scenario — cookie not saved in logs and private files are not served
    # Raw browser tokens never appear in logs, and demo or credential files are not
    # reachable by URL.
    def test_cookie_not_saved_in_logs_and_files_not_served(self):
        a, aid = self.browser()
        self.call(cookie=a, method='POST')
        token = a.split('=', 1)[1]
        self.assertEqual(hashlib.sha256(token.encode()).hexdigest(), aid)
        for path in (self.folder / 'demos').rglob('*.log'):
            self.assertNotIn(token, path.read_text())
        self.assertEqual(self.call('/dwolla-credentials.local.json', a)[0], 404)
        self.assertEqual(self.call('/demos/' + aid + '/payments.sqlite3', a)[0], 404)


class DemoStoreLimitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    # FEATURE 13 — Scenario — demo limits must be positive
    def test_limits_must_be_positive(self):
        with mock.patch.dict(os.environ, {'DEMO_MAX_SESSIONS': '0'}):
            with self.assertRaises(ValueError):
                DemoStore(self.folder, DeskDemo)

    # FEATURE 14 — Scenario — a full service refuses a new demo but keeps existing ones
    def test_capacity_refuses_new_demo(self):
        with mock.patch.dict(os.environ, {'DEMO_MAX_SESSIONS': '1'}):
            store = DemoStore(self.folder, DeskDemo)
        try:
            demo_id, _, _, cookie, operator = store.resolve('', create=True)
            self.assertTrue(operator.can_decide())
            store.release(demo_id)
            with self.assertRaises(DemoCapacityError):
                store.resolve('', create=True)
            again = store.resolve(cookie.split(';')[0], create=True)
            self.assertEqual(again[0], demo_id)
            self.assertEqual(again[4].operator_id, operator.operator_id)
            store.release(demo_id)
        finally:
            store.close()

    # FEATURE 15 — Scenario — idle demos expire, demos in use do not
    def test_idle_demo_expires_unless_in_use(self):
        with mock.patch.dict(os.environ, {'DEMO_IDLE_SECONDS': '60'}):
            store = DemoStore(self.folder, DeskDemo)
        try:
            idle, _, _, _, _ = store.resolve('', create=True)
            store.release(idle)
            busy, _, _, _, _ = store.resolve('', create=True)
            old = time.time() - 120
            for demo_id in (idle, busy):
                os.utime(self.folder / demo_id / '.last-used', (old, old))
            store.resolve('', create=True)
            self.assertFalse((self.folder / idle).exists())
            self.assertTrue((self.folder / busy).exists())
        finally:
            store.close()


if __name__ == '__main__':
    unittest.main()
