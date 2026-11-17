# START HERE — HTTP OFAC vendor provider tests (P2).
"""Local vs HTTP screening, fail-closed vendor outage, Clear/Block unchanged."""
from __future__ import annotations

import tempfile
import unittest
from io import BytesIO
from unittest import mock
from urllib.error import HTTPError, URLError

import ofac
import payout_rules
import payouts
from desk_common import APIError
from hosted_app import DeskDemo


class FakeResponse:
    def __init__(self, payload, status=200):
        self._raw = __import__('json').dumps(payload).encode('utf-8') if payload is not None else b'null'
        self.status = status

    def read(self):
        return self._raw

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class OfacVendorTests(unittest.TestCase):
    def test_http_match_and_null(self):
        match = {
            'uid': '306',
            'entry_name': 'Banco Nacional de Cuba',
            'entry_type': 'entity',
            'matched_name': 'Banco Nacional de Cuba',
            'score': 100,
            'list_published': '2026-11-09',
            'snapshot_taken': '2026-11-17',
        }
        env = {ofac.PROVIDER_ENV: ofac.PROVIDER_HTTP, ofac.HTTP_URL_ENV: 'https://ofac.example/screen'}
        with mock.patch('urllib.request.urlopen', return_value=FakeResponse(match)) as opener:
            hit = ofac.screen(ofac.DEMO_PAYEE_NAME, env=env)
        self.assertEqual(hit['uid'], '306')
        self.assertEqual(hit['score'], 100)
        opener.assert_called_once()
        with mock.patch('urllib.request.urlopen', return_value=FakeResponse(None)):
            self.assertIsNone(ofac.screen('Oak Street Books', env=env))

    def test_http_outage_fails_closed(self):
        env = {ofac.PROVIDER_ENV: ofac.PROVIDER_HTTP, ofac.HTTP_URL_ENV: 'https://ofac.example/screen'}
        with mock.patch('urllib.request.urlopen', side_effect=URLError('down')):
            held = ofac.screen('Anyone', env=env)
        self.assertEqual(held['uid'], ofac.VENDOR_UNAVAILABLE_UID)
        self.assertEqual(held['score'], 100)
        self.assertEqual(held['entry_type'], 'provider_error')
        self.assertIn('vendor unavailable', ofac.flag_sentence(held).lower())

    def test_http_bad_shape_fails_closed(self):
        env = {ofac.PROVIDER_ENV: ofac.PROVIDER_HTTP, ofac.HTTP_URL_ENV: 'https://ofac.example/screen'}
        with mock.patch('urllib.request.urlopen', return_value=FakeResponse({'uid': 'x'})):
            held = ofac.screen('Anyone', env=env)
        self.assertEqual(held['uid'], ofac.VENDOR_UNAVAILABLE_UID)

    def test_http_error_status_fails_closed(self):
        env = {ofac.PROVIDER_ENV: ofac.PROVIDER_HTTP, ofac.HTTP_URL_ENV: 'https://ofac.example/screen'}
        error = HTTPError('https://ofac.example/screen', 503, 'down', hdrs=None, fp=BytesIO(b''))
        with mock.patch('urllib.request.urlopen', side_effect=error):
            held = ofac.screen('Anyone', env=env)
        self.assertEqual(held['uid'], ofac.VENDOR_UNAVAILABLE_UID)

    def test_missing_url_fails_closed(self):
        env = {ofac.PROVIDER_ENV: ofac.PROVIDER_HTTP}
        held = ofac.screen('Anyone', env=env)
        self.assertEqual(held['uid'], ofac.VENDOR_UNAVAILABLE_UID)

    def test_local_provider_unchanged(self):
        self.assertIsNotNone(ofac.screen(ofac.DEMO_PAYEE_NAME))
        self.assertIsNone(ofac.screen('Oak Street Books'))


class OfacVendorCreateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        demo = DeskDemo(str(__import__('pathlib').Path(self.temp.name) / 'payments.sqlite3'))
        self.database = demo.database

    def tearDown(self):
        self.temp.cleanup()

    def test_vendor_outage_holds_and_refuse_approve(self):
        env = {ofac.PROVIDER_ENV: ofac.PROVIDER_HTTP, ofac.HTTP_URL_ENV: 'https://ofac.example/screen'}
        with mock.patch.dict('os.environ', env, clear=False):
            with mock.patch('urllib.request.urlopen', side_effect=URLError('down')):
                result = payout_rules.create(
                    self.database, 'PO-OFAC-V1', 'Clean Sounding Payee', 'DEMO-vendor-1',
                    500, 'ACH', idempotency_key='ofac-vendor-create-1')
        self.assertEqual(result['status'], 'FLAGGED')
        payout = payouts.get_payout(self.database, 'PO-OFAC-V1')
        self.assertIn(ofac.FLAG_CODE, (payout['flag_code'] or '').split(','))
        with self.assertRaises(APIError) as caught:
            payouts.decide(self.database, 'PO-OFAC-V1', 'approve', 'Op <op>', 'ofac-vendor-approve')
        self.assertEqual(caught.exception.status, 409)
        self.assertIn('Clear or Block', caught.exception.message)


if __name__ == '__main__':
    unittest.main()
