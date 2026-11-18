"""Admin Stripe recon Desk surface: dry-run default, apply gated, last summary."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import api_contract
import operators
import payouts
import storage
import stripe_reconciliation
from hosted_app import create_app

SITE = 'https://demo.example'


class StripeReconSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(self.temp.name, SITE)
        self.client = self.app.test_client()
        self.assertEqual(self.client.get('/desk', base_url=SITE).status_code, 200)

    def tearDown(self):
        self.app.extensions['close']()
        self.temp.cleanup()

    def database(self):
        paths = list(Path(self.temp.name).glob('*/payments.sqlite3'))
        self.assertEqual(len(paths), 1)
        return paths[0]

    def set_execution(self, payout_id, execution_status, execution_ref):
        with storage.connect(str(self.database())) as db:
            db.execute(
                'UPDATE payouts SET status = ?, review_status = ?, execution_status = ?, '
                'execution_ref = ? WHERE payout_id = ?',
                ('PAID', 'APPROVED', execution_status, execution_ref, payout_id))
            db.commit()

    def test_get_summary_null_before_run(self):
        reply = self.client.get('/stripe/reconciliation', base_url=SITE)
        self.assertEqual(reply.status_code, 200)
        self.assertIsNone(reply.json['summary'])

    def test_post_defaults_to_dry_run_without_secrets(self):
        reply = self.client.post(
            '/stripe/reconciliation', base_url=SITE,
            headers={'Origin': SITE}, json={})
        self.assertEqual(reply.status_code, 200)
        self.assertTrue(reply.json['report']['dry_run'])
        self.assertTrue(reply.json['summary']['dry_run'])
        self.assertEqual(reply.json['report']['run']['updated'], 0)
        again = self.client.get('/stripe/reconciliation', base_url=SITE)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json['summary']['run'], reply.json['summary']['run'])

    def test_empty_body_is_dry_run(self):
        reply = self.client.post(
            '/stripe/reconciliation', base_url=SITE, headers={'Origin': SITE})
        self.assertEqual(reply.status_code, 200)
        self.assertTrue(reply.json['report']['dry_run'])

    def test_desk_shows_recon_for_admin(self):
        html = self.client.get('/desk', base_url=SITE).get_data(as_text=True)
        self.assertIn('aria-label="Stripe recon"', html)
        self.assertIn('id="recon-dry"', html)
        self.assertIn('id="recon-apply"', html)
        self.assertIn('No Stripe recon yet.', html)
        self.assertIn('Apply Stripe recon?', html)
        dry = self.client.post(
            '/stripe/reconciliation', base_url=SITE,
            headers={'Origin': SITE}, json={})
        self.assertEqual(dry.status_code, 200)
        html = self.client.get('/desk', base_url=SITE).get_data(as_text=True)
        self.assertIn('Last dry-run · 0 mismatches', html)

    def test_apply_writes_when_retrieve_mocked(self):
        database = self.database()
        self.set_execution('PO-1042', 'SUBMITTED', 'tr_surface')
        with sqlite3.connect(database) as db:
            amount_cents = db.execute(
                'SELECT amount_cents FROM payouts WHERE payout_id = ?',
                ('PO-1042',)).fetchone()[0]
        transfer = {
            'id': 'tr_surface',
            'object': 'transfer',
            'amount': amount_cents,
            'currency': 'usd',
            'destination': 'acct_acme',
            'reversed': False,
            'livemode': False,
            'metadata': {
                'payout_id': 'PO-1042',
                'demo_id': database.parent.name,
            },
        }
        with mock.patch('stripe_connect.retrieve_object', return_value=transfer):
            dry = self.client.post(
                '/stripe/reconciliation', base_url=SITE,
                headers={'Origin': SITE}, json={'apply': False})
            self.assertEqual(dry.status_code, 200)
            self.assertTrue(dry.json['report']['dry_run'])
            self.assertEqual(dry.json['report']['items'][0]['result'], 'would_update')
            self.assertEqual(
                payouts.get_payout(database, 'PO-1042')['execution_status'], 'SUBMITTED')
            applied = self.client.post(
                '/stripe/reconciliation', base_url=SITE,
                headers={'Origin': SITE}, json={'apply': True})
        self.assertEqual(applied.status_code, 200)
        self.assertFalse(applied.json['report']['dry_run'])
        self.assertEqual(applied.json['report']['items'][0]['result'], 'updated')
        self.assertEqual(
            payouts.get_payout(database, 'PO-1042')['execution_status'], 'SETTLED')

    def test_invalid_json_body_is_422(self):
        reply = self.client.post(
            '/stripe/reconciliation', base_url=SITE,
            headers={'Origin': SITE, 'Content-Type': 'application/json'},
            data='[1]')
        self.assertEqual(reply.status_code, 422)
        self.assertEqual(reply.json['code'], api_contract.INVALID_JSON_BODY)


class StripeReconSummaryUnitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'payments.sqlite3'
        payouts.ensure_seeded(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def test_run_and_remember_persists_mismatches(self):
        with sqlite3.connect(self.db) as db:
            db.execute(
                'UPDATE payouts SET status = ?, review_status = ?, execution_status = ?, '
                'execution_ref = ? WHERE payout_id = ?',
                ('PAID', 'APPROVED', 'SUBMITTED', 'tr_miss', 'PO-1042'))
        result = stripe_reconciliation.run_and_remember(
            self.db, dry_run=True, operator_id='op-test',
            retrieve=lambda ref: None, ran_at='2026-11-18T02:00:00Z')
        self.assertTrue(result['summary']['dry_run'])
        self.assertEqual(result['summary']['operator_id'], 'op-test')
        self.assertEqual(result['summary']['mismatches'][0]['result'], 'missing_on_stripe')
        loaded = stripe_reconciliation.load_last_summary(self.db)
        self.assertEqual(loaded['ran_at'], '2026-11-18T02:00:00Z')
        self.assertEqual(loaded['mismatches'][0]['payout_id'], 'PO-1042')

    def test_desk_hides_recon_for_reviewer(self):
        with mock.patch.dict(os.environ, {operators.ENV_ROLE: 'reviewer'}, clear=False):
            app = create_app(self.temp.name + '-rev', SITE)
            client = app.test_client()
            try:
                html = client.get('/desk', base_url=SITE).get_data(as_text=True)
                self.assertNotIn('id="recon-dry"', html)
                self.assertNotIn('aria-label="Stripe recon"', html)
            finally:
                app.extensions['close']()


if __name__ == '__main__':
    unittest.main()
