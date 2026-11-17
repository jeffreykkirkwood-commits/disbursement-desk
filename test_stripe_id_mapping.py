"""Bidirectional mapping between Stripe idempotency keys and object ids."""
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import stripe

import payouts
import stripe_connect

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
WEBHOOK_SECRET = 'whsec_test_secret'
ENV_KEYS = (
    'STRIPE_PAYOUTS_ENABLED', 'STRIPE_SECRET_KEY', 'STRIPE_WEBHOOK_SECRET',
    'STRIPE_DESTINATION_MAP', 'STRIPE_DEMO_ID', 'STRIPE_API_BASE',
    'STRIPE_SEND_PATH', 'STRIPE_FUNDING_PAYMENT_METHOD',
)
DESTINATIONS = {
    'DEMO-ACME-001': 'acct_acme',
}
PI_TEST_ID = 'pi_map_1'
TR_TEST_ID = 'tr_map_1'


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length)
        stub = self.server.stub
        stub.requests.append({
            'path': self.path,
            'headers': {key.lower(): value for key, value in self.headers.items()},
            'body': raw.decode(),
        })
        form = parse_qs(raw.decode())

        def one(name):
            return form[name][0]

        if self.path.startswith('/v1/payment_intents'):
            payload = {
                'id': PI_TEST_ID,
                'object': 'payment_intent',
                'amount': int(one('amount')),
                'currency': one('currency'),
                'status': 'succeeded',
                'livemode': False,
                'transfer_data': {'destination': one('transfer_data[destination]')},
                'metadata': {
                    'payout_id': one('metadata[payout_id]'),
                    'demo_id': one('metadata[demo_id]'),
                },
            }
        else:
            payload = {
                'id': TR_TEST_ID,
                'object': 'transfer',
                'amount': int(one('amount')),
                'currency': one('currency'),
                'destination': one('destination'),
                'livemode': False,
                'reversed': False,
                'metadata': {
                    'payout_id': one('metadata[payout_id]'),
                    'demo_id': one('metadata[demo_id]'),
                },
            }
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        return


class TransferStub:
    def __init__(self):
        self.requests = []
        self._httpd = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        self._httpd.stub = self
        self.base = f'http://127.0.0.1:{self._httpd.server_address[1]}'
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=3)


class StripeIdMappingTests(unittest.TestCase):
    def setUp(self):
        self.saved_env = {key: os.environ.get(key) for key in ENV_KEYS}
        self.saved_api_key = stripe.api_key
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pilot = 'c' * 64
        self.db = self.root / self.pilot / 'payments.sqlite3'
        self.db.parent.mkdir()
        payouts.ensure_seeded(self.db)
        self.stub = TransferStub()
        os.environ.update({
            'STRIPE_PAYOUTS_ENABLED': '1',
            'STRIPE_SECRET_KEY': 'rk_test_desk',
            'STRIPE_WEBHOOK_SECRET': WEBHOOK_SECRET,
            'STRIPE_DESTINATION_MAP': json.dumps(DESTINATIONS),
            'STRIPE_DEMO_ID': self.pilot,
            'STRIPE_API_BASE': self.stub.base,
        })

    def tearDown(self):
        for key, value in self.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        stripe.api_key = self.saved_api_key
        self.stub.close()
        self.temp.cleanup()

    def test_submit_evidence_maps_payment_intent_and_transfer(self):
        pi = stripe_connect.submit_evidence({
            'payment_intent_id': 'pi_abc',
            'idempotency_key': 'desk-charge:demo:PO-1',
            'destination': 'acct_acme',
            'livemode': False,
        })
        self.assertEqual(pi['stripe_object_id'], 'pi_abc')
        self.assertEqual(pi['stripe_object_type'], 'payment_intent')
        self.assertEqual(pi['stripe_payment_intent_id'], 'pi_abc')
        self.assertNotIn('stripe_transfer_id', pi)

        tr = stripe_connect.submit_evidence({
            'transfer_id': 'tr_abc',
            'idempotency_key': 'desk-transfer:demo:PO-1',
            'destination': 'acct_acme',
            'livemode': False,
        })
        self.assertEqual(tr['stripe_object_id'], 'tr_abc')
        self.assertEqual(tr['stripe_object_type'], 'transfer')
        self.assertEqual(tr['stripe_transfer_id'], 'tr_abc')
        self.assertNotIn('stripe_payment_intent_id', tr)

    def test_destination_charge_mapping_queryable_both_ways(self):
        code, decision = payouts.decide(
            self.db, 'PO-1042', 'approve', 'Test Reviewer', 'map-charge-1', now=NOW)
        self.assertEqual(code, 201)
        idem = stripe_connect.charge_idempotency_key(self.pilot, 'PO-1042')
        evidence = decision['evidence']
        self.assertEqual(evidence['stripe_object_id'], PI_TEST_ID)
        self.assertEqual(evidence['stripe_object_type'], 'payment_intent')
        self.assertEqual(evidence['stripe_payment_intent_id'], PI_TEST_ID)
        self.assertEqual(evidence['stripe_idempotency_key'], idem)

        by_key = payouts.stripe_mapping_by_idempotency_key(self.db, idem)
        by_id = payouts.stripe_mapping_by_object_id(self.db, PI_TEST_ID)
        self.assertEqual(by_key['stripe_object_id'], PI_TEST_ID)
        self.assertEqual(by_key['stripe_object_type'], 'payment_intent')
        self.assertEqual(by_key['payout_id'], 'PO-1042')
        self.assertEqual(by_key, by_id)
        self.assertIsNone(payouts.stripe_mapping_by_idempotency_key(self.db, 'missing'))
        self.assertIsNone(payouts.stripe_mapping_by_object_id(self.db, 'pi_missing'))

    def test_legacy_transfer_mapping_queryable_both_ways(self):
        os.environ['STRIPE_SEND_PATH'] = 'transfer'
        code, decision = payouts.decide(
            self.db, 'PO-1042', 'approve', 'Test Reviewer', 'map-transfer-1', now=NOW)
        self.assertEqual(code, 201)
        idem = stripe_connect.transfer_idempotency_key(self.pilot, 'PO-1042')
        evidence = decision['evidence']
        self.assertEqual(evidence['stripe_object_id'], TR_TEST_ID)
        self.assertEqual(evidence['stripe_object_type'], 'transfer')
        self.assertEqual(evidence['stripe_transfer_id'], TR_TEST_ID)
        self.assertEqual(evidence['stripe_idempotency_key'], idem)

        by_key = payouts.stripe_mapping_by_idempotency_key(self.db, idem)
        by_id = payouts.stripe_mapping_by_object_id(self.db, TR_TEST_ID)
        self.assertEqual(by_key['stripe_object_id'], TR_TEST_ID)
        self.assertEqual(by_key['stripe_object_type'], 'transfer')
        self.assertEqual(by_key, by_id)


if __name__ == '__main__':
    unittest.main()
