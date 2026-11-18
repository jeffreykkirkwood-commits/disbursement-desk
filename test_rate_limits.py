# Wave C — in-process rate limits on mutating Desk POSTs.
"""Prove allow/refuse math and HTTP 429 on decide/create/reset/simulate/demo/recon."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

import api_contract
import payout_events
import rate_limits
from hosted_app import create_app

SITE = 'https://demo.example'


class RateLimitUnitTests(unittest.TestCase):
    def setUp(self):
        rate_limits.reset()
        with mock.patch.dict(os.environ, {
            rate_limits.ENV_MAX: '2',
            rate_limits.ENV_WINDOW: '60',
        }, clear=False):
            rate_limits.configure()

    def tearDown(self):
        rate_limits.reset()
        rate_limits.configure()

    def test_allows_until_cap_then_refuses(self):
        self.assertTrue(rate_limits.allow('decide', '1.1.1.1', 'demo-a'))
        self.assertTrue(rate_limits.allow('decide', '1.1.1.1', 'demo-a'))
        self.assertFalse(rate_limits.allow('decide', '1.1.1.1', 'demo-a'))

    def test_separate_keys_for_ip_demo_and_bucket(self):
        self.assertTrue(rate_limits.allow('decide', '1.1.1.1', 'demo-a'))
        self.assertTrue(rate_limits.allow('decide', '1.1.1.1', 'demo-a'))
        self.assertFalse(rate_limits.allow('decide', '1.1.1.1', 'demo-a'))
        self.assertTrue(rate_limits.allow('decide', '2.2.2.2', 'demo-a'))
        self.assertTrue(rate_limits.allow('decide', '1.1.1.1', 'demo-b'))
        self.assertTrue(rate_limits.allow('reset', '1.1.1.1', 'demo-a'))

    def test_window_expiry_frees_slots(self):
        self.assertTrue(rate_limits.allow('decide', '1.1.1.1', 'demo-a', now=100.0))
        self.assertTrue(rate_limits.allow('decide', '1.1.1.1', 'demo-a', now=101.0))
        self.assertFalse(rate_limits.allow('decide', '1.1.1.1', 'demo-a', now=102.0))
        self.assertTrue(rate_limits.allow('decide', '1.1.1.1', 'demo-a', now=161.0))


class RateLimitHttpTests(unittest.TestCase):
    def setUp(self):
        rate_limits.reset()
        payout_events.reset_stream_slots()
        self.temp = tempfile.TemporaryDirectory()
        with mock.patch.dict(os.environ, {
            rate_limits.ENV_MAX: '2',
            rate_limits.ENV_WINDOW: '60',
        }, clear=False):
            rate_limits.configure()
            self.app = create_app(self.temp.name, SITE)
        self.client = self.app.test_client()
        opened = self.client.get('/desk', base_url=SITE)
        self.assertEqual(opened.status_code, 200)

    def tearDown(self):
        self.app.extensions['close']()
        self.temp.cleanup()
        rate_limits.reset()
        rate_limits.configure()
        payout_events.reset_stream_slots()

    def post(self, path, body=None, key='rl-1'):
        return self.client.post(
            path, json={} if body is None else body, base_url=SITE,
            headers={'Idempotency-Key': key, 'Origin': SITE})

    def assert_rate_limited(self, reply):
        self.assertEqual(reply.status_code, 429)
        self.assertEqual(reply.get_json()['code'], api_contract.RATE_LIMITED)

    def test_decide_returns_429_after_cap(self):
        first = self.post('/payouts/PO-1044/hold', key='rl-hold-1')
        second = self.post('/payouts/PO-1043/hold', key='rl-hold-2')
        third = self.post('/payouts/PO-1042/hold', key='rl-hold-3')
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assert_rate_limited(third)

    def test_create_and_simulate_and_reset_and_demo_and_recon(self):
        # Two creates under the shared create/simulate? Different buckets.
        # Exhaust simulate, then prove create/reset/demo/recon independently.
        for n in (1, 2):
            reply = self.post(
                '/payouts/simulate',
                {'payee_name': f'RL Payee {n}', 'payee_account': f'DEMO-RL-{n:03d}',
                 'amount_cents': 1000, 'rail': 'ACH'},
                key=f'rl-sim-{n}')
            self.assertEqual(reply.status_code, 201, reply.get_json())
        self.assert_rate_limited(self.post(
            '/payouts/simulate',
            {'payee_name': 'RL Payee 3', 'payee_account': 'DEMO-RL-003',
             'amount_cents': 1000, 'rail': 'ACH'},
            key='rl-sim-3'))

        for n in (1, 2):
            reply = self.post(
                '/v1/payouts',
                {'payee_name': f'RL V1 {n}', 'payee_account': f'DEMO-RLV-{n:03d}',
                 'amount_cents': 1000, 'rail': 'ACH'},
                key=f'rl-v1-{n}')
            self.assertEqual(reply.status_code, 201, reply.get_json())
        self.assert_rate_limited(self.post(
            '/v1/payouts',
            {'payee_name': 'RL V1 3', 'payee_account': 'DEMO-RLV-003',
             'amount_cents': 1000, 'rail': 'ACH'},
            key='rl-v1-3'))

        self.assertEqual(self.post('/payouts/reset', key='rl-reset-1').status_code, 200)
        self.assertEqual(self.post('/payouts/reset', key='rl-reset-2').status_code, 200)
        self.assert_rate_limited(self.post('/payouts/reset', key='rl-reset-3'))

        self.assertEqual(self.post('/demo-scenario', key='rl-demo-1').status_code, 200)
        self.assertEqual(self.post('/demo-scenario', key='rl-demo-2').status_code, 200)
        self.assert_rate_limited(self.post('/demo-scenario', key='rl-demo-3'))

        self.assertEqual(self.post('/reconciliation', key='rl-recon-1').status_code, 200)
        self.assertEqual(self.post('/reconciliation', key='rl-recon-2').status_code, 200)
        self.assert_rate_limited(self.post('/reconciliation', key='rl-recon-3'))


if __name__ == '__main__':
    unittest.main()
