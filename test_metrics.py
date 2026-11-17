# Automated checks for Desk process-local metrics (decide/create/503/stream).
"""Exercise metrics counters and the /metrics snapshot route."""
import tempfile
import unittest
from unittest import mock

import metrics
import payout_events
from hosted_app import create_app

SITE = 'https://demo.example'


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

    def test_metrics_route_skips_demo_allocation(self):
        reply = self.client.get('/metrics', base_url=SITE)
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.get_json()['status'], 'ok')
        self.assertIn(metrics.DECIDE_TOTAL, reply.get_json()['metrics'])
        self.assertNotIn('Set-Cookie', reply.headers)
        self.assertEqual(list(__import__('pathlib').Path(self.temp.name).rglob('payments.sqlite3')), [])

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


if __name__ == '__main__':
    unittest.main()
