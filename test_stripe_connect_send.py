import json
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import stripe
from stripe import WebhookSignature

import api_contract
import ofac
import payout_rules
import payouts
import reconciliation
import stripe_connect
from desk_common import APIError
from hosted_app import create_app

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
WINDOW_CLOSED = datetime(2026, 11, 26, 16, 0, tzinfo=timezone.utc)
WEBHOOK_SECRET = 'whsec_test_secret'
ENV_KEYS = (
    'STRIPE_PAYOUTS_ENABLED', 'STRIPE_SECRET_KEY', 'STRIPE_WEBHOOK_SECRET',
    'STRIPE_DESTINATION_MAP', 'STRIPE_DEMO_ID', 'STRIPE_API_BASE',
    'STRIPE_SEND_PATH', 'STRIPE_FUNDING_PAYMENT_METHOD',
)
PI_TEST_ID = 'pi_test_1'
TR_TEST_ID = 'tr_test_1'
DESTINATIONS = {
    'DEMO-ACME-001': 'acct_acme',
    'DEMO-NWND-003': 'acct_north',
    'DEMO-BLUE-002': 'acct_blue',
    'DEMO-HRBR-006': 'acct_harbor',
    'DEMO-OFAC-011': 'acct_ofac',
    'DEMO-OAKS-010': 'acct_oak',
    'DEMO-MAPL-010': 'acct_maple',
}


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
        if stub.error_status:
            status, payload = stub.error_status, stub.error_body
        else:
            status, payload = 200, stub.payload_for(self.path, parse_qs(raw.decode()))
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        return


