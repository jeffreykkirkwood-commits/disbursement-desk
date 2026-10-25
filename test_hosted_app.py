"""Exercise the hosted HTTP boundary with temporary fictional databases."""
import tempfile
import unittest
from pathlib import Path
from hosted_app import create_app


class HostedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(self.temp.name, 'https://demo.example')
        self.client = self.app.test_client()
        self.body = dict(debtor_account='DEMO-CHECKING-001',
                         creditor_account='DEMO-MERCHANT-002', amount='0.07', currency='USD', rail='ACH')

    def tearDown(self):
        self.app.extensions['demos'].close()
        self.temp.cleanup()

    def get(self, path, client=None):
        return (client or self.client).get(path, base_url='https://demo.example')

    def post(self, body=None, **headers):
        return self.client.post('/payments', json=body or self.body, base_url='https://demo.example',
                                headers={'Idempotency-Key': 'test-key', 'Origin': 'https://demo.example', **headers})

    def test_health_assets_and_unknown_do_not_create_demo(self):
        for path in ('/', '/health', '/payment-form.js', '/not-found', '/dwolla-credentials.local.json'):
            response = self.get(path)
            self.assertNotIn('Set-Cookie', response.headers)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

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

    def test_browser_isolation(self):
        self.get('/dashboard')
        payment = self.post().json
        other = self.app.test_client()
        self.assertEqual(self.get('/accounts', other).json[0]['balance'], '100.00')
        self.assertEqual(self.get('/payments', other).json, [])
        self.assertEqual(self.get('/payments/' + payment['payment_id'], other).status_code, 404)

    def test_missing_stale_and_cross_origin_submissions(self):
        self.assertEqual(self.post().json['code'], 'DEMO_SESSION_CHANGED')
        self.get('/dashboard')
        self.assertEqual(self.post(**{'X-Demo-ID': 'old-tab'}).status_code, 409)
        self.assertEqual(self.post(Origin='https://different.example').status_code, 403)
        self.assertEqual(self.get('/payments').json, [])

    def test_validation_and_business_rejection(self):
        self.get('/dashboard')
        invalid = dict(self.body, creditor_account=self.body['debtor_account'])
        self.assertEqual(self.post(invalid).status_code, 422)
        self.assertEqual(self.get('/payments').json, [])
        rejection = self.post(dict(self.body, amount='555.00'))
        self.assertEqual(rejection.status_code, 201)
        self.assertEqual(rejection.json['reason_code'], 'INSUFFICIENT_FUNDS')
        self.assertEqual(self.get('/accounts').json[0]['balance'], '100.00')

    def test_oversized_body_rejected(self):
        self.get('/dashboard')
        response = self.client.post('/payments', data='x' * 10001, content_type='application/json',
                                    base_url='https://demo.example')
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.get('/payments').json, [])
