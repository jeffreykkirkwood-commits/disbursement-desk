# Automated checks for session-bound operators, role gates, and /ready (P1).
"""Operator identity binds decide/reset; free-text reviewer is refused."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import operators
from hosted_app import create_app

SITE = 'https://demo.example'


class OperatorUnitTests(unittest.TestCase):
    def test_default_operator_uses_env_role_and_name(self):
        op = operators.default_operator('abcdef1234567890', env={
            operators.ENV_ROLE: 'reviewer',
            operators.ENV_NAME: 'Ada',
        })
        self.assertEqual(op.role, 'reviewer')
        self.assertEqual(op.display_name, 'Ada')
        self.assertTrue(op.operator_id.startswith('op-abcdef'))
        self.assertEqual(op.actor, f'Ada <{op.operator_id}>')
        self.assertTrue(op.can_decide())
        self.assertFalse(op.can_reset())
        self.assertFalse(op.can_reconcile_stripe())

    def test_ensure_for_demo_persists_once(self):
        with tempfile.TemporaryDirectory() as folder:
            first = operators.ensure_for_demo(folder, 'deadbeefcafef00d')
            second = operators.ensure_for_demo(folder, 'deadbeefcafef00d')
            self.assertEqual(first, second)
            self.assertTrue((Path(folder) / operators.OPERATOR_FILE).is_file())

    def test_bearer_token_resolution(self):
        env = {
            operators.ENV_TOKENS: json.dumps({
                'tok-ada': {'id': 'op-ada', 'name': 'Ada Reviewer', 'role': 'reviewer'},
            })
        }
        op = operators.resolve_bearer('Bearer tok-ada', env=env)
        self.assertEqual(op.operator_id, 'op-ada')
        self.assertEqual(op.role, 'reviewer')
        with self.assertRaises(PermissionError):
            operators.resolve_bearer('Bearer unknown', env=env)
        self.assertIsNone(operators.resolve_bearer(None, env=env))


class OperatorHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(self.temp.name, SITE)
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.extensions['close']()
        self.temp.cleanup()

    def open_desk(self):
        reply = self.client.get('/desk', base_url=SITE)
        self.assertEqual(reply.status_code, 200)
        return reply

    def test_ready_and_health_do_not_create_demos(self):
        for path in ('/health', '/ready'):
            reply = self.client.get(path, base_url=SITE)
            self.assertEqual(reply.status_code, 200)
            self.assertNotIn('Set-Cookie', reply.headers)
        self.assertEqual(list(Path(self.temp.name).rglob('payments.sqlite3')), [])
        ready = self.client.get('/ready', base_url=SITE).get_json()
        self.assertEqual(ready['status'], 'ready')

    def test_operator_endpoint_and_desk_meta(self):
        page = self.open_desk()
        html = page.get_data(as_text=True)
        self.assertIn('name="operator-id"', html)
        self.assertIn('name="operator-role" content="admin"', html)
        self.assertIn('Desk operator · admin', html)
        me = self.client.get('/operator', base_url=SITE)
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json['operator']['role'], 'admin')
        self.assertEqual(me.json['operator']['display_name'], 'Desk operator')
        self.assertIn('<', me.json['operator']['actor'])

    def test_decide_binds_session_operator_and_rejects_reviewer(self):
        self.open_desk()
        me = self.client.get('/operator', base_url=SITE).json['operator']
        refused = self.client.post(
            '/payouts/PO-1043/hold',
            json={'reviewer': 'Forged Name'},
            base_url=SITE,
            headers={'Idempotency-Key': 'forged', 'Origin': SITE})
        self.assertEqual(refused.status_code, 422)
        self.assertEqual(refused.json['code'], 'REVIEWER_NOT_ACCEPTED')
        held = self.client.post(
            '/payouts/PO-1043/hold',
            json={},
            base_url=SITE,
            headers={'Idempotency-Key': 'hold-1', 'Origin': SITE})
        self.assertEqual(held.status_code, 201)
        self.assertEqual(held.json['decision']['actor'], me['actor'])
        self.assertEqual(held.json['operator']['operator_id'], me['operator_id'])

    def test_reviewer_role_cannot_reset(self):
        with mock.patch.dict(os.environ, {operators.ENV_ROLE: 'reviewer'}, clear=False):
            app = create_app(self.temp.name + '-rev', SITE)
            client = app.test_client()
            try:
                self.assertEqual(client.get('/desk', base_url=SITE).status_code, 200)
                me = client.get('/operator', base_url=SITE).json['operator']
                self.assertEqual(me['role'], 'reviewer')
                reset = client.post('/payouts/reset', base_url=SITE, headers={'Origin': SITE})
                self.assertEqual(reset.status_code, 403)
                self.assertEqual(reset.json['code'], 'OPERATOR_FORBIDDEN')
                recon = client.post('/stripe/reconciliation', base_url=SITE,
                                    headers={'Origin': SITE}, json={})
                self.assertEqual(recon.status_code, 403)
                self.assertEqual(recon.json['code'], 'OPERATOR_FORBIDDEN')
                hold = client.post(
                    '/payouts/PO-1043/hold', json={}, base_url=SITE,
                    headers={'Idempotency-Key': 'rev-hold', 'Origin': SITE})
                self.assertEqual(hold.status_code, 201)
            finally:
                app.extensions['close']()

    def test_bearer_token_overrides_session_operator(self):
        tokens = json.dumps({
            'tok-admin': {'id': 'op-token', 'name': 'Token Admin', 'role': 'admin'},
        })
        with mock.patch.dict(os.environ, {operators.ENV_TOKENS: tokens}, clear=False):
            self.open_desk()
            reply = self.client.post(
                '/payouts/PO-1043/hold', json={}, base_url=SITE,
                headers={'Idempotency-Key': 'tok-hold', 'Origin': SITE,
                         'Authorization': 'Bearer tok-admin'})
            self.assertEqual(reply.status_code, 201)
            self.assertEqual(reply.json['decision']['actor'], 'Token Admin <op-token>')
            bad = self.client.post(
                '/payouts/PO-1042/hold', json={}, base_url=SITE,
                headers={'Idempotency-Key': 'tok-bad', 'Origin': SITE,
                         'Authorization': 'Bearer nope'})
            self.assertEqual(bad.status_code, 401)
            self.assertEqual(bad.json['code'], 'OPERATOR_UNAUTHORIZED')


if __name__ == '__main__':
    unittest.main()
