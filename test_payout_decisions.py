# Automated checks for the Desk decisions in payouts.py and hosted_app.py (issue #35):
# approve, hold and return, blocked moves, optional reason, retry keys, approve-from-held,
# and that each viewer's demo is separate. Temporary fictional databases only.
"""Exercise Disbursement Desk approve/hold/return decisions."""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
import desk
import payout_events
import payout_rules
import payouts
from hosted_app import create_app
from desk_common import APIError

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
SITE = 'https://demo.example'
MAIN_QUEUE_IDS = ['PO-1042', 'PO-1043', 'PO-1044', 'PO-1038', 'PO-1037',
                  'PO-1036', 'PO-1039', 'PO-1040', 'PO-1041', 'PO-1045']


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


    def flags(self, database=None):
        return {p['payout_id']: (p['flag_code'], p['flag_text'])
                for p in payouts.list_payouts(database or self.db)}

    # FEATURE 12 — Scenario — Approve records evidence and settles in the same flow (#55 option B)
    def test_approve_writes_evidence_and_settles(self):
        before = payouts.get_payout(self.db, 'PO-1042')
        code, decision = self.decide('PO-1042', 'approve', 'approve-ev')
        self.assertEqual(code, 201)
        payout = payouts.get_payout(self.db, 'PO-1042')
        self.assertEqual(payout['status'], 'PAID')
        self.assertEqual(payout['review_status'], 'APPROVED')
        self.assertEqual(payout['execution_status'], 'SETTLED')
        self.assertEqual(payout['execution_ref'], 'demo:PO-1042')
        self.assertEqual(payout['execution_updated_at'], NOW.isoformat())
        self.assertEqual(decision['evidence']['amount_cents'], 485000)
        self.assertEqual(decision['evidence']['flag_code'], before['flag_code'])
        self.assertEqual(decision['evidence']['flag_text'], before['flag_text'])
        self.assertEqual(decision['evidence']['payee_usual_cents'], before['payee_usual_cents'])
        self.assertEqual(decision['evidence']['bank_details_changed_at'],
                         before['bank_details_changed_at'])
        self.assertEqual(decision['evidence']['rules_version'], payouts.RULES_VERSION)
        self.assertEqual(decision['evidence']['actor'], 'Test Reviewer')
        self.assertIsNone(decision['evidence']['reason'])
        [logged] = payouts.list_events(self.db, 'PO-1042')
        self.assertEqual(logged, decision)
        [event] = payouts.list_execution_events(self.db, 'PO-1042')
        self.assertEqual(event['event_id'], 'demo-settle:PO-1042')
        self.assertEqual(event['execution_status'], 'SETTLED')
        self.assertEqual(event['source'], 'demo')
        self.assertEqual(event['execution_ref'], 'demo:PO-1042')

    # FEATURE 13 — Scenario — the same Approve key does not settle twice
    def test_approve_settle_is_idempotent(self):
        first = self.decide('PO-1042', 'approve', 'approve-once')
        again = payouts.decide(self.db, 'PO-1042', 'approve', 'Test Reviewer', 'approve-once',
                               now=datetime(2026, 10, 29, tzinfo=timezone.utc))
        self.assertEqual(again, (200, first[1]))
        self.assertEqual(len(payouts.list_events(self.db, 'PO-1042')), 1)
        self.assertEqual(len(payouts.list_execution_events(self.db, 'PO-1042')), 1)
        self.assertEqual(payouts.get_payout(self.db, 'PO-1042')['updated_at'], NOW.isoformat())

    # FEATURE 14 — Scenario — Hold and Return save evidence and do not settle
    def test_hold_and_return_save_evidence_without_settling(self):
        self.decide('PO-1043', 'hold', 'hold-ev')
        held = payouts.get_payout(self.db, 'PO-1043')
        self.assertEqual((held['status'], held['review_status'], held['execution_status']),
                         ('HELD', 'HELD', 'NOT_STARTED'))
        self.assertIsNone(held['execution_ref'])
        self.assertEqual(payouts.list_execution_events(self.db, 'PO-1043'), [])
        self.assertEqual(payouts.list_events(self.db, 'PO-1043')[0]['evidence']['flag_code'],
                         'BANK_CHANGED_7D')
        self.decide('PO-1044', 'return', 'return-ev', reason='Send back')
        returned = payouts.get_payout(self.db, 'PO-1044')
        self.assertEqual((returned['status'], returned['review_status'], returned['execution_status']),
                         ('RETURNED', 'RETURNED', 'NOT_STARTED'))
        self.assertEqual(payouts.list_events(self.db, 'PO-1044')[0]['evidence']['reason'], 'Send back')

    # FEATURE 15 — Scenario — Reset demo restores flags and clears evidence and settle rows
    def test_reset_after_approve_clears_settle_and_evidence(self):
        self.decide('PO-1042', 'approve', 'reset-ev')
        payouts.reset(self.db)
        self.assertEqual(self._seed_flags(), {
            'PO-1037': ('BANK_CHANGED_7D', 'Payee bank details changed 1 day ago'),
            'PO-1038': ('AMOUNT_OVER_3X_AVG', 'Amount is 4× this payee’s usual payout ($520 avg)'),
            'PO-1042': ('AMOUNT_OVER_3X_AVG', 'Amount is 6× this payee’s usual payout ($790 avg)'),
            'PO-1043': ('BANK_CHANGED_7D', 'Payee bank details changed 2 days ago'),
            'PO-1044': ('DUPLICATE_24H', 'Same payee and amount as yesterday (possible duplicate)'),
        })
        self.assertEqual(payouts.list_events(self.db), [])
        self.assertEqual(payouts.list_execution_events(self.db), [])
        self.assertEqual(payouts.get_payout(self.db, 'PO-1042')['status'], 'FLAGGED')
        self.assertIsNone(payouts.get_payout(self.db, 'PO-1042')['execution_ref'])

    def _seed_flags(self):
        return {pid: flags for pid, flags in self.flags().items() if flags[0]}

    # FEATURE 16 — Scenario — seed flags, queue order and Sent words match main
    def test_seed_flags_queue_and_words_match_main(self):
        self.assertEqual(self._seed_flags(), {
            'PO-1037': ('BANK_CHANGED_7D', 'Payee bank details changed 1 day ago'),
            'PO-1038': ('AMOUNT_OVER_3X_AVG', 'Amount is 4× this payee’s usual payout ($520 avg)'),
            'PO-1042': ('AMOUNT_OVER_3X_AVG', 'Amount is 6× this payee’s usual payout ($790 avg)'),
            'PO-1043': ('BANK_CHANGED_7D', 'Payee bank details changed 2 days ago'),
            'PO-1044': ('DUPLICATE_24H', 'Same payee and amount as yesterday (possible duplicate)'),
        })
        unflagged = [p['payout_id'] for p in payouts.list_payouts(self.db) if not p['flag_code']]
        self.assertEqual(len(unflagged), 35)
        queue = payouts.current_queue(self.db)
        self.assertEqual([p['payout_id'] for p in queue], MAIN_QUEUE_IDS)
        sent = next(p for p in queue if p['payout_id'] == 'PO-1040')
        self.assertEqual(desk.STATUS_WORDS[sent['status']], ('Sent', 's-sent'))
        self.assertEqual(desk.STATUS_WORDS['FLAGGED'], ('Needs review', 's-review'))
        self.assertEqual(desk.STATUS_WORDS['HELD'], ('Held', 's-held'))
        self.assertEqual(desk.STATUS_WORDS['RETURNED'], ('Returned', 's-returned'))

    # FEATURE 17 — Scenario — simulated payouts flag the same as they did on main
    def test_simulated_payouts_flag_as_on_main(self):
        now = datetime(2026, 11, 10, 18, tzinfo=timezone.utc)
        paid_1000 = payout_rules.create(
            self.db, 'PO-SIM-1000', 'New Sim Payee', 'DEMO-NSIM-001', 100000, 'ACH', now=now)
        payout_rules.create(
            self.db, 'PO-SIM-100001', 'New Sim Payee 2', 'DEMO-NSIM-002', 100001, 'ACH', now=now)
        payout_rules.create(
            self.db, 'PO-SIM-CEDAR', 'Cedar Cleaning Co.', 'DEMO-CEDR-004', 64000, 'ACH', now=now)
        payout_rules.create(
            self.db, 'PO-SIM-CEDAR-BIG', 'Cedar Cleaning Co.', 'DEMO-CEDR-004', 200000, 'ACH', now=now)
        self.assertEqual(paid_1000['status'], 'PAID')
        self.assertIsNone(payouts.get_payout(self.db, 'PO-SIM-1000')['flag_code'])
        self.assertEqual(payouts.get_payout(self.db, 'PO-SIM-1000')['execution_status'], 'SETTLED')
        over_row = payouts.get_payout(self.db, 'PO-SIM-100001')
        self.assertEqual((over_row['status'], over_row['flag_code'], over_row['flag_text']),
                         ('FLAGGED', 'NEW_PAYEE_OVER_1000',
                          'No paid payout to this payee yet; amount is over $1,000'))
        cedar = payouts.get_payout(self.db, 'PO-SIM-CEDAR')
        self.assertEqual((cedar['status'], cedar['flag_code'], cedar['payee_usual_cents']),
                         ('PAID', None, 64000))
        big_row = payouts.get_payout(self.db, 'PO-SIM-CEDAR-BIG')
        self.assertEqual((big_row['status'], big_row['flag_code'], big_row['flag_text']),
                         ('FLAGGED', 'AMOUNT_OVER_3X_AVG',
                          'Amount is 3× this payee’s usual payout ($640 avg)'))
        # A later payout to the auto-paid new payee is no longer new, same as on main.
        later = payout_rules.create(
            self.db, 'PO-SIM-1000-LATER', 'New Sim Payee', 'DEMO-NSIM-001', 100001, 'ACH',
            now=datetime(2026, 11, 10, 19, tzinfo=timezone.utc))
        self.assertIsNone(payouts.get_payout(self.db, 'PO-SIM-1000-LATER')['flag_code'])
        self.assertEqual(later['status'], 'PAID')

    # FEATURE 18 — Scenario — Approve still emits one stream event, status FLAGGED to PAID
    def test_approve_stream_event_unchanged(self):
        payout_events.sync(self.db)
        before = {event['id']: event for event in payout_events.events_after(self.db, 0)}
        self.decide('PO-1042', 'approve', 'stream-ev')
        added = payout_events.sync(self.db)
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]['type'], 'status-changed')
        self.assertEqual(added[0]['payout_id'], 'PO-1042')
        self.assertEqual((added[0]['from_status'], added[0]['to_status']), ('FLAGGED', 'PAID'))
        after = payout_events.events_after(self.db, 0)
        self.assertEqual(sum(1 for event in after if event['type'] == 'status-changed'
                             and event['payout_id'] == 'PO-1042'), 1)
        self.assertTrue(all(event['id'] in before or event is added[0] or event['id'] == added[0]['id']
                            for event in after))

    # FEATURE 19 — Scenario — UNKNOWN cannot be submitted or retried automatically
    def test_unknown_cannot_be_submitted(self):
        with self.assertRaises(APIError) as caught:
            payouts.check_execution_transition('UNKNOWN', 'SUBMITTED')
        self.assertEqual(caught.exception.status, 409)
        self.assertIn('cannot be submitted or retried automatically', caught.exception.message)
        payouts.check_execution_transition('UNKNOWN', 'SETTLED')
        payouts.check_execution_transition('UNKNOWN', 'FAILED')

    # FEATURE 20 — Scenario — create and decide write review and outcome on new rows
    def test_create_and_decide_write_review_and_outcome(self):
        now = datetime(2026, 11, 10, 18, tzinfo=timezone.utc)
        payout_rules.create(
            self.db, 'PO-WRITE-NEW', 'Write Payee', 'DEMO-WRIT-001', 100001, 'ACH', now=now)
        flagged = payouts.get_payout(self.db, 'PO-WRITE-NEW')
        self.assertEqual((flagged['status'], flagged['review_status'], flagged['execution_status']),
                         ('FLAGGED', 'FLAGGED', 'NOT_STARTED'))
        self.decide('PO-WRITE-NEW', 'approve', 'write-ev')
        paid = payouts.get_payout(self.db, 'PO-WRITE-NEW')
        self.assertEqual((paid['status'], paid['review_status'], paid['execution_status']),
                         ('PAID', 'APPROVED', 'SETTLED'))
        payout_rules.create(
            self.db, 'PO-WRITE-CLEAN', 'Write Payee Clean', 'DEMO-WRIT-002', 100000, 'ACH', now=now)
        clean = payouts.get_payout(self.db, 'PO-WRITE-CLEAN')
        self.assertEqual((clean['status'], clean['review_status'], clean['execution_status']),
                         ('PAID', 'APPROVED', 'SETTLED'))
        self.assertEqual(clean['execution_ref'], 'demo:PO-WRITE-CLEAN')



class HostedDecisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(self.temp.name, SITE)

    def tearDown(self):
        self.app.extensions['close']()
        self.temp.cleanup()

    def client(self):
        client = self.app.test_client()
        self.assertEqual(client.get('/desk', base_url=SITE).status_code, 200)
        return client

    def post(self, client, path, body=None, key='k1', **kwargs):
        return client.post(path, json={} if body is None else body, base_url=SITE,
                           headers={'Idempotency-Key': key, 'Origin': SITE}, **kwargs)

    # FEATURE 09 — Scenario — the routes record decisions, replay retries, refuse bad moves
    def test_routes(self):
        client = self.client()
        actor = client.get('/operator', base_url=SITE).json['operator']['actor']
        held = self.post(client, '/payouts/PO-1043/hold')
        self.assertEqual(held.status_code, 201)
        self.assertEqual(held.json['payout']['status'], 'HELD')
        self.assertEqual(held.json['decision']['actor'], actor)
        self.assertIsNone(held.json['decision']['reason'])
        retry = self.post(client, '/payouts/PO-1043/hold')
        self.assertEqual((retry.status_code, retry.json), (200, held.json))
        sent = self.post(client, '/payouts/PO-1043/approve', {'reason': 'ok'}, 'k2')
        self.assertEqual((sent.status_code, sent.json['payout']['status']), (201, 'PAID'))
        refused = self.post(client, '/payouts/PO-1043/return', key='k3')
        self.assertEqual(refused.status_code, 409)
        self.assertIn('PAID', refused.json['error'])
        # Free-text reviewer is refused; empty body is accepted (actor from session).
        self.assertEqual(self.post(client, '/payouts/PO-1042/hold',
                                   {'reviewer': 'X'}, 'k4').status_code, 422)
        self.assertEqual(self.post(client, '/payouts/PO-1042/hold', {}, 'k4b').status_code, 201)
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
        foreign = client.post('/payouts/PO-1042/approve', json={}, base_url=SITE,
                              headers={'Idempotency-Key': 'k', 'Origin': 'https://evil.example'})
        self.assertEqual(foreign.status_code, 403)
        for db in Path(self.temp.name).rglob('payments.sqlite3'):
            self.assertEqual(payouts.list_events(db), [])

    # FEATURE 21 — Scenario — the JSON API returns evidence; the Desk does not show it
    def test_json_returns_evidence_desk_does_not(self):
        client = self.client()
        reply = self.post(client, '/payouts/PO-1042/approve', key='desk-ev')
        self.assertEqual(reply.status_code, 201)
        self.assertEqual(reply.json['payout']['status'], 'PAID')
        self.assertEqual(reply.json['payout']['review_status'], 'APPROVED')
        self.assertEqual(reply.json['payout']['execution_status'], 'SETTLED')
        self.assertIn('evidence', reply.json['decision'])
        self.assertEqual(reply.json['decision']['evidence']['rules_version'], payouts.RULES_VERSION)
        html = client.get('/desk', base_url=SITE).get_data(as_text=True)
        self.assertIn('Sent', html)
        self.assertIn('PO-1042', html)
        self.assertNotIn('rules_version', html)
        self.assertNotIn('desk-rules-1', html)
        self.assertNotIn('execution_ref', html)
        self.assertNotIn('demo:PO-1042', html)



if __name__ == '__main__':
    unittest.main()
