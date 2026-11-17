"""Stripe reconciliation. Injected retrieve, no live Stripe secrets."""
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import payouts
import stripe
import stripe_connect
import stripe_reconciliation
from desk_common import APIError


NOW = datetime(2026, 11, 17, 21, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 11, 17, 22, 0, tzinfo=timezone.utc)
EMPTY_RUN = {
    'matched': 0,
    'would_update': 0,
    'updated': 0,
    'mismatch': 0,
    'missing_on_stripe': 0,
    'skipped': 0,
    'error': 0,
}


def _run(**counts):
    report = dict(EMPTY_RUN)
    report.update(counts)
    return report


class StripeReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'payouts.sqlite3'
        payouts.ensure_seeded(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def cents(self, payout_id, database=None):
        with sqlite3.connect(database or self.db) as db:
            return db.execute(
                'SELECT amount_cents FROM payouts WHERE payout_id = ?',
                (payout_id,)).fetchone()[0]

    def set_execution(self, payout_id, execution_status, execution_ref, *,
                      status='PAID', review='APPROVED', account=None, database=None):
        with sqlite3.connect(database or self.db) as db:
            db.execute(
                'UPDATE payouts SET status = ?, review_status = ?, execution_status = ?, '
                'execution_ref = ?, stripe_connected_account_id = ? WHERE payout_id = ?',
                (status, review, execution_status, execution_ref, account, payout_id))

    def payout(self, payout_id, database=None):
        return payouts.get_payout(database or self.db, payout_id)

    def events(self, payout_id, database=None):
        return payouts.list_execution_events(database or self.db, payout_id)

    def snapshot(self, database=None):
        database = database or self.db
        rows = tuple(
            (payout['payout_id'], payout['status'], payout['execution_status'],
             payout['execution_ref'], payout['execution_updated_at'])
            for payout in payouts.list_payouts(database))
        return rows, tuple(tuple(sorted(event.items()))
                           for event in payouts.list_execution_events(database))

    def transfer(self, payout_id, ref, *, reversed_flag=False, amount=None, currency='usd',
                 destination='acct_acme', metadata=True, livemode=False, database=None):
        if metadata is True:
            meta = {'payout_id': payout_id, 'demo_id': 'd' * 64}
        else:
            meta = metadata or {}
        return {
            'id': ref,
            'object': 'transfer',
            'amount': self.cents(payout_id, database) if amount is None else amount,
            'currency': currency,
            'destination': destination,
            'reversed': reversed_flag,
            'livemode': livemode,
            'metadata': meta,
        }

    def by_id(self, report):
        return {item['payout_id']: item for item in report['items']}

    def test_seed_without_stripe_refs_is_empty(self):
        def retrieve(ref):
            raise AssertionError(ref)

        report = stripe_reconciliation.reconcile_stripe(self.db, retrieve=retrieve)
        self.assertTrue(report['dry_run'])
        self.assertEqual(report['items'], [])
        self.assertEqual(report['run'], _run())

    def test_dry_run_would_update_leaves_the_database_unchanged(self):
        self.set_execution('PO-1042', 'SUBMITTED', 'tr_1042')
        before = self.snapshot()
        report = stripe_reconciliation.reconcile_stripe(
            self.db, now=NOW, retrieve=lambda ref: self.transfer('PO-1042', ref))
        item = report['items'][0]
        self.assertEqual(report['dry_run'], True)
        self.assertEqual(report['run'], _run(would_update=1))
        self.assertEqual(
            (item['payout_id'], item['result'], item['execution_status'], item['stripe_outcome']),
            ('PO-1042', 'would_update', 'SUBMITTED', 'SETTLED'))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.events('PO-1042'), [])

    def test_apply_settles_submitted_transfer_and_second_apply_does_not_write(self):
        self.set_execution('PO-1042', 'SUBMITTED', 'tr_1042')
        retrieve = lambda ref: self.transfer('PO-1042', ref)
        first = stripe_reconciliation.reconcile_stripe(
            self.db, dry_run=False, now=NOW, retrieve=retrieve)
        payout = self.payout('PO-1042')
        [event] = self.events('PO-1042')
        self.assertEqual(first['dry_run'], False)
        self.assertEqual(first['run'], _run(updated=1))
        self.assertEqual(first['items'][0]['result'], 'updated')
        self.assertEqual(payout['status'], 'PAID')
        self.assertEqual(payout['execution_status'], 'SETTLED')
        self.assertEqual(payout['execution_ref'], 'tr_1042')
        self.assertEqual(payout['execution_updated_at'], NOW.isoformat())
        self.assertEqual(
            (event['event_id'], event['source'], event['execution_status'],
             event['execution_ref'], event['at']),
            ('recon:stripe:tr_1042:SETTLED', 'stripe', 'SETTLED', 'tr_1042', NOW.isoformat()))
        self.assertEqual(self.payout('PO-1043')['execution_status'], 'NOT_STARTED')
        second = stripe_reconciliation.reconcile_stripe(
            self.db, dry_run=False, now=LATER, retrieve=retrieve)
        self.assertEqual(second['run'], _run(matched=1))
        self.assertEqual(second['items'][0]['result'], 'matched')
        self.assertEqual(self.payout('PO-1042')['execution_updated_at'], NOW.isoformat())
        self.assertEqual(
            [row['event_id'] for row in self.events('PO-1042')],
            ['recon:stripe:tr_1042:SETTLED'])

    def test_settled_desk_versus_reversed_transfer_is_a_mismatch(self):
        self.set_execution('PO-1042', 'SETTLED', 'tr_1042')
        before = self.snapshot()
        report = stripe_reconciliation.reconcile_stripe(
            self.db, dry_run=False, now=NOW,
            retrieve=lambda ref: self.transfer('PO-1042', ref, reversed_flag=True))
        self.assertEqual(report['run'], _run(mismatch=1))
        self.assertEqual(report['items'][0]['stripe_outcome'], 'FAILED')
        self.assertEqual(report['items'][0]['result'], 'mismatch')
        self.assertEqual(self.snapshot(), before)

    def test_matched_when_desk_already_equals_stripe(self):
        self.set_execution('PO-1042', 'SETTLED', 'tr_1042')
        before = self.snapshot()
        report = stripe_reconciliation.reconcile_stripe(
            self.db, dry_run=False, now=NOW,
            retrieve=lambda ref: self.transfer('PO-1042', ref))
        self.assertEqual(report['run'], _run(matched=1))
        self.assertEqual(report['items'][0]['result'], 'matched')
        self.assertEqual(self.snapshot(), before)

    def test_missing_on_stripe_leaves_the_row(self):
        self.set_execution('PO-1042', 'SUBMITTED', 'tr_missing')
        before = self.snapshot()
        report = stripe_reconciliation.reconcile_stripe(
            self.db, dry_run=False, now=NOW, retrieve=lambda ref: None)
        self.assertEqual(report['run'], _run(missing_on_stripe=1))
        self.assertEqual(report['items'][0]['result'], 'missing_on_stripe')
        self.assertIsNone(report['items'][0]['stripe_outcome'])
        self.assertEqual(self.snapshot(), before)

    def test_apply_payment_intent_and_payout(self):
        self.set_execution('PO-1043', 'UNKNOWN', 'pi_1043')
        self.set_execution('PO-1044', 'SUBMITTED', 'po_1044')
        objects = {
            'pi_1043': {
                'id': 'pi_1043',
                'object': 'payment_intent',
                'status': 'canceled',
                'amount': self.cents('PO-1043'),
                'currency': 'usd',
                'livemode': False,
                'metadata': {'payout_id': 'PO-1043'},
            },
            'po_1044': {
                'id': 'po_1044',
                'object': 'payout',
                'status': 'paid',
                'amount': self.cents('PO-1044'),
                'currency': 'usd',
                'livemode': False,
                'metadata': {'payout_id': 'PO-1044'},
            },
        }
        report = stripe_reconciliation.reconcile_stripe(
            self.db, dry_run=False, now=NOW, retrieve=objects.get)
        items = self.by_id(report)
        self.assertEqual(report['run'], _run(updated=2))
        self.assertEqual(items['PO-1043']['result'], 'updated')
        self.assertEqual(items['PO-1044']['result'], 'updated')
        self.assertEqual(self.payout('PO-1043')['status'], 'PAID')
        self.assertEqual(self.payout('PO-1043')['execution_status'], 'FAILED')
        self.assertEqual(self.payout('PO-1044')['execution_status'], 'SETTLED')
        self.assertEqual(
            self.events('PO-1043')[0]['event_id'], 'recon:stripe:pi_1043:FAILED')
        self.assertEqual(
            self.events('PO-1044')[0]['event_id'], 'recon:stripe:po_1044:SETTLED')
        self.assertEqual(self.events('PO-1043')[0]['source'], 'stripe')
        again = stripe_reconciliation.reconcile_stripe(
            self.db, dry_run=False, now=LATER, retrieve=objects.get)
        self.assertEqual(again['run'], _run(matched=2))
        self.assertEqual(len(self.events('PO-1043')), 1)
        self.assertEqual(len(self.events('PO-1044')), 1)

    def test_outcome_registry_reports_without_writing(self):
        rows = (
            ('PO-1042', 'NOT_STARTED', 'tr_idle', 'FLAGGED', 'FLAGGED'),
            ('PO-1043', 'SUBMITTED', 'pi_ok', 'PAID', 'APPROVED'),
            ('PO-1044', 'SUBMITTED', 'pi_cancel', 'PAID', 'APPROVED'),
            ('PO-1039', 'SUBMITTED', 'pi_err', 'PAID', 'APPROVED'),
            ('PO-1040', 'SUBMITTED', 'pi_open', 'PAID', 'APPROVED'),
            ('PO-1041', 'UNKNOWN', 'po_wait', 'PAID', 'APPROVED'),
            ('PO-1036', 'SUBMITTED', 'po_paid', 'PAID', 'APPROVED'),
            ('PO-1045', 'SUBMITTED', 'po_fail', 'PAID', 'APPROVED'),
            ('PO-1038', 'SUBMITTED', 'po_stop', 'PAID', 'APPROVED'),
            ('PO-1037', 'SUBMITTED', 'tr_rev', 'PAID', 'APPROVED'),
        )
        for payout_id, execution, ref, status, review in rows:
            self.set_execution(payout_id, execution, ref, status=status, review=review)
        objects = {
            'pi_ok': self._intent('PO-1043', 'pi_ok', 'succeeded'),
            'pi_cancel': self._intent('PO-1044', 'pi_cancel', 'canceled'),
            'pi_err': self._intent(
                'PO-1039', 'pi_err', 'requires_payment_method',
                last_payment_error={'code': 'card_declined'}),
            'pi_open': self._intent('PO-1040', 'pi_open', 'processing'),
            'po_wait': self._payout_obj('PO-1041', 'po_wait', 'pending'),
            'po_paid': self._payout_obj('PO-1036', 'po_paid', 'paid'),
            'po_fail': self._payout_obj('PO-1045', 'po_fail', 'failed'),
            'po_stop': self._payout_obj('PO-1038', 'po_stop', 'canceled'),
            'tr_rev': self.transfer('PO-1037', 'tr_rev', reversed_flag=True),
        }
        before = self.snapshot()
        report = stripe_reconciliation.reconcile_stripe(self.db, retrieve=objects.get)
        items = self.by_id(report)
        self.assertEqual(items['PO-1042']['result'], 'skipped')
        self.assertEqual(
            (items['PO-1043']['result'], items['PO-1043']['stripe_outcome']),
            ('would_update', 'SETTLED'))
        self.assertEqual(
            (items['PO-1044']['result'], items['PO-1044']['stripe_outcome']),
            ('would_update', 'FAILED'))
        self.assertEqual(
            (items['PO-1039']['result'], items['PO-1039']['stripe_outcome']),
            ('would_update', 'FAILED'))
        self.assertEqual(
            (items['PO-1040']['result'], items['PO-1040']['stripe_outcome']),
            ('matched', None))
        self.assertEqual(
            (items['PO-1041']['result'], items['PO-1041']['stripe_outcome']),
            ('skipped', None))
        self.assertEqual(items['PO-1036']['stripe_outcome'], 'SETTLED')
        self.assertEqual(items['PO-1045']['stripe_outcome'], 'FAILED')
        self.assertEqual(items['PO-1038']['stripe_outcome'], 'FAILED')
        self.assertEqual(items['PO-1037']['stripe_outcome'], 'FAILED')
        self.assertEqual(report['run'], _run(
            skipped=2, would_update=7, matched=1))
        self.assertEqual(self.snapshot(), before)

    def test_amount_conflict_does_not_write_or_stop_the_run(self):
        self.set_execution('PO-1042', 'SUBMITTED', 'tr_bad')
        self.set_execution('PO-1043', 'SUBMITTED', 'tr_good')
        objects = {
            'tr_bad': self.transfer('PO-1042', 'tr_bad', amount=self.cents('PO-1042') + 1),
            'tr_good': self.transfer('PO-1043', 'tr_good'),
        }
        report = stripe_reconciliation.reconcile_stripe(
            self.db, dry_run=False, now=NOW, retrieve=objects.get)
        items = self.by_id(report)
        self.assertEqual(items['PO-1042']['result'], 'mismatch')
        self.assertEqual(items['PO-1043']['result'], 'updated')
        self.assertEqual(self.payout('PO-1042')['execution_status'], 'SUBMITTED')
        self.assertEqual(self.events('PO-1042'), [])
        self.assertEqual(self.payout('PO-1043')['execution_status'], 'SETTLED')

    def test_metadata_payout_conflict_writes_neither_row(self):
        self.set_execution('PO-1042', 'SUBMITTED', 'tr_meta')
        before_other = self.payout('PO-1043')['execution_status']
        obj = self.transfer('PO-1042', 'tr_meta', metadata={'payout_id': 'PO-1043'})
        report = stripe_reconciliation.reconcile_stripe(
            self.db, dry_run=False, now=NOW, retrieve=lambda ref: obj)
        self.assertEqual(report['items'][0]['result'], 'mismatch')
        self.assertEqual(self.payout('PO-1042')['execution_status'], 'SUBMITTED')
        self.assertEqual(self.payout('PO-1043')['execution_status'], before_other)
        self.assertEqual(self.events('PO-1042'), [])
        self.assertEqual(self.events('PO-1043'), [])

    def test_returned_payout_is_not_forced(self):
        self.set_execution(
            'PO-1037', 'SUBMITTED', 'tr_back', status='RETURNED', review='RETURNED')
        before = self.snapshot()
        report = stripe_reconciliation.reconcile_stripe(
            self.db, dry_run=False, now=NOW,
            retrieve=lambda ref: self.transfer('PO-1037', ref))
        self.assertEqual(report['items'][0]['result'], 'mismatch')
        self.assertEqual(self.snapshot(), before)

    def test_retrieve_error_is_isolated(self):
        self.set_execution('PO-1042', 'SUBMITTED', 'tr_down')
        self.set_execution('PO-1043', 'SUBMITTED', 'tr_ok')

        def retrieve(ref):
            if ref == 'tr_down':
                raise RuntimeError('network down')
            return self.transfer('PO-1043', ref)

        before_bad = self.payout('PO-1042')['execution_status']
        report = stripe_reconciliation.reconcile_stripe(self.db, retrieve=retrieve)
        items = self.by_id(report)
        self.assertEqual(items['PO-1042']['result'], 'error')
        self.assertEqual(items['PO-1043']['result'], 'would_update')
        self.assertEqual(self.payout('PO-1042')['execution_status'], before_bad)
        self.assertEqual(self.events('PO-1042'), [])
        self.assertEqual(self.events('PO-1043'), [])

    def test_demo_metadata_conflict_on_a_tenant_database(self):
        tenant = 'a' * 64
        database = Path(self.temp.name) / tenant / 'payments.sqlite3'
        database.parent.mkdir()
        payouts.ensure_seeded(database)
        self.set_execution('PO-1042', 'SUBMITTED', 'tr_demo', database=database)
        obj = self.transfer(
            'PO-1042', 'tr_demo', database=database,
            metadata={'payout_id': 'PO-1042', 'demo_id': 'b' * 64})
        before = self.snapshot(database)
        report = stripe_reconciliation.reconcile_stripe(
            database, dry_run=False, now=NOW, retrieve=lambda ref: obj)
        self.assertEqual(report['items'][0]['result'], 'mismatch')
        self.assertEqual(self.snapshot(database), before)

    def test_cli_dry_run_and_apply(self):
        self.set_execution('PO-1042', 'SUBMITTED', 'tr_cli')
        transfer = self.transfer('PO-1042', 'tr_cli')
        with mock.patch('stripe_connect.retrieve_object', return_value=transfer):
            dry_out = io.StringIO()
            with mock.patch('sys.stdout', dry_out):
                dry_code = stripe_reconciliation.main([str(self.db)])
            self.assertEqual(dry_code, 0)
            dry_report = json.loads(dry_out.getvalue())
            self.assertEqual(dry_report['dry_run'], True)
            self.assertEqual(dry_report['items'][0]['result'], 'would_update')
            self.assertEqual(self.payout('PO-1042')['execution_status'], 'SUBMITTED')
            self.assertEqual(self.events('PO-1042'), [])
            apply_out = io.StringIO()
            with mock.patch('sys.stdout', apply_out):
                apply_code = stripe_reconciliation.main([str(self.db), '--apply'])
            self.assertEqual(apply_code, 0)
            apply_report = json.loads(apply_out.getvalue())
            self.assertEqual(apply_report['dry_run'], False)
            self.assertEqual(apply_report['items'][0]['result'], 'updated')
            self.assertEqual(self.payout('PO-1042')['execution_status'], 'SETTLED')
            self.assertEqual(len(self.events('PO-1042')), 1)

    def test_livemode_dry_run_is_an_error(self):
        self.set_execution('PO-1042', 'SUBMITTED', 'tr_live')
        before = self.snapshot()
        report = stripe_reconciliation.reconcile_stripe(
            self.db, retrieve=lambda ref: self.transfer('PO-1042', ref, livemode=True))
        self.assertEqual(report['items'][0]['result'], 'error')
        self.assertEqual(self.snapshot(), before)

    def test_cli_usage(self):
        err = io.StringIO()
        with mock.patch('sys.stderr', err):
            code = stripe_reconciliation.main([])
        self.assertEqual(code, 2)
        self.assertIn('python -m stripe_reconciliation DATABASE [--apply]', err.getvalue())

    def _intent(self, payout_id, ref, status, last_payment_error=None):
        obj = {
            'id': ref,
            'object': 'payment_intent',
            'status': status,
            'amount': self.cents(payout_id),
            'currency': 'usd',
            'livemode': False,
            'metadata': {'payout_id': payout_id},
        }
        if last_payment_error is not None:
            obj['last_payment_error'] = last_payment_error
        return obj

    def _payout_obj(self, payout_id, ref, status):
        return {
            'id': ref,
            'object': 'payout',
            'status': status,
            'amount': self.cents(payout_id),
            'currency': 'usd',
            'livemode': False,
            'metadata': {'payout_id': payout_id},
        }


