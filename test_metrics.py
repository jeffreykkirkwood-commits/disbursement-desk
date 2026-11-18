# Automated checks for Desk process-local metrics (decide/create/503/stream/OFAC).
"""Exercise metrics counters and the /metrics snapshot route."""
import os
import tempfile
import unittest
from unittest import mock

import metrics
import ofac
import payout_events
from hosted_app import create_app

SITE = 'https://demo.example'
METRICS_SECRET = 'metrics-test-secret'


class MetricsUnitTests(unittest.TestCase):
    def setUp(self):
        metrics.reset()

    def test_inc_and_snapshot_group_by_labels(self):
        metrics.inc(metrics.DECIDE_TOTAL, action='hold', http_status=201, replayed=False)
        metrics.inc(metrics.DECIDE_TOTAL, action='hold', http_status=201, replayed=False)
        metrics.inc(metrics.CREATE_TOTAL, http_status=201, payout_status='FLAGGED', replayed=False)
        snap = metrics.snapshot()
        self.assertEqual(snap[metrics.DECIDE_TOTAL][0]['value'], 2)
        self.assertEqual(snap[metrics.CREATE_TOTAL][0]['labels']['payout_status'], 'FLAGGED')
        self.assertEqual(snap[metrics.HTTP_503_TOTAL], [])

    def test_unknown_metric_is_refused(self):
        with self.assertRaises(ValueError):
            metrics.inc('not_a_real_metric')


