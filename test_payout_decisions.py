# Automated checks for the Desk decisions in payouts.py and hosted_app.py (issue #35):
# approve, hold and return, blocked moves, optional reason, retry keys, approve-from-held,
# and that each viewer's demo is separate. Temporary fictional databases only.
"""Exercise Disbursement Desk approve/hold/return decisions."""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
import payouts
from hosted_app import create_app
from payments import APIError

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
SITE = 'https://demo.example'


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'payments.sqlite3'
        payouts.ensure_seeded(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def decide(self, payout_id, action, key, reason=None, actor='Test Reviewer'):
        return payouts.decide(self.db, payout_id, action, actor, key, reason, now=NOW)

    def status(self, payout_id):
        return payouts.get_payout(self.db, payout_id)['status']

    # FEATURE 01 — Scenario — each allowed decision moves the payout and logs it
    def test_each_allowed_decision(self):
        cases = (('PO-1042', 'approve', 'FLAGGED', 'PAID'), ('PO-1043', 'hold', 'FLAGGED', 'HELD'),
                 ('PO-1044', 'return', 'FLAGGED', 'RETURNED'), ('PO-1038', 'return', 'HELD', 'RETURNED'))
        for payout_id, action, before, after in cases:
            with self.subTest(payout_id=payout_id, action=action):
                self.assertEqual(self.status(payout_id), before)
                code, decision = self.decide(payout_id, action, f'key-{payout_id}')
                self.assertEqual(code, 201)
                self.assertEqual(self.status(payout_id), after)
                self.assertEqual(payouts.get_payout(self.db, payout_id)['updated_at'], NOW.isoformat())
                [logged] = payouts.list_events(self.db, payout_id)
                self.assertEqual(logged, decision)
                self.assertEqual((logged['action'], logged['from_status'], logged['to_status'],
                                  logged['actor'], logged['at']),
                                 (action, before, after, 'Test Reviewer', NOW.isoformat()))

    # FEATURE 02 — Scenario — Held payouts can be approved later, straight to Sent
    def test_approve_from_held(self):
        self.decide('PO-1042', 'hold', 'hold-1')
        code, decision = self.decide('PO-1042', 'approve', 'approve-1')
        self.assertEqual(code, 201)
        self.assertEqual((decision['from_status'], decision['to_status']), ('HELD', 'PAID'))
        self.assertEqual([e['action'] for e in payouts.list_events(self.db, 'PO-1042')], ['hold', 'approve'])
        self.assertEqual(self.status('PO-1038'), 'HELD')  # seeded held payout
        self.assertEqual(self.decide('PO-1038', 'approve', 'approve-2')[1]['to_status'], 'PAID')

    # FEATURE 03 — Scenario — blocked moves are refused clearly and change nothing
    def test_rejected_moves(self):
        # PENDING and APPROVED are internal-only states that are never seeded, so put two
        # sent payouts into them directly to check the Desk still refuses to act on them.
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE payouts SET status = 'PENDING' WHERE payout_id = 'PO-1045'")
            db.execute("UPDATE payouts SET status = 'APPROVED' WHERE payout_id = 'PO-1036'")
        blocked = (('PO-1039', 'approve'), ('PO-1039', 'hold'), ('PO-1039', 'return'),  # PAID
                   ('PO-1037', 'approve'), ('PO-1037', 'hold'), ('PO-1037', 'return'),  # RETURNED
                   ('PO-1045', 'approve'), ('PO-1045', 'hold'), ('PO-1045', 'return'),  # PENDING
                   ('PO-1036', 'approve'), ('PO-1036', 'hold'), ('PO-1036', 'return'),  # APPROVED
                   ('PO-1038', 'hold'))                                                 # HELD again
        for payout_id, action in blocked:
            with self.subTest(payout_id=payout_id, action=action):
                before = payouts.get_payout(self.db, payout_id)
                with self.assertRaises(APIError) as caught:
                    self.decide(payout_id, action, f'{action}-{payout_id}')
                self.assertEqual(caught.exception.status, 409)
                self.assertIn(before['status'], caught.exception.message)
                self.assertEqual(payouts.get_payout(self.db, payout_id), before)
        self.assertEqual(payouts.list_events(self.db), [])
        with self.assertRaises(APIError) as caught:
            self.decide('PO-9999', 'approve', 'missing')
        self.assertEqual(caught.exception.status, 404)

    # FEATURE 04 — Scenario — bad input is refused before anything changes
    def test_bad_input(self):
        for kwargs in (dict(action='pay'), dict(key=''), dict(key='x' * 201), dict(actor=''),
                       dict(actor='   '), dict(actor=None), dict(actor='x' * 81), dict(reason=7),
                       dict(reason='x' * 501)):
            with self.subTest(**{k: repr(v)[:20] for k, v in kwargs.items()}):
                args = dict(payout_id='PO-1042', action='approve', key='k', actor='Test Reviewer')
                args.update(kwargs)
                with self.assertRaises(APIError) as caught:
                    self.decide(**args)
                self.assertEqual(caught.exception.status, 422)
        self.assertEqual(self.status('PO-1042'), 'FLAGGED')
        self.assertEqual(payouts.list_events(self.db), [])

    # FEATURE 05 — Scenario — a reason is optional (confirm-only Hold and Return)
    def test_optional_reason(self):
        self.assertIsNone(self.decide('PO-1042', 'hold', 'a')[1]['reason'])
        self.assertIsNone(self.decide('PO-1043', 'return', 'b', reason='   ')[1]['reason'])
        self.assertEqual(self.decide('PO-1044', 'approve', 'c', reason=' Checked with payee ')[1]['reason'],
                         'Checked with payee')
        self.assertEqual([e['reason'] for e in payouts.list_events(self.db)], [None, None, 'Checked with payee'])

    # FEATURE 06 — Scenario — same decision, same key: nothing happens twice
    def test_idempotent_retry(self):
        first = self.decide('PO-1042', 'hold', 'retry-1')
        again = payouts.decide(self.db, 'PO-1042', 'hold', 'Test Reviewer', 'retry-1',
                               now=datetime(2026, 10, 29, tzinfo=timezone.utc))
        self.assertEqual(first[0], 201)
        self.assertEqual(again, (200, first[1]))
        self.assertEqual(len(payouts.list_events(self.db)), 1)
        self.assertEqual(payouts.get_payout(self.db, 'PO-1042')['updated_at'], NOW.isoformat())
        # A retry still replays after a later decision instead of undoing it.
        self.decide('PO-1042', 'approve', 'retry-2')
        self.assertEqual(self.decide('PO-1042', 'hold', 'retry-1'), (200, first[1]))
        self.assertEqual(self.status('PO-1042'), 'PAID')
        # The same key for different details is a conflict, not a new decision.
        for args in (('PO-1043', 'hold', 'retry-1'), ('PO-1042', 'return', 'retry-1'),
                     ('PO-1042', 'hold', 'retry-1', 'different reason')):
            with self.subTest(args=args):
                with self.assertRaises(APIError) as caught:
                    self.decide(*args)
                self.assertEqual(caught.exception.status, 409)
        self.assertEqual(len(payouts.list_events(self.db)), 2)

    # FEATURE 07 — Scenario — no Desk action leaves a payout PENDING or APPROVED
    def test_never_left_pending_or_approved(self):
        for payout_id in ('PO-1042', 'PO-1043', 'PO-1044', 'PO-1038'):
            for action in ('hold', 'approve', 'return'):
                try:
                    self.decide(payout_id, action, f'{payout_id}-{action}')
                except APIError:
                    pass
                with self.subTest(payout_id=payout_id, action=action):
                    self.assertNotIn(self.status(payout_id), ('PENDING', 'APPROVED'))
        self.assertTrue(all(e['to_status'] in ('HELD', 'RETURNED', 'PAID') for e in payouts.list_events(self.db)))
        for action, (*_, final) in payouts.DECISIONS.items():
            self.assertNotIn(final, ('PENDING', 'APPROVED'), action)

    # FEATURE 08 — Scenario — reset clears decisions back to the starting set
    def test_reset_after_decisions(self):
        self.decide('PO-1042', 'approve', 'r1')
        payouts.reset(self.db)
        self.assertEqual(self.status('PO-1042'), 'FLAGGED')
        self.assertEqual(payouts.list_events(self.db), [])
        self.assertEqual(self.decide('PO-1042', 'approve', 'r1')[0], 201)  # old key free again


class HostedDecisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(self.temp.name, SITE)

    def tearDown(self):
        self.app.extensions['close']()
        self.temp.cleanup()

    def client(self):
        client = self.app.test_client()
        self.assertEqual(client.get('/dashboard', base_url=SITE).status_code, 200)
        return client

    def post(self, client, path, body=None, key='k1', **kwargs):
        return client.post(path, json={'reviewer': 'Test Reviewer'} if body is None else body, base_url=SITE,
                           headers={'Idempotency-Key': key, 'Origin': SITE}, **kwargs)

    # FEATURE 09 — Scenario — the routes record decisions, replay retries, refuse bad moves
    def test_routes(self):
        client = self.client()
        held = self.post(client, '/payouts/PO-1043/hold')
        self.assertEqual(held.status_code, 201)
        self.assertEqual(held.json['payout']['status'], 'HELD')
        self.assertEqual(held.json['decision']['actor'], 'Test Reviewer')
        self.assertIsNone(held.json['decision']['reason'])
        retry = self.post(client, '/payouts/PO-1043/hold')
        self.assertEqual((retry.status_code, retry.json), (200, held.json))
        sent = self.post(client, '/payouts/PO-1043/approve', {'reviewer': 'Test Reviewer', 'reason': 'ok'}, 'k2')
        self.assertEqual((sent.status_code, sent.json['payout']['status']), (201, 'PAID'))
        refused = self.post(client, '/payouts/PO-1043/return', key='k3')
        self.assertEqual(refused.status_code, 409)
        self.assertIn('PAID', refused.json['error'])
        self.assertEqual(self.post(client, '/payouts/PO-1043/hold', {}, 'k4').status_code, 422)
        self.assertEqual(self.post(client, '/payouts/PO-1043/hold', key='').status_code, 422)
        self.assertEqual(self.post(client, '/payouts/PO-1043/hold', [], 'k5').status_code, 422)
        self.assertEqual(self.post(client, '/payouts/PO-9999/hold', key='k6').status_code, 404)
        self.assertEqual(self.post(client, '/payouts/PO-1042/pay', key='k7').status_code, 404)
        self.assertEqual(client.post('/payouts/PO-1042/hold', data='x', base_url=SITE,
                                     headers={'Idempotency-Key': 'k8', 'Origin': SITE}).status_code, 415)
        self.assertEqual(client.get('/payouts/PO-1042/hold', base_url=SITE).status_code, 405)

    # FEATURE 10 — Scenario — a decision changes only this viewer's demo
    def test_per_viewer_isolation(self):
        first, second = self.client(), self.client()
        self.assertEqual(self.post(first, '/payouts/PO-1042/approve').status_code, 201)
        databases = sorted(Path(self.temp.name).rglob('payments.sqlite3'))
        self.assertEqual(len(databases), 2)
        statuses = sorted(payouts.get_payout(db, 'PO-1042')['status'] for db in databases)
        self.assertEqual(statuses, ['FLAGGED', 'PAID'])
        self.assertEqual(sorted(len(payouts.list_events(db)) for db in databases), [0, 1])
        # The same key in the other viewer's demo is a fresh, separate decision.
        again = self.post(second, '/payouts/PO-1042/approve')
        self.assertEqual(again.status_code, 201)
        self.assertEqual(again.json['decision']['from_status'], 'FLAGGED')

    # FEATURE 11 — Scenario — a decision cannot be made without a demo or from another site
    def test_needs_own_session(self):
        stranger = self.app.test_client()
        self.assertEqual(self.post(stranger, '/payouts/PO-1042/approve').status_code, 409)
        client = self.client()
        foreign = client.post('/payouts/PO-1042/approve', json={'reviewer': 'X'}, base_url=SITE,
                              headers={'Idempotency-Key': 'k', 'Origin': 'https://evil.example'})
        self.assertEqual(foreign.status_code, 403)
        for db in Path(self.temp.name).rglob('payments.sqlite3'):
            self.assertEqual(payouts.list_events(db), [])


if __name__ == '__main__':
    unittest.main()