class RetrieveObjectTests(unittest.TestCase):
    def test_prefix_routes_retrieve_and_404_is_missing(self):
        env = {
            'STRIPE_PAYOUTS_ENABLED': '1',
            'STRIPE_SECRET_KEY': 'rk_test_desk',
            'STRIPE_WEBHOOK_SECRET': 'whsec_test',
            'STRIPE_DESTINATION_MAP': '{"DEMO-ACME-001": "acct_acme"}',
            'STRIPE_DEMO_ID': 'c' * 64,
        }
        clients = []

        class _Service:
            def __init__(self, kind):
                self.kind = kind
                self.calls = []

            def retrieve(self, ref):
                self.calls.append(ref)
                if str(ref).endswith('gone'):
                    raise stripe.InvalidRequestError(
                        'missing', 'id', code='resource_missing', http_status=404)
                return _Payload({'id': ref, 'kind': self.kind, 'reversed': False,
                                 'status': 'paid', 'amount': 10, 'currency': 'usd'})

        class _Client:
            def __init__(self, **kwargs):
                self.v1 = type('V1', (), {})()
                self.v1.transfers = _Service('transfer')
                self.v1.payment_intents = _Service('payment_intent')
                self.v1.payouts = _Service('payout')
                clients.append(self)

        with mock.patch('stripe_connect.stripe.StripeClient', _Client):
            missing = stripe_connect.retrieve_object('tr_gone', env=env)
            payment = stripe_connect.retrieve_object('pi_1', env=env)
            payout = stripe_connect.retrieve_object('po_1', env=env)
        self.assertIsNone(missing)
        self.assertEqual(payment['kind'], 'payment_intent')
        self.assertEqual(payout['kind'], 'payout')
        self.assertEqual(clients[0].v1.transfers.calls, ['tr_gone'])
        self.assertEqual(clients[1].v1.payment_intents.calls, ['pi_1'])
        self.assertEqual(clients[2].v1.payouts.calls, ['po_1'])

    def test_retrieve_without_stripe_config_is_blocked(self):
        with self.assertRaises(APIError):
            stripe_connect.retrieve_object('tr_1', env={})


class _Payload:
    def __init__(self, payload):
        self.payload = payload

    def to_dict(self):
        return self.payload


class OutcomeRegistryTests(unittest.TestCase):
    def test_requires_payment_method_without_an_error_stays_in_flight(self):
        outcome = stripe_connect.outcome_from_object({
            'id': 'pi_open',
            'object': 'payment_intent',
            'status': 'requires_payment_method',
        })
        self.assertIsNone(outcome)

    def test_disagreed_prefix_and_type_are_refused(self):
        with self.assertRaises(ValueError):
            stripe_connect.outcome_from_object({
                'id': 'tr_1',
                'object': 'payment_intent',
                'status': 'succeeded',
                'reversed': False,
            })