class MetricsHttpTests(unittest.TestCase):
    def setUp(self):
        metrics.reset()
        payout_events.reset_stream_slots()
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(self.temp.name, SITE)
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.extensions['close']()
        self.temp.cleanup()
        metrics.reset()
        payout_events.reset_stream_slots()

    def open_desk(self):
        reply = self.client.get('/desk', base_url=SITE)
        self.assertEqual(reply.status_code, 200)
        return reply

    def post(self, path, body=None, key='k1'):
        return self.client.post(
            path, json={} if body is None else body, base_url=SITE,
            headers={'Idempotency-Key': key, 'Origin': SITE})

    def ofac_value(self, source):
        rows = [row for row in self.metrics_snap()[metrics.OFAC_HOLD_TOTAL]
                if row['labels'].get('source') == source]
        return rows[0]['value'] if rows else 0

    def metrics_snap(self):
        reply = self.client.get('/metrics', base_url=SITE)
        self.assertEqual(reply.status_code, 200)
        return reply.get_json()['metrics']

    def test_metrics_route_skips_demo_allocation(self):
        reply = self.client.get('/metrics', base_url=SITE)
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.get_json()['status'], 'ok')
        self.assertIn(metrics.DECIDE_TOTAL, reply.get_json()['metrics'])
        self.assertEqual(reply.get_json()['metrics'][metrics.OFAC_HOLD_TOTAL], [])
        self.assertNotIn('Set-Cookie', reply.headers)
        self.assertEqual(list(__import__('pathlib').Path(self.temp.name).rglob('payments.sqlite3')), [])

    def test_metrics_secret_required_when_configured(self):
        missing = self.client.get('/not-a-route', base_url=SITE)
        with mock.patch.dict(os.environ, {metrics.ENV_SECRET: METRICS_SECRET}, clear=False):
            closed = self.client.get('/metrics', base_url=SITE)
            wrong = self.client.get(
                '/metrics', base_url=SITE,
                headers={metrics.HEADER: 'nope'})
            opened = self.client.get(
                '/metrics', base_url=SITE,
                headers={metrics.HEADER: METRICS_SECRET})
        self.assertEqual(closed.status_code, 404)
        self.assertEqual(closed.get_json(), missing.get_json())
        self.assertEqual(wrong.status_code, 404)
        self.assertEqual(opened.status_code, 200)
        self.assertEqual(opened.get_json()['status'], 'ok')

    def test_metrics_closed_on_render_when_secret_unset(self):
        missing = self.client.get('/not-a-route', base_url=SITE)
        with mock.patch.dict(os.environ, {'RENDER': 'true'}, clear=False):
            os.environ.pop(metrics.ENV_SECRET, None)
            closed = self.client.get('/metrics', base_url=SITE)
        self.assertEqual(closed.status_code, 404)
        self.assertEqual(closed.get_json(), missing.get_json())

    def test_decide_and_create_increment_counters(self):
        self.open_desk()
        hold = self.client.post(
            '/payouts/PO-1043/hold',
            json={},
            base_url=SITE,
            headers={'Idempotency-Key': 'm-hold', 'Origin': SITE})
        self.assertEqual(hold.status_code, 201)
        created = self.client.post(
            '/payouts/simulate',
            json={'payee_name': 'Metrics Payee', 'payee_account': 'DEMO-MET-001',
                  'amount_cents': 50000, 'rail': 'ACH'},
            base_url=SITE,
            headers={'Idempotency-Key': 'm-create', 'Origin': SITE})
        self.assertEqual(created.status_code, 201)
        snap = self.client.get('/metrics', base_url=SITE).get_json()['metrics']
        decide = snap[metrics.DECIDE_TOTAL]
        self.assertEqual(len(decide), 1)
        self.assertEqual(decide[0]['value'], 1)
        self.assertEqual(decide[0]['labels']['action'], 'hold')
        create = snap[metrics.CREATE_TOTAL]
        self.assertEqual(create[0]['value'], 1)
        self.assertEqual(create[0]['labels']['replayed'], 'False')

    def test_stream_open_and_refused_are_counted(self):
        self.open_desk()
        with mock.patch('payout_events.stream', return_value=iter([': keepalive\n\n'])):
            opened = self.client.get('/payouts/stream', base_url=SITE)
            self.assertEqual(opened.status_code, 200)
            self.assertEqual(b''.join(opened.response).decode(), ': keepalive\n\n')
        with mock.patch('payout_events.acquire_stream', return_value=False):
            refused = self.client.get('/payouts/stream', base_url=SITE)
        self.assertEqual(refused.status_code, 503)
        snap = self.client.get('/metrics', base_url=SITE).get_json()['metrics']
        self.assertEqual(snap[metrics.STREAM_OPEN_TOTAL][0]['value'], 1)
        self.assertEqual(snap[metrics.STREAM_REFUSED_TOTAL][0]['value'], 1)
        reasons = {row['labels'].get('reason') for row in snap[metrics.HTTP_503_TOTAL]}
        self.assertIn('stream_cap', reasons)

    def test_ordinary_approve_and_hold_do_not_count_as_ofac_hold(self):
        self.open_desk()
        approved = self.post('/payouts/PO-1042/approve', key='m-approve')
        self.assertEqual(approved.status_code, 201)
        self.assertEqual(approved.get_json()['payout']['status'], 'PAID')
        held = self.post('/payouts/PO-1044/hold', key='m-hold-op')
        self.assertEqual(held.status_code, 201)
        self.assertEqual(held.get_json()['payout']['status'], 'HELD')
        flagged = self.post(
            '/payouts/simulate',
            {'payee_name': 'Metrics New Payee', 'payee_account': 'DEMO-MET-900',
             'amount_cents': 150000, 'rail': 'ACH'},
            key='m-flag')
        self.assertEqual(flagged.status_code, 201)
        self.assertEqual(flagged.get_json()['payout']['status'], 'FLAGGED')
        self.assertNotIn('POSSIBLE_SANCTIONS_MATCH', flagged.get_json()['payout']['flag_code'])
        self.assertEqual(self.metrics_snap()[metrics.OFAC_HOLD_TOTAL], [])

    def test_ofac_hold_counter_bumps_on_create_and_screen_only(self):
        self.open_desk()
        created = self.post(
            '/payouts/simulate',
            {'payee_name': ofac.DEMO_PAYEE_NAME, 'payee_account': ofac.DEMO_PAYEE_ACCOUNT,
             'amount_cents': ofac.DEMO_AMOUNT_CENTS, 'rail': 'ACH', 'payout_id': 'PO-4100'},
            key='m-ofac')
        self.assertEqual(created.status_code, 201)
        payout = created.get_json()['payout']
        self.assertEqual(payout['status'], 'FLAGGED')
        self.assertIn('POSSIBLE_SANCTIONS_MATCH', payout['flag_code'])
        self.assertEqual(self.ofac_value('create'), 1)
        self.assertEqual(self.ofac_value('screen'), 0)
        replay = self.post(
            '/payouts/simulate',
            {'payee_name': ofac.DEMO_PAYEE_NAME, 'payee_account': ofac.DEMO_PAYEE_ACCOUNT,
             'amount_cents': ofac.DEMO_AMOUNT_CENTS, 'rail': 'ACH', 'payout_id': 'PO-4100'},
            key='m-ofac')
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(self.ofac_value('create'), 1)
        cleared = self.post('/payouts/PO-4100/clear', key='m-clear')
        self.assertEqual(cleared.status_code, 201)
        self.assertEqual(cleared.get_json()['payout']['status'], 'PAID')
        self.assertEqual(self.ofac_value('create'), 1)
        self.assertEqual(self.ofac_value('screen'), 0)
        match = ofac.screen(ofac.DEMO_PAYEE_NAME)
        self.assertIsNotNone(match)
        with mock.patch('payouts.ofac.screen', return_value=match):
            screened = self.post('/payouts/PO-1043/approve', key='m-screen')
        self.assertEqual(screened.status_code, 201)
        self.assertEqual(screened.get_json()['decision']['action'], 'screen')
        self.assertEqual(screened.get_json()['payout']['status'], 'FLAGGED')
        self.assertIn('POSSIBLE_SANCTIONS_MATCH', screened.get_json()['payout']['flag_code'])
        self.assertEqual(self.ofac_value('screen'), 1)
        self.assertEqual(self.ofac_value('create'), 1)
        versioned = self.post(
            '/v1/payouts',
            {'payee_name': ofac.DEMO_PAYEE_NAME, 'payee_account': ofac.DEMO_PAYEE_ACCOUNT,
             'amount_cents': ofac.DEMO_AMOUNT_CENTS, 'rail': 'ACH', 'payout_id': 'PO-4101'},
            key='m-ofac-v1')
        self.assertEqual(versioned.status_code, 201)
        self.assertEqual(versioned.get_json()['payout']['status'], 'FLAGGED')
        self.assertEqual(self.ofac_value('create'), 2)
        self.assertEqual(self.ofac_value('screen'), 1)


if __name__ == '__main__':
    unittest.main()
