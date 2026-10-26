# Automated checks for the web layer in hosted_app.py (cookies, origins, size limits).
"""Exercise the hosted HTTP boundary with temporary fictional databases."""
import tempfile
import unittest
from pathlib import Path
from hosted_app import create_app


class HostedTests(unittest.TestCase):
    # FEATURE 01 — Test support — setUp
    # Create isolated temporary storage and test clients so scenarios cannot touch real
    # accounts or production data.
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(self.temp.name, 'https://demo.example')
        self.client = self.app.test_client()
        self.body = dict(debtor_account='DEMO-CHECKING-001',
                         creditor_account='DEMO-MERCHANT-002', amount='0.07', currency='USD', rail='ACH')

    # FEATURE 02 — Test support — tearDown
    # Close test resources and remove only the temporary fixture created by this test.
    def tearDown(self):
        self.app.extensions['close']()
        self.temp.cleanup()

    # FEATURE 03 — Test support — get
    # Read through an in-memory Flask client; no external website is contacted.
    def get(self, path, client=None):
        return (client or self.client).get(path, base_url='https://demo.example')

    # FEATURE 04 — Test support — post
    # Submit synthetic JSON through the test client with the selected retry key.
    def post(self, body=None, **headers):
        return self.client.post('/payments', json=body or self.body, base_url='https://demo.example',
                                headers={'Idempotency-Key': 'test-key', 'Origin': 'https://demo.example', **headers})

    # FEATURE 05 — Scenario — health assets and unknown do not create demo
    # Read health, assets, root and unknown/private paths. These requests must not
    # allocate a visitor database.
    def test_health_assets_and_unknown_do_not_create_demo(self):
        for path in ('/', '/health', '/payment-form.js', '/not-found', '/dwolla-credentials.local.json'):
            response = self.get(path)
            self.assertNotIn('Set-Cookie', response.headers)
        self.assertEqual(list(Path(self.temp.name).rglob('payments.sqlite3')), [])

    # FEATURE 06 — Scenario — https cookie and retry moves once
    # Check secure cookie attributes and retry a seven-cent transfer. Balances must move
    # once and conflicting reuse must fail.
    def test_https_cookie_and_retry_moves_once(self):
        response = self.get('/dashboard')
        self.assertIn('Secure', response.headers['Set-Cookie'])
        self.assertIn('HttpOnly', response.headers['Set-Cookie'])
        first = self.post()
        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.json['status'], 'COMPLETED')
        retry = self.post()
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(first.json, retry.json)
        self.assertEqual(self.get('/accounts').json[0]['balance'], '99.93')
        self.assertEqual(self.post(dict(self.body, amount='1.00')).status_code, 409)
        self.assertEqual(self.get('/logs').status_code, 200)

    # FEATURE 07 — Scenario — browser isolation
    # Give separate Flask test clients their own cookies and verify their balances and
    # payment lookup remain separate.
    def test_browser_isolation(self):
        self.get('/dashboard')
        payment = self.post().json
        other = self.app.test_client()
        self.assertEqual(self.get('/accounts', other).json[0]['balance'], '100.00')
        self.assertEqual(self.get('/payments', other).json, [])
        self.assertEqual(self.get('/payments/' + payment['payment_id'], other).status_code, 404)

    # FEATURE 08 — Scenario — missing stale and cross origin submissions
    # Try missing sessions, stale tab identifiers and foreign origins. Reject each
    # before a transfer is created.
    def test_missing_stale_and_cross_origin_submissions(self):
        self.assertEqual(self.post().json['code'], 'DEMO_SESSION_CHANGED')
        self.get('/dashboard')
        self.assertEqual(self.post(**{'X-Demo-ID': 'old-tab'}).status_code, 409)
        self.assertEqual(self.post(Origin='https://different.example').status_code, 403)
        self.assertEqual(self.get('/payments').json, [])

    # FEATURE 09 — Scenario — validation and business rejection
    # Contrast invalid input with a valid unaffordable instruction. The first creates no
    # payment; the second saves a rejection without moving funds.
    def test_validation_and_business_rejection(self):
        self.get('/dashboard')
        invalid = dict(self.body, creditor_account=self.body['debtor_account'])
        self.assertEqual(self.post(invalid).status_code, 422)
        self.assertEqual(self.get('/payments').json, [])
        rejection = self.post(dict(self.body, amount='555.00'))
        self.assertEqual(rejection.status_code, 201)
        self.assertEqual(rejection.json['reason_code'], 'INSUFFICIENT_FUNDS')
        self.assertEqual(self.get('/accounts').json[0]['balance'], '100.00')

    # FEATURE 10 — Scenario — oversized body rejected
    # Send more than the allowed input size. Reject the request without adding a
    # payment.
    def test_oversized_body_rejected(self):
        self.get('/dashboard')
        response = self.client.post('/payments', data='x' * 10001, content_type='application/json',
                                    base_url='https://demo.example')
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.get('/payments').json, [])
