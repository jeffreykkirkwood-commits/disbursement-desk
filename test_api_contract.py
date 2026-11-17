# START HERE — Versioned API + OpenAPI contract tests (P1).
"""OpenAPI document, /v1 create/decide, and stable error codes."""
from __future__ import annotations

import tempfile
import unittest
from uuid import uuid4

import api_contract
from hosted_app import create_app

SITE = 'http://127.0.0.1'


class OpenApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(self.temp.name, SITE)
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.extensions['close']()
        self.temp.cleanup()

    def test_openapi_skips_demo_allocation(self):
        before = list(__import__('pathlib').Path(self.temp.name).rglob('payments.sqlite3'))
        reply = self.client.get('/openapi.json', base_url=SITE)
        self.assertEqual(reply.status_code, 200)
        doc = reply.get_json()
        self.assertEqual(doc['openapi'], api_contract.OPENAPI_VERSION)
        self.assertIn('/v1/payouts', doc['paths'])
        self.assertIn('/v1/payouts/{payout_id}/decisions', doc['paths'])
        after = list(__import__('pathlib').Path(self.temp.name).rglob('payments.sqlite3'))
        self.assertEqual(before, after)

    def test_error_catalog_matches_openapi_enum(self):
        enum = set(api_contract.openapi_document()['components']['schemas']['Error']['properties']['code']['enum'])
        self.assertEqual(enum, set(api_contract.ERROR_CODES))


class V1ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(self.temp.name, SITE)
        self.client = self.app.test_client()
        self.client.get('/desk', base_url=SITE)

    def tearDown(self):
        self.app.extensions['close']()
        self.temp.cleanup()

    def _headers(self, key=None):
        return {
            'Origin': SITE,
            'Idempotency-Key': key or str(uuid4()),
        }

    def test_v1_create_and_decide(self):
        created = self.client.post(
            '/v1/payouts',
            json={
                'payee_name': 'Contract Payee',
                'payee_account': 'DEMO-contract-1',
                'amount_cents': 1200,
            },
            base_url=SITE,
            headers=self._headers('create-v1-1'),
        )
        self.assertEqual(created.status_code, 201)
        payout_id = created.get_json()['payout']['payout_id']
        # Force a FLAGGED seed payout for decide (PO-1042 is flagged in seed).
        decided = self.client.post(
            '/v1/payouts/PO-1042/decisions',
            json={'action': 'hold', 'reason': 'contract check'},
            base_url=SITE,
            headers=self._headers('decide-v1-1'),
        )
        self.assertEqual(decided.status_code, 201)
        self.assertEqual(decided.get_json()['decision']['to_status'], 'HELD')
        self.assertNotIn('reviewer', decided.get_json()['decision'])
        # payout_id from create is unused above except to prove create works
        self.assertTrue(payout_id.startswith('PO-'))

    def test_v1_decide_rejects_reviewer_and_bad_action(self):
        bad_action = self.client.post(
            '/v1/payouts/PO-1042/decisions',
            json={'action': 'sneeze'},
            base_url=SITE,
            headers=self._headers(),
        )
        self.assertEqual(bad_action.status_code, 422)
        self.assertEqual(bad_action.get_json()['code'], api_contract.VALIDATION_ERROR)
        refused = self.client.post(
            '/v1/payouts/PO-1042/decisions',
            json={'action': 'hold', 'reviewer': 'Nope'},
            base_url=SITE,
            headers=self._headers(),
        )
        self.assertEqual(refused.status_code, 422)
        self.assertEqual(refused.get_json()['code'], api_contract.REVIEWER_NOT_ACCEPTED)

    def test_missing_idempotency_key_has_stable_code(self):
        reply = self.client.post(
            '/v1/payouts',
            json={
                'payee_name': 'No Key',
                'payee_account': 'DEMO-nokey',
                'amount_cents': 500,
            },
            base_url=SITE,
            headers={'Origin': SITE},
        )
        self.assertEqual(reply.status_code, 422)
        self.assertEqual(reply.get_json()['code'], api_contract.IDEMPOTENCY_KEY_REQUIRED)

    def test_not_found_includes_code(self):
        reply = self.client.get('/payouts/PO-missing/review-reasons', base_url=SITE)
        self.assertEqual(reply.status_code, 404)
        self.assertEqual(reply.get_json()['code'], api_contract.NOT_FOUND)


if __name__ == '__main__':
    unittest.main()
