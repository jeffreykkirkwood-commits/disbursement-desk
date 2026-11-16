# Automated checks for the web layer in hosted_app.py (cookies, origins, size limits, errors).
# The app shell (#122 step 2) has no payout routes yet, so these tests add two small probe
# routes to their own app copy. The probes exist only inside the tests.
"""Exercise the hosted HTTP boundary with temporary fictional databases."""
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from flask import g, jsonify, request
from desk_common import APIError
from hosted_app import create_app

ORIGIN = 'https://demo.example'


# FEATURE 00 — Test support — app with probe routes
# GET /probe opens (or starts) a demo; POST /probe needs an existing one. Both report the
# demo id the web layer picked. /probe/expected and /probe/crash raise errors.
def probe_app(directory, origin=ORIGIN):
    app = create_app(directory, origin)

    def read():
        return jsonify(demo_id=g.demo_id, database=str(g.payments.database))

    def write():
        request.get_data()  # Reading the body applies the size limit.
        return jsonify(demo_id=g.demo_id), 201

    def expected():
        raise APIError(422, 'Send a JSON object with a reviewer name.')

    def crash():
        raise RuntimeError('boom')

    app.add_url_rule('/probe', 'probe_read', read, methods=['GET'])
    app.add_url_rule('/probe', 'probe_write', write, methods=['POST'])
    app.add_url_rule('/probe/expected', 'probe_expected', expected, methods=['GET'])
    app.add_url_rule('/probe/crash', 'probe_crash', crash, methods=['GET'])
    return app