class TransferStub:
    def __init__(self):
        self.requests = []
        self.error_status = None
        self.error_body = {
            'error': {'type': 'invalid_request_error', 'message': 'no', 'code': 'account_invalid'},
        }
        self.fixed = None
        self._httpd = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        self._httpd.stub = self
        self.base = f'http://127.0.0.1:{self._httpd.server_address[1]}'
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=3)

    def payload_for(self, path, form):
        if self.fixed is not None:
            return self.fixed

        def one(name):
            return form[name][0]

        if path.startswith('/v1/payment_intents'):
            dest = one('transfer_data[destination]')
            return {
                'id': PI_TEST_ID,
                'object': 'payment_intent',
                'amount': int(one('amount')),
                'currency': one('currency'),
                'status': 'succeeded',
                'livemode': False,
                'transfer_data': {'destination': dest},
                'metadata': {
                    'payout_id': one('metadata[payout_id]'),
                    'demo_id': one('metadata[demo_id]'),
                },
            }
        return {
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


class StripeSendTests(unittest.TestCase):
    def setUp(self):
        self.saved_env = {key: os.environ.get(key) for key in ENV_KEYS}
        self.saved_api_key = stripe.api_key
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pilot = 'a' * 64
        self.other = 'b' * 64
        self.db = self.root / self.pilot / 'payments.sqlite3'
        self.db.parent.mkdir()
        payouts.ensure_seeded(self.db)
        self.stub = TransferStub()
        self.app = create_app(self.root)
        self.client = self.app.test_client()

    def tearDown(self):
        for key, value in self.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        stripe.api_key = self.saved_api_key
        self.stub.close()
        self.temp.cleanup()

    def arm(self, enabled='1', **overrides):
        values = {
            'STRIPE_PAYOUTS_ENABLED': enabled,
            'STRIPE_SECRET_KEY': 'rk_test_desk',
            'STRIPE_WEBHOOK_SECRET': WEBHOOK_SECRET,
            'STRIPE_DESTINATION_MAP': json.dumps(DESTINATIONS),
            'STRIPE_DEMO_ID': self.pilot,
            'STRIPE_API_BASE': self.stub.base,
        }
        values.update(overrides)
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def cents(self, payout_id, database=None):
        with sqlite3.connect(database or self.db) as db:
            return db.execute(
                'SELECT amount_cents FROM payouts WHERE payout_id = ?', (payout_id,)).fetchone()[0]

    def payout(self, payout_id, database=None):
        return payouts.get_payout(database or self.db, payout_id)

    def decide(self, payout_id, action, key, database=None, now=None):
        return payouts.decide(
            database or self.db, payout_id, action, 'Test Reviewer', key,
            now=NOW if now is None else now)

    def assert_blocked(self, payout_id='PO-1042', key='blocked'):
        before = self.payout(payout_id)
        with self.assertRaises(APIError) as caught:
            self.decide(payout_id, 'approve', key)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.code, api_contract.STRIPE_SEND_BLOCKED)
        self.assertEqual(self.payout(payout_id), before)
        self.assertEqual(self.stub.requests, [])
        return caught.exception

    def payment_intent_event(self, payout_id, event_id='evt_1',
                             event_type='payment_intent.succeeded',
                             payment_intent_id=PI_TEST_ID, amount=None, destination='acct_acme',
                             livemode=False, currency='usd', demo_id=None):
        return {
            'id': event_id,
            'object': 'event',
            'type': event_type,
            'livemode': livemode,
            'data': {
                'object': {
                    'id': payment_intent_id,
                    'object': 'payment_intent',
                    'amount': self.cents(payout_id) if amount is None else amount,
                    'currency': currency,
                    'livemode': livemode,
                    'transfer_data': {'destination': destination},
                    'metadata': {
                        'payout_id': payout_id,
                        'demo_id': self.pilot if demo_id is None else demo_id,
                    },
                },
            },
        }

    def transfer_event(self, payout_id, event_id='evt_1', event_type='transfer.created',
                       transfer_id=TR_TEST_ID, amount=None, destination='acct_acme',
                       reversed_flag=False, livemode=False, currency='usd', demo_id=None):
        return {
            'id': event_id,
            'object': 'event',
            'type': event_type,
            'livemode': livemode,
            'data': {
                'object': {
                    'id': transfer_id,
                    'object': 'transfer',
                    'amount': self.cents(payout_id) if amount is None else amount,
                    'currency': currency,
                    'destination': destination,
                    'reversed': reversed_flag,
                    'livemode': livemode,
                    'metadata': {
                        'payout_id': payout_id,
                        'demo_id': self.pilot if demo_id is None else demo_id,
                    },
                },
            },
        }

    def post_event(self, event, secret=WEBHOOK_SECRET, signature=None, include_signature=True,
                   extra_headers=None):
        payload = json.dumps(event, separators=(',', ':')).encode()
        if signature is None and include_signature:
            signature = WebhookSignature.generate_signature_header(payload.decode(), secret)
        headers = {'Content-Type': 'application/json'}
        if include_signature:
            headers['Stripe-Signature'] = signature
        if extra_headers:
            headers.update(extra_headers)
        return self.client.post('/stripe/webhook', data=payload, headers=headers)

    def test_flag_off_keeps_demo_settle_when_secrets_are_present(self):
        self.arm(enabled=None)
        code, decision = self.decide('PO-1042', 'approve', 'demo-off')
        payout = self.payout('PO-1042')
        self.assertEqual(code, 201)
        self.assertEqual(decision['to_status'], 'PAID')
        self.assertEqual(payout['status'], 'PAID')
        self.assertEqual(payout['execution_status'], 'SETTLED')
        self.assertEqual(payout['execution_ref'], 'demo:PO-1042')
        self.assertNotIn('stripe_transfer_id', decision['evidence'])
        [event] = payouts.list_execution_events(self.db, 'PO-1042')
        self.assertEqual(event['source'], 'demo')
        self.assertEqual(self.stub.requests, [])

    def test_flag_other_than_one_keeps_demo_settle(self):
        self.arm(enabled='true')
        code, _decision = self.decide('PO-1042', 'approve', 'demo-true')
        payout = self.payout('PO-1042')
        self.assertEqual(code, 201)
        self.assertEqual((payout['status'], payout['execution_status'], payout['execution_ref']),
                         ('PAID', 'SETTLED', 'demo:PO-1042'))
        self.assertEqual(self.stub.requests, [])

    def test_flag_on_misconfig_blocks_without_a_transfer(self):
        cases = (
            {'STRIPE_SECRET_KEY': ''},
            {'STRIPE_WEBHOOK_SECRET': ''},
            {'STRIPE_WEBHOOK_SECRET': 'not-a-webhook-secret'},
            {'STRIPE_DESTINATION_MAP': ''},
            {'STRIPE_DESTINATION_MAP': '[]'},
            {'STRIPE_DESTINATION_MAP': '{"DEMO-ACME-001": "nope"}'},
            {'STRIPE_DEMO_ID': ''},
            {'STRIPE_DEMO_ID': 'not-a-demo'},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                self.stub.requests.clear()
                self.arm(**overrides)
                self.assert_blocked(key=f"bad-{list(overrides)[0]}")

    def test_live_keys_are_refused(self):
        for key in ('sk_live_abc', 'rk_live_abc'):
            with self.subTest(key=key):
                self.stub.requests.clear()
                self.arm(STRIPE_SECRET_KEY=key)
                self.assert_blocked(key=f'live-{key[:2]}')

    def test_sk_test_key_can_submit(self):
        self.arm(STRIPE_SECRET_KEY='sk_test_desk')
        code, _decision = self.decide('PO-1042', 'approve', 'sk-test')
        payout = self.payout('PO-1042')
        self.assertEqual(code, 201)
        self.assertEqual(payout['execution_status'], 'SUBMITTED')
        self.assertEqual(payout['execution_ref'], PI_TEST_ID)
        self.assertTrue(self.stub.requests[0]['headers']['authorization'].startswith('Bearer sk_test_'))
        self.assertIsNone(stripe.api_key)

    def test_missing_destination_blocks_without_a_transfer(self):
        self.arm(STRIPE_DESTINATION_MAP=json.dumps({'DEMO-OTHER': 'acct_other'}))
        self.assert_blocked(key='no-dest')

    def test_invalid_stored_destination_blocks_without_a_transfer(self):
        self.arm()
        with sqlite3.connect(self.db) as db:
            db.execute(
                'UPDATE payouts SET stripe_connected_account_id = ? WHERE payout_id = ?',
                ('not-an-account', 'PO-1042'))
        self.assert_blocked(key='bad-column')

    def test_stored_destination_wins_over_the_map(self):
        self.arm()
        with sqlite3.connect(self.db) as db:
            db.execute(
                'UPDATE payouts SET stripe_connected_account_id = ? WHERE payout_id = ?',
                ('acct_rowcol', 'PO-1042'))
        code, decision = self.decide('PO-1042', 'approve', 'column')
        self.assertEqual(code, 201)
        self.assertEqual(decision['evidence']['stripe_destination'], 'acct_rowcol')
        self.assertIn('transfer_data[destination]=acct_rowcol', self.stub.requests[0]['body'])
        self.assertEqual(self.payout('PO-1042')['stripe_connected_account_id'], 'acct_rowcol')

    def test_approve_submits_transfer_and_does_not_settle(self):
        self.arm()
        before_requests = len(self.stub.requests)
        code, decision = self.decide('PO-1042', 'approve', 'approve-1')
        payout = self.payout('PO-1042')
        idem = f'desk-charge:{self.pilot}:PO-1042'
        self.assertEqual(code, 201)
        self.assertEqual(decision['to_status'], 'PAID')
        self.assertEqual(payout['status'], 'PAID')
        self.assertEqual(payout['review_status'], 'APPROVED')
        self.assertEqual(payout['execution_status'], 'SUBMITTED')
        self.assertNotEqual(payout['execution_status'], 'SETTLED')
        self.assertEqual(payout['execution_ref'], PI_TEST_ID)
        self.assertEqual(payout['stripe_connected_account_id'], 'acct_acme')
        self.assertEqual(decision['evidence']['stripe_payment_intent_id'], PI_TEST_ID)
        self.assertEqual(decision['evidence']['stripe_destination'], 'acct_acme')
        self.assertEqual(decision['evidence']['stripe_idempotency_key'], idem)
        self.assertIs(decision['evidence']['stripe_livemode'], False)
        [event] = payouts.list_execution_events(self.db, 'PO-1042')
        self.assertEqual(
            (event['event_id'], event['source'], event['execution_status'], event['execution_ref']),
            ('stripe-submit:PO-1042', 'stripe', 'SUBMITTED', PI_TEST_ID))
        self.assertEqual(len(self.stub.requests), before_requests + 1)
        [request] = self.stub.requests
        self.assertEqual(request['path'], '/v1/payment_intents')
        self.assertEqual(request['headers']['idempotency-key'], idem)
        self.assertEqual(request['headers']['stripe-version'], stripe_connect.API_VERSION)
        self.assertTrue(request['headers']['authorization'].startswith('Bearer rk_test_'))
        self.assertIn(f'amount={self.cents("PO-1042")}', request['body'])
        self.assertIn('currency=usd', request['body'])
        self.assertIn('transfer_data[destination]=acct_acme', request['body'])
        self.assertIn('confirm=true', request['body'])
        self.assertIn('off_session=true', request['body'])
        self.assertIn('error_on_requires_action=true', request['body'])
        self.assertIn('payment_method_types[0]=card', request['body'])
        self.assertIn('payment_method=pm_card_visa', request['body'])
        self.assertIn('metadata[payout_id]=PO-1042', request['body'])
        self.assertIn(f'metadata[demo_id]={self.pilot}', request['body'])
        self.assertIsNone(stripe.api_key)
        replay_code, replay = self.decide('PO-1042', 'approve', 'approve-1')
        self.assertEqual(replay_code, 200)
        self.assertEqual(replay['evidence']['stripe_payment_intent_id'], PI_TEST_ID)
        self.assertEqual(len(self.stub.requests), 1)
        with self.assertRaises(APIError) as caught:
            self.decide('PO-1042', 'approve', 'approve-2')
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(len(self.stub.requests), 1)
        self.assertEqual(len(payouts.list_execution_events(self.db, 'PO-1042')), 1)

    def test_clear_sends_one_transfer_and_block_sends_none(self):
        self.arm()
        payout_rules.create(
            self.db, 'PO-OFAC-1', ofac.DEMO_PAYEE_NAME, ofac.DEMO_PAYEE_ACCOUNT,
            ofac.DEMO_AMOUNT_CENTS, 'ACH', now=NOW)
        with self.assertRaises(APIError) as caught:
            self.decide('PO-OFAC-1', 'approve', 'ofac-approve')
        self.assertEqual(caught.exception.status, 409)
        self.assertIn('Clear or Block', caught.exception.message)
        self.assertEqual(self.stub.requests, [])
        self.assertEqual(self.payout('PO-OFAC-1')['status'], 'FLAGGED')
        code, decision = self.decide('PO-OFAC-1', 'clear', 'ofac-clear')
        self.assertEqual(code, 201)
        self.assertEqual(decision['action'], 'clear')
        self.assertEqual(self.payout('PO-OFAC-1')['status'], 'PAID')
        self.assertEqual(self.payout('PO-OFAC-1')['execution_status'], 'SUBMITTED')
        self.assertEqual(len(self.stub.requests), 1)
        self.assertEqual(
            self.stub.requests[0]['headers']['idempotency-key'],
            f'desk-charge:{self.pilot}:PO-OFAC-1')
        self.assertIn(f'amount={ofac.DEMO_AMOUNT_CENTS}', self.stub.requests[0]['body'])
        self.assertIn('transfer_data[destination]=acct_ofac', self.stub.requests[0]['body'])
        payout_rules.create(
            self.db, 'PO-OFAC-2', ofac.DEMO_PAYEE_NAME, ofac.DEMO_PAYEE_ACCOUNT,
            ofac.DEMO_AMOUNT_CENTS, 'ACH', now=NOW)
        code, decision = self.decide('PO-OFAC-2', 'block', 'ofac-block')
        self.assertEqual(code, 201)
        self.assertEqual(decision['to_status'], 'RETURNED')
        self.assertEqual(self.payout('PO-OFAC-2')['status'], 'RETURNED')
        self.assertEqual(len(self.stub.requests), 1)

    def test_rail_refusal_happens_before_a_transfer(self):
        self.arm()
        with sqlite3.connect(self.db) as db:
            db.execute(
                'UPDATE payouts SET amount_cents = ? WHERE payout_id = ?',
                (100_000_001, 'PO-1044'))
            db.execute(
                "UPDATE payouts SET rail = 'SAME_DAY_ACH' WHERE payout_id = ?",
                ('PO-1042',))
        with self.assertRaises(APIError) as over:
            self.decide('PO-1044', 'approve', 'over-limit')
        self.assertEqual(over.exception.code, api_contract.RAIL_OVER_LIMIT)
        self.assertEqual(self.payout('PO-1044')['status'], 'FLAGGED')
        self.assertNotEqual(self.payout('PO-1044')['status'], 'PAID')
        with self.assertRaises(APIError) as closed:
            self.decide('PO-1042', 'approve', 'window', now=WINDOW_CLOSED)
        self.assertEqual(closed.exception.code, api_contract.RAIL_WINDOW_CLOSED)
        self.assertEqual(self.payout('PO-1042')['status'], 'FLAGGED')
        self.assertEqual(self.payout('PO-1042')['execution_status'], 'NOT_STARTED')
        self.assertEqual(self.stub.requests, [])

    def test_stripe_error_rolls_back(self):
        self.arm()
        self.stub.error_status = 400
        before = self.payout('PO-1042')
        with self.assertRaises(APIError) as caught:
            self.decide('PO-1042', 'approve', 'stripe-err')
        self.assertEqual(caught.exception.status, 502)
        self.assertEqual(caught.exception.code, api_contract.STRIPE_SEND_BLOCKED)
        self.assertEqual(self.payout('PO-1042'), before)
        self.assertEqual(len(self.stub.requests), 1)

    def test_live_response_and_non_transfer_id_roll_back(self):
        cents = self.cents('PO-1042')
        base = {
            'object': 'payment_intent', 'amount': cents, 'currency': 'usd',
            'status': 'succeeded', 'transfer_data': {'destination': 'acct_acme'},
            'metadata': {'payout_id': 'PO-1042', 'demo_id': self.pilot},
        }
        cases = (
            dict(base, id=PI_TEST_ID, livemode=True),
            dict(base, id='py_123', livemode=False),
        )
        for payload in cases:
            with self.subTest(transfer_id=payload['id'], livemode=payload['livemode']):
                self.stub.requests.clear()
                self.stub.fixed = payload
                self.arm()
                before = self.payout('PO-1042')
                with self.assertRaises(APIError) as caught:
                    self.decide('PO-1042', 'approve', f"bad-{payload['id']}")
                self.assertEqual(caught.exception.code, api_contract.STRIPE_SEND_BLOCKED)
                self.assertEqual(self.payout('PO-1042'), before)
                self.assertEqual(len(self.stub.requests), 1)

    def test_other_demo_folder_keeps_demo_settle(self):
        other = self.root / self.other / 'payments.sqlite3'
        other.parent.mkdir()
        payouts.ensure_seeded(other)
        self.arm()
        code, _decision = self.decide('PO-1042', 'approve', 'other-demo', database=other)
        payout = self.payout('PO-1042', other)
        self.assertEqual(code, 201)
        self.assertEqual((payout['status'], payout['execution_status'], payout['execution_ref']),
                         ('PAID', 'SETTLED', 'demo:PO-1042'))
        [event] = payouts.list_execution_events(other, 'PO-1042')
        self.assertEqual(event['source'], 'demo')
        self.assertEqual(self.stub.requests, [])

    def test_missing_demo_id_blocks_instead_of_demo_settle(self):
        loose = self.root / 'loose.sqlite3'
        payouts.ensure_seeded(loose)
        self.arm()
        before = self.payout('PO-1042', loose)
        with self.assertRaises(APIError) as caught:
            self.decide('PO-1042', 'approve', 'loose', database=loose)
        self.assertEqual(caught.exception.code, api_contract.STRIPE_SEND_BLOCKED)
        self.assertEqual(self.payout('PO-1042', loose), before)
        self.assertNotEqual(before['execution_ref'], 'demo:PO-1042')
        self.assertEqual(self.stub.requests, [])

    def test_clean_route_flag_off_demo_settles(self):
        result = payout_rules.create(
            self.db, 'PO-CLEAN-OFF', 'Oak Street Books', 'DEMO-OAKS-010', 50000, 'ACH', now=NOW)
        payout = self.payout('PO-CLEAN-OFF')
        self.assertEqual(result['status'], 'PAID')
        self.assertEqual(payout['execution_status'], 'SETTLED')
        self.assertEqual(payout['execution_ref'], 'demo:PO-CLEAN-OFF')
        self.assertEqual(self.stub.requests, [])

    def test_clean_route_flag_on_submits_and_does_not_settle(self):
        self.arm()
        result = payout_rules.create(
            self.db, 'PO-CLEAN-ON', 'Oak Street Books', 'DEMO-OAKS-010', 50000, 'ACH', now=NOW)
        payout = self.payout('PO-CLEAN-ON')
        self.assertEqual(result['status'], 'PAID')
        self.assertEqual(payout['status'], 'PAID')
        self.assertEqual(payout['execution_status'], 'SUBMITTED')
        self.assertNotEqual(payout['execution_status'], 'SETTLED')
        self.assertEqual(payout['execution_ref'], PI_TEST_ID)
        self.assertEqual(result['event']['to_status'], 'PAID')
        self.assertEqual(result['event']['evidence']['stripe_payment_intent_id'], PI_TEST_ID)
        self.assertEqual(len(self.stub.requests), 1)

    def test_clean_route_misconfig_does_not_leave_a_payout(self):
        self.arm(STRIPE_SECRET_KEY='')
        with self.assertRaises(APIError) as caught:
            payout_rules.create(
                self.db, 'PO-CLEAN-BAD', 'Oak Street Books', 'DEMO-OAKS-010', 50000, 'ACH', now=NOW)
        self.assertEqual(caught.exception.code, api_contract.STRIPE_SEND_BLOCKED)
        with self.assertRaises(APIError) as missing:
            self.payout('PO-CLEAN-BAD')
        self.assertEqual(missing.exception.status, 404)
        self.assertEqual(self.stub.requests, [])

    def test_empty_webhook_secret_is_503_without_parsing(self):
        before = self.payout('PO-1042')
        for secret in ('', '   '):
            with self.subTest(secret=repr(secret)):
                self.arm(STRIPE_WEBHOOK_SECRET=secret)
                response = self.post_event(self.payment_intent_event('PO-1042'))
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json['error'], 'Stripe webhook is not configured.')
                self.assertEqual(self.payout('PO-1042'), before)
                self.assertEqual(payouts.list_execution_events(self.db, 'PO-1042'), [])

    def test_bad_webhook_signature_is_400_without_a_demo_session(self):
        self.arm()
        before = self.payout('PO-1042')
        response = self.post_event(
            self.payment_intent_event('PO-1042'), signature='t=1,v1=deadbeef',
            extra_headers={'Origin': 'https://evil.example'})
        self.assertEqual(response.status_code, 400)
        self.assertNotEqual(response.status_code, 403)
        self.assertNotEqual(response.status_code, 409)
        missing = self.post_event(self.payment_intent_event('PO-1042'), include_signature=False)
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(self.payout('PO-1042'), before)
        self.assertEqual(payouts.list_execution_events(self.db, 'PO-1042'), [])

    def test_transfer_created_settles_and_replay_does_not_add_a_row(self):
        self.arm()
        self.decide('PO-1042', 'approve', 'submit')
        response = self.post_event(self.payment_intent_event('PO-1042', event_id='evt_created'))
        self.assertEqual(response.status_code, 200)
        payout = self.payout('PO-1042')
        self.assertEqual(payout['status'], 'PAID')
        self.assertEqual(payout['execution_status'], 'SETTLED')
        self.assertEqual(payout['execution_ref'], PI_TEST_ID)
        again = self.post_event(self.payment_intent_event('PO-1042', event_id='evt_created'))
        self.assertEqual(again.status_code, 200)
        events = payouts.list_execution_events(self.db, 'PO-1042')
        self.assertEqual(
            [event['event_id'] for event in events],
            ['stripe-submit:PO-1042', 'evt_created'])
        settled = [event for event in events if event['event_id'] == 'evt_created'][0]
        self.assertEqual((settled['source'], settled['execution_status']), ('stripe', 'SETTLED'))

    def test_webhook_mismatch_leaves_submitted(self):
        self.arm()
        self.decide('PO-1042', 'approve', 'submit')
        cases = (
            self.payment_intent_event('PO-1042', amount=self.cents('PO-1042') + 1),
            self.payment_intent_event('PO-1042', currency='eur'),
            self.payment_intent_event('PO-1042', destination='acct_other'),
            self.payment_intent_event('PO-1042', payment_intent_id='pi_other'),
        )
        for event in cases:
            with self.subTest(event_id=event['id'], transfer=event['data']['object']['id'],
                              amount=event['data']['object']['amount'],
                              currency=event['data']['object']['currency'],
                              destination=event['data']['object']['transfer_data']['destination']):
                response = self.post_event(event)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json['code'], api_contract.STRIPE_SEND_BLOCKED)
        payout = self.payout('PO-1042')
        self.assertEqual(payout['execution_status'], 'SUBMITTED')
        self.assertEqual(payout['execution_ref'], PI_TEST_ID)
        self.assertEqual(
            [event['event_id'] for event in payouts.list_execution_events(self.db, 'PO-1042')],
            ['stripe-submit:PO-1042'])

    def test_reversed_transfer_fails_without_a_new_desk_status(self):
        self.arm()
        self.decide('PO-1042', 'approve', 'submit')
        response = self.post_event(self.payment_intent_event(
            'PO-1042', event_id='evt_rev', event_type='payment_intent.payment_failed'))
        self.assertEqual(response.status_code, 200)
        payout = self.payout('PO-1042')
        self.assertEqual(payout['status'], 'PAID')
        self.assertEqual(payout['execution_status'], 'FAILED')
        self.assertNotIn('FAILED', payouts.STATUSES)
        failed = [event for event in payouts.list_execution_events(self.db, 'PO-1042')
                  if event['event_id'] == 'evt_rev'][0]
        self.assertEqual((failed['source'], failed['execution_status']), ('stripe', 'FAILED'))

    def test_reversal_after_settled_stays_settled(self):
        self.arm()
        self.decide('PO-1042', 'approve', 'submit')
        self.post_event(self.payment_intent_event('PO-1042', event_id='evt_created'))
        response = self.post_event(self.payment_intent_event(
            'PO-1042', event_id='evt_late', event_type='payment_intent.payment_failed'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.payout('PO-1042')['execution_status'], 'SETTLED')
        self.assertEqual(self.payout('PO-1042')['status'], 'PAID')
        ids = [event['event_id'] for event in payouts.list_execution_events(self.db, 'PO-1042')]
        self.assertEqual(ids, ['stripe-submit:PO-1042', 'evt_created'])

    def test_webhook_before_submit_is_503_and_writes_nothing(self):
        self.arm()
        before = {row['payout_id']: (row['status'], row['execution_status'])
                  for row in payouts.list_payouts(self.db)}
        response = self.post_event(self.payment_intent_event('PO-1042', event_id='evt_early'))
        self.assertEqual(response.status_code, 503)
        after = {row['payout_id']: (row['status'], row['execution_status'])
                 for row in payouts.list_payouts(self.db)}
        self.assertEqual(after, before)
        self.assertEqual(payouts.list_execution_events(self.db, 'PO-1042'), [])

    def test_ignored_event_and_unknown_payout_write_nothing(self):
        self.arm()
        self.decide('PO-1042', 'approve', 'submit')
        ignored = self.post_event({
            'id': 'evt_charge', 'object': 'event', 'type': 'charge.succeeded',
            'livemode': False, 'data': {'object': {'id': 'ch_1', 'object': 'charge'}},
        })
        self.assertEqual(ignored.status_code, 200)
        unknown = self.post_event(self.payment_intent_event('PO-NOPE', event_id='evt_missing', amount=1))
        self.assertEqual(unknown.status_code, 200)
        stranger = self.post_event(self.payment_intent_event(
            'PO-1042', event_id='evt_other_demo', demo_id='c' * 64))
        self.assertEqual(stranger.status_code, 200)
        self.assertEqual(self.payout('PO-1042')['execution_status'], 'SUBMITTED')
        ids = [event['event_id'] for event in payouts.list_execution_events(self.db, 'PO-1042')]
        self.assertEqual(ids, ['stripe-submit:PO-1042'])

    def test_livemode_webhook_is_422_and_writes_nothing(self):
        self.arm()
        self.decide('PO-1042', 'approve', 'submit')
        response = self.post_event(self.payment_intent_event('PO-1042', event_id='evt_live', livemode=True))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json['code'], api_contract.STRIPE_SEND_BLOCKED)
        self.assertEqual(self.payout('PO-1042')['execution_status'], 'SUBMITTED')
        ids = [event['event_id'] for event in payouts.list_execution_events(self.db, 'PO-1042')]
        self.assertEqual(ids, ['stripe-submit:PO-1042'])

    def test_webhook_accepts_a_body_over_the_demo_cap(self):
        self.arm()
        event = {
            'id': 'evt_big', 'object': 'event', 'type': 'charge.succeeded', 'livemode': False,
            'data': {'object': {'id': 'ch_big', 'object': 'charge', 'note': 'x' * 12000}},
        }
        response = self.post_event(event)
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.status_code, 413)

    def test_flag_off_webhook_settles_a_submitted_transfer_only(self):
        os.environ['STRIPE_WEBHOOK_SECRET'] = WEBHOOK_SECRET
        with sqlite3.connect(self.db) as db:
            db.execute(
                'UPDATE payouts SET status = ?, review_status = ?, execution_status = ?, '
                'execution_ref = ?, stripe_connected_account_id = ? WHERE payout_id = ?',
                ('PAID', 'APPROVED', 'SUBMITTED', 'tr_manual', 'acct_acme', 'PO-1042'))
        settled = self.post_event(self.transfer_event(
            'PO-1042', event_id='evt_off', transfer_id='tr_manual'))
        self.assertEqual(settled.status_code, 200)
        self.assertEqual(self.payout('PO-1042')['status'], 'PAID')
        self.assertEqual(self.payout('PO-1042')['execution_status'], 'SETTLED')
        waiting = self.payout('PO-1043')
        early = self.post_event(self.transfer_event(
            'PO-1043', event_id='evt_waiting', transfer_id='tr_waiting',
            destination='acct_blue', amount=self.cents('PO-1043')))
        self.assertEqual(early.status_code, 503)
        self.assertEqual(self.payout('PO-1043')['status'], waiting['status'])
        self.assertEqual(self.payout('PO-1043')['execution_status'], 'NOT_STARTED')
        self.assertNotEqual(self.payout('PO-1043')['status'], 'PAID')

    def test_kill_switch_stops_new_transfers_and_keeps_webhook_settlement(self):
        self.arm()
        self.decide('PO-1042', 'approve', 'pilot-submit')
        self.assertEqual(len(self.stub.requests), 1)
        os.environ['STRIPE_PAYOUTS_ENABLED'] = '0'
        code, _decision = self.decide('PO-1044', 'approve', 'killed')
        killed = self.payout('PO-1044')
        self.assertEqual(code, 201)
        self.assertEqual((killed['status'], killed['execution_status'], killed['execution_ref']),
                         ('PAID', 'SETTLED', 'demo:PO-1044'))
        self.assertEqual(len(self.stub.requests), 1)
        self.assertEqual(self.payout('PO-1042')['execution_status'], 'SUBMITTED')
        response = self.post_event(self.payment_intent_event('PO-1042', event_id='evt_after_kill'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.payout('PO-1042')['status'], 'PAID')
        self.assertEqual(self.payout('PO-1042')['execution_status'], 'SETTLED')
        self.assertEqual(self.payout('PO-1042')['execution_ref'], PI_TEST_ID)

    def test_reconciliation_skips_transfer_refs(self):
        cases = (
            ('PO-1042', 'tr_out'),
            ('PO-1043', 'pi_out'),
            ('PO-1044', 'po_out'),
        )
        with sqlite3.connect(self.db) as db:
            for payout_id, ref in cases:
                db.execute(
                    "UPDATE payouts SET status = 'PAID', review_status = 'APPROVED', "
                    "execution_status = 'SETTLED', execution_ref = ? WHERE payout_id = ?",
                    (ref, payout_id))
        report = reconciliation.reconcile(self.db)
        ids = {item['payout_id'] for item in report['items']}
        for payout_id, _ref in cases:
            self.assertNotIn(payout_id, ids)

    def test_reconciliation_skips_payment_intent_refs(self):
        with sqlite3.connect(self.db) as db:
            db.execute(
                "UPDATE payouts SET status = 'PAID', review_status = 'APPROVED', "
                "execution_status = 'SETTLED', execution_ref = 'pi_out' WHERE payout_id = 'PO-1042'")
        report = reconciliation.reconcile(self.db)
        ids = {item['payout_id'] for item in report['items']}
        self.assertNotIn('PO-1042', ids)

    def test_legacy_transfer_path_selectable_via_env(self):
        self.arm(STRIPE_SEND_PATH='transfer')
        code, decision = self.decide('PO-1042', 'approve', 'legacy-transfer')
        payout = self.payout('PO-1042')
        idem = f'desk-transfer:{self.pilot}:PO-1042'
        self.assertEqual(code, 201)
        self.assertEqual(payout['execution_ref'], TR_TEST_ID)
        self.assertEqual(decision['evidence']['stripe_transfer_id'], TR_TEST_ID)
        self.assertEqual(decision['evidence']['stripe_idempotency_key'], idem)
        request = self.stub.requests[0]
        self.assertEqual(request['path'], '/v1/transfers')
        self.assertIn('destination=acct_acme', request['body'])
        response = self.post_event(self.transfer_event('PO-1042', event_id='evt_legacy'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.payout('PO-1042')['execution_status'], 'SETTLED')

    def test_upgrade_adds_destination_column_and_stripe_source(self):
        path = self.root / 'upgrade.sqlite3'
        payouts.ensure_seeded(path)
        with sqlite3.connect(path) as db:
            db.execute('PRAGMA foreign_keys = OFF')
            db.execute('ALTER TABLE payouts DROP COLUMN stripe_connected_account_id')
            sql = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='execution_events'"
            ).fetchone()[0]
            db.execute('ALTER TABLE execution_events RENAME TO execution_events_old')
            db.execute(sql.replace(", 'stripe'", ''))
            db.execute('DROP TABLE execution_events_old')
            db.execute(
                "INSERT INTO execution_events (event_id, payout_id, execution_status, at, source) "
                "VALUES ('demo-old', 'PO-1042', 'SETTLED', '2026-11-01T00:00:00+00:00', 'demo')")
            db.execute(
                "INSERT INTO payout_events (payout_id, action, from_status, to_status, actor, at) "
                "VALUES ('PO-1042', 'hold', 'FLAGGED', 'HELD', 'Tester', '2026-11-01T00:00:00+00:00')")
        payouts.ensure_seeded(path)
        with sqlite3.connect(path) as db:
            cols = [row[1] for row in db.execute('PRAGMA table_info(payouts)')]
            self.assertIn('stripe_connected_account_id', cols)
            sql = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='execution_events'"
            ).fetchone()[0]
            self.assertIn("'stripe'", sql)
            kept = db.execute(
                "SELECT source FROM execution_events WHERE event_id = 'demo-old'").fetchone()
            self.assertEqual(kept[0], 'demo')
            db.execute(
                'INSERT INTO execution_events '
                '(event_id, payout_id, execution_status, execution_ref, at, source) '
                "VALUES ('evt_upgrade', 'PO-1042', 'SETTLED', 'tr_upgrade', "
                "'2026-11-17T00:00:00+00:00', 'stripe')")


if __name__ == '__main__':
    unittest.main()
