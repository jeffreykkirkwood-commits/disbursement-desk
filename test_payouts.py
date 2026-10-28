# Automated checks for payouts.py: the payout table, every allowed and blocked status
# change, and the fictional seed data (issue #34).
"""Exercise the Disbursement Desk data model with temporary fictional databases."""
import hashlib
import json
import re
import sqlite3
import tempfile
import unittest
from itertools import product
from pathlib import Path
import payouts
from hosted_app import create_app
from payments import APIError

# Every move the Desk allows; all other pairs of statuses must be blocked.
ALLOWED = {
    ('PENDING', 'FLAGGED'), ('PENDING', 'APPROVED'),
    ('FLAGGED', 'APPROVED'), ('FLAGGED', 'HELD'), ('FLAGGED', 'RETURNED'),
    ('HELD', 'APPROVED'), ('HELD', 'RETURNED'),
    ('APPROVED', 'PAID'),
}
# Names used by real banks or payment companies must never appear in the seed.
REAL_NAMES = ('chase', 'wells fargo', 'citi', 'bank of america', 'paypal', 'stripe', 'dwolla', 'venmo')


class TransitionTests(unittest.TestCase):
    # FEATURE 01 — Scenario — the six statuses and the transition table agree
    def test_statuses_listed(self):
        self.assertEqual(set(payouts.STATUSES),
                         {'PENDING', 'FLAGGED', 'APPROVED', 'HELD', 'RETURNED', 'PAID'})
        self.assertEqual(set(payouts.TRANSITIONS), set(payouts.STATUSES))
        self.assertEqual(payouts.FINAL_STATUSES, {'RETURNED', 'PAID'})

    # FEATURE 02 — Scenario — every allowed and every blocked move (36 pairs)
    def test_every_pair(self):
        for current, target in product(payouts.STATUSES, repeat=2):
            with self.subTest(current=current, target=target):
                if (current, target) in ALLOWED:
                    self.assertTrue(payouts.can_transition(current, target))
                    payouts.check_transition(current, target)  # no error
                else:
                    self.assertFalse(payouts.can_transition(current, target))
                    with self.assertRaises(APIError) as caught:
                        payouts.check_transition(current, target)
                    self.assertEqual(caught.exception.status, 409)
                    self.assertIn(current, caught.exception.message)

    # FEATURE 03 — Scenario — unknown statuses are refused, not guessed
    def test_unknown_status(self):
        self.assertFalse(payouts.can_transition('SENT', 'PAID'))
        for pair in (('SENT', 'PAID'), ('FLAGGED', 'SENT')):
            with self.assertRaises(APIError) as caught:
                payouts.check_transition(*pair)
            self.assertEqual(caught.exception.status, 422)


class SeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'payments.sqlite3'

    def tearDown(self):
        self.temp.cleanup()

    def dump(self, path):
        return json.dumps(payouts.list_payouts(path), sort_keys=True)

    # FEATURE 04 — Scenario — two boots give identical data
    # Two separate databases seeded independently must match row for row, and the seed
    # fingerprint is pinned so a change on any Python version (CI runs 3.10 and 3.13)
    # is caught.
    def test_deterministic(self):
        other = Path(self.temp.name) / 'second.sqlite3'
        self.assertEqual(payouts.ensure_seeded(self.db), 25)
        self.assertEqual(payouts.ensure_seeded(other), 25)
        self.assertEqual(self.dump(self.db), self.dump(other))
        self.assertEqual(payouts.seed_rows(), payouts.seed_rows())
        fingerprint = hashlib.sha256(json.dumps(payouts.seed_rows(), sort_keys=True).encode()).hexdigest()
        self.assertEqual(fingerprint, PINNED_FINGERPRINT)

    # FEATURE 05 — Scenario — reopening keeps changes; reset restores the start
    def test_reopen_and_reset(self):
        payouts.ensure_seeded(self.db)
        original = self.dump(self.db)
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE payouts SET status = 'HELD' WHERE payout_id = 'PO-1042'")
            db.execute("INSERT INTO payout_events (payout_id, action, from_status, to_status, actor, at) "
                       "VALUES ('PO-1042', 'hold', 'FLAGGED', 'HELD', 'Test Operator', '2026-10-28T12:00:00+00:00')")
        self.assertEqual(payouts.ensure_seeded(self.db), 0)  # restart: nothing reseeded
        self.assertEqual(payouts.get_payout(self.db, 'PO-1042')['status'], 'HELD')
        self.assertEqual(payouts.reset(self.db), 25)
        self.assertEqual(self.dump(self.db), original)
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM payout_events').fetchone()[0], 0)

    # FEATURE 06 — Scenario — at least one payout for each exception rule
    def test_each_rule_has_a_payout(self):
        rows = payouts.seed_rows()
        for code in payouts.FLAG_TEXT:
            with self.subTest(rule=code):
                self.assertTrue(any(r['flag_code'] == code and r['status'] == 'FLAGGED' for r in rows))
        for r in rows:
            self.assertEqual(r['flag_text'], payouts.FLAG_TEXT.get(r['flag_code']))
            if r['status'] in ('FLAGGED', 'HELD'):
                self.assertIsNotNone(r['flag_code'])
        # The seeded data really trips each rule as described.
        by_code = {r['flag_code']: r for r in rows if r['status'] == 'FLAGGED'}
        over = by_code['AMOUNT_OVER_THRESHOLD']
        self.assertGreater(over['amount_cents'], payouts.AMOUNT_THRESHOLD * 100)
        mismatch = by_code['PAYEE_NAME_MISMATCH']
        self.assertNotEqual(mismatch['payee_name'], mismatch['account_name'])
        dup = by_code['DUPLICATE_ACCOUNT']
        self.assertTrue(any(r['payee_account'] == dup['payee_account'] and r['payee_name'] != dup['payee_name']
                            for r in rows))
        # Clean payouts stay under the threshold with matching names.
        for r in rows:
            if r['flag_code'] is None:
                self.assertLessEqual(r['amount_cents'], payouts.AMOUNT_THRESHOLD * 100)
                self.assertEqual(r['payee_name'], r['account_name'])

    # FEATURE 07 — Scenario — the starting queue matches the approved Desk mock
    # 3 need review + 1 held = 4 exceptions; every status appears at least once.
    def test_queue_shape(self):
        rows = payouts.seed_rows()
        statuses = [r['status'] for r in rows]
        self.assertEqual(statuses.count('FLAGGED'), 3)
        self.assertEqual(statuses.count('HELD'), 1)
        self.assertEqual(set(statuses), set(payouts.STATUSES))
        self.assertEqual(len({r['payout_id'] for r in rows}), len(rows))

    # FEATURE 08 — Scenario — only fictional names and DEMO accounts
    def test_fictional_only(self):
        payouts.ensure_seeded(self.db)
        for r in payouts.list_payouts(self.db):
            self.assertRegex(r['payee_account'], r'^DEMO-[A-Z]{4}-\d{3}$')
            self.assertEqual(r['currency'], 'USD')
            self.assertIn(r['rail'], ('ACH', 'RTP'))
            self.assertRegex(r['amount'], r'^\d+\.\d{2}$')
            for name in (r['payee_name'], r['account_name']):
                self.assertFalse(any(real in name.lower() for real in REAL_NAMES), name)
            self.assertIsNone(r['note_text'])  # notes are built later (#37)

    # FEATURE 09 — Scenario — the database refuses non-DEMO accounts and bad statuses
    def test_constraints(self):
        payouts.ensure_seeded(self.db)
        with sqlite3.connect(self.db) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE payouts SET status = 'SENT' WHERE payout_id = 'PO-1042'")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE payouts SET payee_account = '123456789' WHERE payout_id = 'PO-1042'")

    # FEATURE 10 — Scenario — read helpers
    def test_get_payout(self):
        payouts.ensure_seeded(self.db)
        acme = payouts.get_payout(self.db, 'PO-1042')
        self.assertEqual((acme['payee_name'], acme['amount'], acme['status']),
                         ('Acme Supplies', '4850.00', 'FLAGGED'))
        with self.assertRaises(APIError) as caught:
            payouts.get_payout(self.db, 'PO-9999')
        self.assertEqual(caught.exception.status, 404)


class HostedSeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(self.temp.name, 'https://demo.example')

    def tearDown(self):
        self.app.extensions['close']()
        self.temp.cleanup()

    # FEATURE 11 — Scenario — each visitor demo gets its own seeded copy
    # Changing one visitor's payouts leaves a second browser's demo untouched.
    def test_per_viewer_seed(self):
        first, second = self.app.test_client(), self.app.test_client()
        for client in (first, second):
            self.assertEqual(client.get('/dashboard', base_url='https://demo.example').status_code, 200)
        databases = sorted(Path(self.temp.name).rglob('payments.sqlite3'))
        self.assertEqual(len(databases), 2)
        with sqlite3.connect(databases[0]) as db:
            db.execute("UPDATE payouts SET status = 'RETURNED' WHERE payout_id = 'PO-1044'")
        self.assertEqual(payouts.get_payout(databases[0], 'PO-1044')['status'], 'RETURNED')
        self.assertEqual(payouts.get_payout(databases[1], 'PO-1044')['status'], 'FLAGGED')
        self.assertEqual(len(payouts.list_payouts(databases[1])), 25)

    # FEATURE 12 — Scenario — nothing visible changes: no payout route, no Desk text
    def test_no_new_routes(self):
        client = self.app.test_client()
        page = client.get('/dashboard', base_url='https://demo.example').get_data(as_text=True)
        self.assertNotIn('PO-10', page)
        rules = {str(rule) for rule in self.app.url_map.iter_rules()}
        self.assertFalse([r for r in rules if re.search('payout|desk', r)], rules)
        self.assertEqual(client.get('/payouts', base_url='https://demo.example').status_code, 404)


# sha256 of the seed rows as sorted JSON; update only on purpose when the seed changes.
PINNED_FINGERPRINT = '79f5d331dc1b28649f2dab6c529aa3622c64de9cc6902020097bae2eb8a398cf'