class HostedTests(unittest.TestCase):
    # FEATURE 01 — Test support — setUp
    # Create isolated temporary storage and test clients so scenarios cannot touch real
    # data.
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = probe_app(self.temp.name)
        self.client = self.app.test_client()

    # FEATURE 02 — Test support — tearDown
    # Close test resources and remove only the temporary fixture created by this test.
    def tearDown(self):
        self.app.extensions['close']()
        self.temp.cleanup()

    # FEATURE 03 — Test support — get
    # Read through an in-memory Flask client; no external website is contacted.
    def get(self, path='/probe', client=None):
        return (client or self.client).get(path, base_url=ORIGIN)

    # FEATURE 04 — Test support — post
    # Submit synthetic JSON through the test client from the allowed origin.
    def post(self, client=None, **headers):
        return (client or self.client).post('/probe', json={'reviewer': 'Test'}, base_url=ORIGIN,
                                            headers={'Origin': ORIGIN, **headers})

    def demo_files(self):
        return list(Path(self.temp.name).rglob('payments.sqlite3'))

    # FEATURE 05a — Scenario — health reports the running commit
    # RENDER_GIT_COMMIT (fake value here) appears as commit; unset gives null. Status stays ok.
    def test_health_reports_commit(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('RENDER_GIT_COMMIT', None)
            reply = self.client.get('/health')
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.get_json(), dict(status='ok', mode='fictional payments only', commit=None))
        with mock.patch.dict(os.environ, {'RENDER_GIT_COMMIT': 'abc1234fake'}):
            reply = self.client.get('/health')
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.get_json(), dict(status='ok', mode='fictional payments only', commit='abc1234fake'))

    # FEATURE 05 — Scenario — health and unknown paths do not create a demo
    # These requests must not allocate a visitor database or set a cookie.
    def test_health_and_unknown_do_not_create_demo(self):
        for path in ('/', '/health', '/not-found', '/dwolla-credentials.local.json'):
            response = self.get(path)
            self.assertNotIn('Set-Cookie', response.headers)
        self.assertEqual(self.get('/not-found').status_code, 404)
        self.assertEqual(self.demo_files(), [])

    # FEATURE 06 — Scenario — HTTPS cookie and safety headers
    # A first page view starts a demo with a private, same-site-only, HTTPS-only cookie.
    def test_https_cookie_and_headers(self):
        response = self.get()
        self.assertEqual(response.status_code, 200)
        cookie = response.headers['Set-Cookie']
        for part in ('payments_demo=', 'Path=/', 'HttpOnly', 'SameSite=Strict', 'Max-Age=', 'Secure'):
            self.assertIn(part, cookie)
        self.assertEqual(response.headers['X-Demo-ID'], response.json['demo_id'])
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        self.assertTrue(response.headers['X-Request-ID'])
        self.assertEqual(len(self.demo_files()), 1)
        # The same browser keeps its demo.
        self.assertEqual(self.get().json['demo_id'], response.json['demo_id'])
        self.assertEqual(len(self.demo_files()), 1)

    # FEATURE 07 — Scenario — browsers are isolated
    # Two browsers get two different demos and database files.
    def test_browser_isolation(self):
        other = self.app.test_client()
        first, second = self.get().json, self.get(client=other).json
        self.assertNotEqual(first['demo_id'], second['demo_id'])
        self.assertNotEqual(first['database'], second['database'])
        self.assertEqual(len(self.demo_files()), 2)

    # FEATURE 08 — Scenario — missing, stale and cross-tab submissions
    # A POST without a demo, or from a tab of another demo, gets 409 DEMO_SESSION_CHANGED
    # with the same wording as the Lab, and never creates a demo.
    def test_missing_stale_and_cross_tab_submissions(self):
        missing = self.post()
        self.assertEqual(missing.status_code, 409)
        self.assertEqual(missing.json, dict(error='Demo session missing. Reload the dashboard before submitting.',
                                            code='DEMO_SESSION_CHANGED'))
        self.client.set_cookie('payments_demo', 'a' * 64, domain='demo.example')
        self.assertEqual(self.post().status_code, 409)
        self.assertEqual(self.demo_files(), [])
        demo_id = self.get().json['demo_id']
        self.assertEqual(self.post(**{'X-Demo-ID': demo_id}).status_code, 201)
        stale = self.post(**{'X-Demo-ID': 'b' * 64})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json, dict(error='This tab belongs to a different demo. Reload the dashboard.',
                                          code='DEMO_SESSION_CHANGED'))

    # FEATURE 09 — Scenario — a demo removed by a restart is refused, then replaced on reload (#125)
    # Render's temporary folder is emptied when the server is replaced. The old cookie's
    # POST gets 409; the next page view starts a fresh demo.
    def test_removed_demo_is_refused_then_replaced(self):
        old = self.get().json['demo_id']
        self.app.extensions['close']()
        for folder in Path(self.temp.name).iterdir():
            if folder.name == old:
                for item in sorted(folder.rglob('*'), reverse=True):
                    item.rmdir() if item.is_dir() else item.unlink()
                folder.rmdir()
        self.assertEqual(self.post().json['code'], 'DEMO_SESSION_CHANGED')
        fresh = self.get().json['demo_id']
        self.assertNotEqual(fresh, old)
        self.assertEqual(self.post(**{'X-Demo-ID': fresh}).status_code, 201)

    # FEATURE 10 — Scenario — cross-site submissions are refused
    # The Origin header is checked only when present; Sec-Fetch-Site cross-site is refused.
    def test_cross_site_and_origin_checks(self):
        self.get()
        self.assertEqual(self.post(Origin='https://example.com').status_code, 403)
        self.assertEqual(self.post(Origin='null').status_code, 403)
        self.assertEqual(self.post(**{'Sec-Fetch-Site': 'cross-site'}).status_code, 403)
        self.assertEqual(self.post().status_code, 201)
        bare = self.client.post('/probe', json={}, base_url=ORIGIN,
                                headers={'Sec-Fetch-Site': 'same-origin'})
        self.assertEqual(bare.status_code, 201)

    # FEATURE 11 — Scenario — oversized body is refused
    def test_oversized_body_rejected(self):
        self.get()
        response = self.client.post('/probe', data='x' * 10001, content_type='application/json',
                                    base_url=ORIGIN, headers={'Origin': ORIGIN})
        self.assertEqual(response.status_code, 413)
        self.assertIn('error', response.json)

    # FEATURE 12 — Scenario — expected and unexpected errors become JSON
    # A Desk APIError keeps its status and message; a crash hides its details.
    def test_errors_are_json(self):
        expected = self.get('/probe/expected')
        self.assertEqual(expected.status_code, 422)
        self.assertEqual(expected.json, {'error': 'Send a JSON object with a reviewer name.'})
        crash = self.get('/probe/crash')
        self.assertEqual(crash.status_code, 500)
        self.assertEqual(crash.json, {'error': 'Internal server error. Retry with the same idempotency key.'})

    # FEATURE 13 — Scenario — Render's URL is the allowed origin
    # On Render the public origin must be HTTPS; RENDER_EXTERNAL_URL becomes the allowed origin.
    def test_render_external_url_is_the_allowed_origin(self):
        render_url = 'https://disbursement-desk-demo.onrender.com'
        cleared = {k: v for k, v in os.environ.items() if k not in ('PUBLIC_ORIGIN', 'RENDER_EXTERNAL_URL')}
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(
                os.environ, {**cleared, 'RENDER': 'true', 'RENDER_EXTERNAL_URL': render_url}, clear=True):
            app = probe_app(d, None)
            try:
                client = app.test_client()
                client.get('/probe', base_url=render_url)
                post = lambda origin: client.post('/probe', json={}, base_url=render_url,
                                                  headers={'Origin': origin})
                self.assertEqual(post(ORIGIN).status_code, 403)
                self.assertEqual(post(render_url).status_code, 201)
            finally:
                app.extensions['close']()
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(
                os.environ, {**cleared, 'RENDER': 'true', 'RENDER_EXTERNAL_URL': 'http://insecure.example'},
                clear=True):
            with self.assertRaises(RuntimeError):
                create_app(d)

    # FEATURE 14 — Scenario — local Gunicorn origin default
    # Without PUBLIC_ORIGIN or Render's URL, browsers may submit only from the local
    # Gunicorn address (port 8001). Setting PUBLIC_ORIGIN replaces that address.
    def test_local_gunicorn_origin_default(self):
        cleared = {k: v for k, v in os.environ.items()
                   if k not in ('PUBLIC_ORIGIN', 'RENDER_EXTERNAL_URL', 'RENDER')}
        for env, allowed, refused in (({}, 'http://127.0.0.1:8001', 'http://127.0.0.1:8000'),
                                      ({'PUBLIC_ORIGIN': 'http://127.0.0.1:8000'},
                                       'http://127.0.0.1:8000', 'http://127.0.0.1:8001')):
            with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {**cleared, **env}, clear=True):
                app = probe_app(d, None)
                try:
                    client = app.test_client()
                    first = client.get('/probe', base_url=allowed)
                    self.assertNotIn('Secure', first.headers['Set-Cookie'])
                    post = lambda origin: client.post('/probe', json={}, base_url=allowed,
                                                      headers={'Origin': origin})
                    self.assertEqual(post(refused).status_code, 403)
                    self.assertEqual(post(allowed).status_code, 201)
                finally:
                    app.extensions['close']()


if __name__ == '__main__':
    unittest.main()
