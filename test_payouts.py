# Automated checks for payouts.py: the payout table, every allowed and blocked status
# change, the fictional seed data (issue #34), and stored rules notes (issue #37).
"""Exercise the Disbursement Desk data model with temporary fictional databases."""
import hashlib
import json
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from itertools import product
from pathlib import Path
from unittest.mock import patch
import payouts
from hosted_app import create_app
from desk_common import APIError

# Every move the Desk allows; all other pairs of statuses must be blocked.
ALLOWED = {
    ('PENDING', 'FLAGGED'), ('PENDING', 'APPROVED'),
    ('FLAGGED', 'APPROVED'), ('FLAGGED', 'HELD'), ('FLAGGED', 'RETURNED'),
    ('HELD', 'APPROVED'), ('HELD', 'RETURNED'),
    ('APPROVED', 'PAID'),
}
# Names used by real banks or payment companies must never appear in the seed.
REAL_NAMES = ('chase', 'wells fargo', 'citi', 'bank of america', 'paypal', 'stripe', 'dwolla', 'venmo')


SEED_COUNT = 40  # 30 earlier PAID history payouts + the 10-payout current queue


def tripped_rules(payout, rows):
    """Which approved rules this seeded payout's stored facts trip (a reference check, not the engine)."""
    when = datetime.fromisoformat(payout['created_at'])
    tripped = []
    usual = payout['payee_usual_cents']
    if usual and payout['amount_cents'] > payouts.USUAL_MULTIPLIER * usual:
        tripped.append('AMOUNT_OVER_3X_AVG')
    changed = payout['bank_details_changed_at']
    if changed and timedelta(0) <= when - datetime.fromisoformat(changed) <= payouts.BANK_CHANGE_WINDOW:
        tripped.append('BANK_CHANGED_7D')
    if any(o['payout_id'] != payout['payout_id'] and o['payee_name'] == payout['payee_name']
           and o['amount_cents'] == payout['amount_cents']
           and timedelta(0) < when - datetime.fromisoformat(o['created_at']) <= payouts.DUPLICATE_WINDOW
           for o in rows):
        tripped.append('DUPLICATE_24H')
    return tripped


# The payouts table as the first version of this PR created it (no usual amount, no
# bank-details date), with one old-style seeded row.
OLD_SCHEMA = (
    """CREATE TABLE payouts (payout_id TEXT PRIMARY KEY, payee_name TEXT NOT NULL,
        payee_account TEXT NOT NULL CHECK(payee_account LIKE 'DEMO-%'), account_name TEXT NOT NULL,
        amount_cents INTEGER NOT NULL CHECK(amount_cents > 0), currency TEXT NOT NULL CHECK(currency = 'USD'),
        rail TEXT NOT NULL CHECK(rail IN ('ACH', 'RTP')),
        status TEXT NOT NULL CHECK(status IN ('PENDING', 'FLAGGED', 'APPROVED', 'HELD', 'RETURNED', 'PAID')),
        flag_code TEXT, flag_text TEXT, note_text TEXT,
        note_source TEXT CHECK(note_source IS NULL OR note_source IN ('ai', 'rules')),
        note_fallback INTEGER CHECK(note_fallback IS NULL OR note_fallback IN (0, 1)),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE payout_events (event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        payout_id TEXT NOT NULL REFERENCES payouts(payout_id), action TEXT NOT NULL,
        from_status TEXT NOT NULL, to_status TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT,
        idempotency_key TEXT UNIQUE, at TEXT NOT NULL)""",
    "CREATE TABLE keep_me (note TEXT)",
    "INSERT INTO keep_me VALUES ('untouched')",
    """INSERT INTO payouts (payout_id, payee_name, payee_account, account_name, amount_cents, currency, rail,
        status, flag_code, flag_text, created_at, updated_at) VALUES ('PO-1044', 'Northwind Freight',
        'DEMO-NWND-003', 'Northwind Freight', 120000, 'USD', 'ACH', 'FLAGGED', 'DUPLICATE_ACCOUNT',
        'Another payee already uses this destination account', '2026-10-28T14:00:00+00:00',
        '2026-10-28T14:00:00+00:00')""",
)


def make_old_demo(path, with_event=False):
    with sqlite3.connect(path) as db:
        for statement in OLD_SCHEMA:
            db.execute(statement)
        if with_event:
            db.execute("INSERT INTO payout_events (payout_id, action, from_status, to_status, actor, at) "
                       "VALUES ('PO-1044', 'hold', 'FLAGGED', 'HELD', 'Test Operator', '2026-10-28T12:00:00+00:00')")
    return path


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
        payouts._build_seed_rows.cache_clear()
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
        self.assertEqual(payouts.ensure_seeded(self.db), SEED_COUNT)
        self.assertEqual(payouts.ensure_seeded(other), SEED_COUNT)
        self.assertEqual(self.dump(self.db), self.dump(other))
        self.assertEqual(payouts.seed_rows(), payouts.seed_rows())
        fingerprint = hashlib.sha256(json.dumps(payouts.seed_rows(), sort_keys=True).encode()).hexdigest()
        self.assertEqual(fingerprint, PINNED_FINGERPRINT)

    # FEATURE 04b — Scenario — starting payouts are built once and handed out as copies
    # Two calls match row for row, but changing one returned list does not change the
    # next call. The sanctions-name check runs only while the starting set is first
    # built, not again on later seed_rows() or Reset calls (#9).
    def test_seed_rows_cached_and_copied(self):
        original = payouts.ofac.screen
        calls = []

        def counting(name):
            calls.append(name)
            return original(name)

        with patch('payouts.ofac.screen', side_effect=counting):
            payouts._build_seed_rows.cache_clear()
            first = payouts.seed_rows()
            built = len(calls)
            self.assertGreater(built, 0)
            second = payouts.seed_rows()
            self.assertEqual(len(calls), built)
            self.assertEqual(first, second)
            self.assertIsNot(first, second)
            first[0]['payee_name'] = 'mutated'
            self.assertNotEqual(first[0]['payee_name'], second[0]['payee_name'])
            self.assertEqual(payouts.seed_rows(), second)
            self.assertEqual(payouts.reset(self.db), SEED_COUNT)
            self.assertEqual(payouts.reset(self.db), SEED_COUNT)
            self.assertEqual(len(calls), built)

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
        self.assertEqual(payouts.reset(self.db), SEED_COUNT)
        self.assertEqual(self.dump(self.db), original)
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM payout_events').fetchone()[0], 0)

    # FEATURE 06 — Scenario — at least one payout for each seed rule, and the data backs it
    # The seed still covers the three original rules. The new-payee rule (#86) is listed
    # but has no seeded payout, so the starting set stays unchanged. Each flagged
    # payout's stored facts really trip its rule, and no unflagged payout trips any rule.
    def test_each_rule_has_a_payout(self):
        rows = payouts.seed_rows()
        self.assertEqual(set(payouts.FLAG_TEXT), {
            'AMOUNT_OVER_3X_AVG', 'BANK_CHANGED_7D', 'DUPLICATE_24H', 'NEW_PAYEE_OVER_1000',
            'POSSIBLE_SANCTIONS_MATCH'})
        for code in ('AMOUNT_OVER_3X_AVG', 'BANK_CHANGED_7D', 'DUPLICATE_24H'):
            with self.subTest(rule=code):
                self.assertTrue(any(r['flag_code'] == code and r['status'] == 'FLAGGED' for r in rows))
        self.assertFalse(any(r['flag_code'] == 'NEW_PAYEE_OVER_1000' for r in rows))
        for r in rows:
            codes = payouts.split_flag_codes(r['flag_code'])
            self.assertTrue(all(code in payouts.FLAG_TEXT for code in codes), r['flag_code'])
            self.assertEqual(r['flag_code'] is None, r['flag_text'] is None)
            if r['status'] in ('FLAGGED', 'HELD'):
                self.assertIsNotNone(r['flag_code'])
        for r in rows:
            with self.subTest(payout=r['payout_id']):
                self.assertEqual(sorted(tripped_rules(r, rows)), sorted(payouts.split_flag_codes(r['flag_code'])))

    # FEATURE 06b — Scenario — stored "usual" amounts equal the payee's earlier average
    def test_usual_amount_matches_history(self):
        rows = payouts.seed_rows()
        acme = next(r for r in rows if r['payout_id'] == 'PO-1042')
        self.assertEqual(acme['payee_usual_cents'], 79000)
        for r in rows:
            if r['payee_usual_cents'] is not None:
                earlier = [h['amount_cents'] for h in rows if h['payee_name'] == r['payee_name']
                           and h['created_at'] < r['created_at'] and h['payee_usual_cents'] is None]
                self.assertEqual(sum(earlier) / len(earlier), r['payee_usual_cents'])
                self.assertTrue(all(abs(c - r['payee_usual_cents']) <= r['payee_usual_cents'] // 10 for c in earlier))

    # FEATURE 07 — Scenario — the starting queue matches the approved Desk mock
    # 3 need review (one per rule) + 1 held = 4 exceptions, plus returned and sent ones.
    def test_queue_shape(self):
        rows = payouts.seed_rows()
        statuses = [r['status'] for r in rows]
        self.assertEqual(statuses.count('FLAGGED'), 3)
        self.assertEqual(statuses.count('HELD'), 1)
        self.assertEqual(statuses.count('RETURNED'), 1)
        self.assertEqual(len({r['payout_id'] for r in rows}), len(rows))
        mock = {r['payout_id']: (r['payee_name'], r['amount_cents'], r['status']) for r in rows}
        self.assertEqual(mock['PO-1042'], ('Acme Supplies', 485000, 'FLAGGED'))
        self.assertEqual(mock['PO-1043'], ('Bluebird Bakery', 31240, 'FLAGGED'))
        self.assertEqual(mock['PO-1044'], ('Northwind Freight', 120000, 'FLAGGED'))
        self.assertEqual(mock['PO-1038'], ('Harbor Hardware', 207550, 'HELD'))
        self.assertEqual(mock['PO-1037'], ('Maple Tutoring LLC', 45000, 'RETURNED'))

    # FEATURE 07b — Scenario — no seeded payout is PENDING or APPROVED
    # Viewers only ever see Needs review, Held, Returned and Sent (issue #35 decision log).
    def test_no_pending_or_approved_in_seed(self):
        self.assertEqual({r['status'] for r in payouts.seed_rows()}, {'FLAGGED', 'HELD', 'RETURNED', 'PAID'})
        payouts.ensure_seeded(self.db)
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM payouts WHERE status IN ('PENDING', 'APPROVED')")
                             .fetchone()[0], 0)

    # FEATURE 07c — Scenario — the bank-details date is a real, seeded column
    def test_bank_details_column_seeded(self):
        payouts.ensure_seeded(self.db)
        with sqlite3.connect(self.db) as db:
            columns = {row[1] for row in db.execute('PRAGMA table_info(payouts)')}
            self.assertIn('bank_details_changed_at', columns)
            changed = dict(db.execute('SELECT payout_id, bank_details_changed_at FROM payouts '
                                      'WHERE bank_details_changed_at IS NOT NULL').fetchall())
        self.assertEqual(changed['PO-1043'], '2026-10-26T15:00:00+00:00')
        self.assertEqual(payouts.get_payout(self.db, 'PO-1043')['flag_text'], 'Payee bank details changed 2 days ago')

    # FEATURE 07d — Scenario — a demo made by the older version is upgraded in place
    # Older demos have a payouts table without the new columns. Opening one adds them
    # (only once), keeps payments untouched, and swaps in the current starting set while
    # no operator has acted. With action-log rows present, the payouts are kept.
    def test_upgrade_older_demo(self):
        make_old_demo(self.db)
        self.assertEqual(payouts.ensure_seeded(self.db), SEED_COUNT)
        self.assertEqual(payouts.ensure_seeded(self.db), 0)  # idempotent
        with sqlite3.connect(self.db) as db:
            columns = [row[1] for row in db.execute('PRAGMA table_info(payouts)')]
            self.assertEqual(columns.count('bank_details_changed_at'), 1)
            self.assertEqual(db.execute('SELECT note FROM keep_me').fetchone()[0], 'untouched')
        self.assertEqual(self.dump(self.db), json.dumps(payouts.list_payouts(self.fresh()), sort_keys=True))
        self.assertNotIn('DUPLICATE_ACCOUNT', self.dump(self.db))
        # With operator history, rows are kept and just gain the empty columns.
        acted = Path(self.temp.name) / 'acted.sqlite3'
        make_old_demo(acted, with_event=True)
        self.assertEqual(payouts.ensure_seeded(acted), 0)
        kept = payouts.get_payout(acted, 'PO-1044')
        self.assertEqual((kept['flag_code'], kept['bank_details_changed_at']), ('DUPLICATE_ACCOUNT', None))
        self.assertEqual(payouts.reset(acted), SEED_COUNT)
        self.assertEqual(payouts.get_payout(acted, 'PO-1044')['flag_code'], 'DUPLICATE_24H')

    def fresh(self):
        path = Path(self.temp.name) / 'fresh.sqlite3'
        payouts.ensure_seeded(path)
        return path

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
            self.assertEqual(r['note_source'], 'rules')
            self.assertTrue(r['note_text'])
            self.assertIs(r['note_fallback'], False)

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
            self.assertEqual(client.get('/desk', base_url='https://demo.example').status_code, 200)
        databases = sorted(Path(self.temp.name).rglob('payments.sqlite3'))
        self.assertEqual(len(databases), 2)
        with sqlite3.connect(databases[0]) as db:
            db.execute("UPDATE payouts SET status = 'RETURNED' WHERE payout_id = 'PO-1044'")
        self.assertEqual(payouts.get_payout(databases[0], 'PO-1044')['status'], 'RETURNED')
        self.assertEqual(payouts.get_payout(databases[1], 'PO-1044')['status'], 'FLAGGED')
        self.assertEqual(len(payouts.list_payouts(databases[1])), SEED_COUNT)

    # FEATURE 12 — Scenario — payouts show only on the Desk, its POST routes, the stream,
    # and the behind-the-scenes review-reasons GET (#32).
    # The payout APIs are the decision route (#35, clear/block in #111), Reset demo (#32 slice 4), the
    # live update stream (#43), simulate (#91; Desk New payout posts via desk.js),
    # the behind-the-scenes execution replay route (#56), and the behind-the-scenes
    # review-reasons GET. /dashboard is not a route here (the Lab dashboard stayed
    # behind). HTML has no simulate/execution-replay/review-reasons link; desk.js posts
    # simulate with Idempotency-Key. Route pin includes /v1 create+decide and /openapi.json.
    def test_no_new_routes(self):
        client = self.app.test_client()
        self.assertEqual(client.get('/dashboard', base_url='https://demo.example').status_code, 404)
        root = client.get('/', base_url='https://demo.example', follow_redirects=True)
        self.assertEqual(root.status_code, 200)
        desk_slash = client.get('/desk/', base_url='https://demo.example')
        self.assertEqual(desk_slash.status_code, 200)
        for page in (root.get_data(as_text=True), desk_slash.get_data(as_text=True)):
            self.assertNotIn('/payouts/simulate', page)
            self.assertNotIn('/payouts/execution-replay', page)
            self.assertNotIn('review-reasons', page)
        script = client.get('/desk.js', base_url='https://demo.example').get_data(as_text=True)
        self.assertIn("fetch('/payouts/simulate'", script)
        self.assertIn('Idempotency-Key', script)
        rules = [rule for rule in self.app.url_map.iter_rules() if re.search('payout', str(rule))]
        self.assertEqual({str(r) for r in rules},
                         {'/payouts/<payout_id>/<any(approve, hold, return, clear, block):action>',
                          '/payouts/<payout_id>/review-reasons',
                          '/payouts/reset', '/payouts/simulate', '/payouts/stream',
                          '/payouts/execution-replay',
                          '/v1/payouts', '/v1/payouts/<payout_id>/decisions'})
        posts = {str(r) for r in rules if (r.methods - {'OPTIONS'}) == {'POST'}}
        self.assertEqual(posts, {'/payouts/<payout_id>/<any(approve, hold, return, clear, block):action>',
                                 '/payouts/reset', '/payouts/simulate',
                                 '/payouts/execution-replay',
                                 '/v1/payouts', '/v1/payouts/<payout_id>/decisions'})
        stream = next(r for r in rules if str(r) == '/payouts/stream')
        self.assertEqual(stream.methods - {'OPTIONS', 'HEAD'}, {'GET'})
        reasons = next(r for r in rules if str(r) == '/payouts/<payout_id>/review-reasons')
        self.assertEqual(reasons.methods - {'OPTIONS', 'HEAD'}, {'GET'})
        self.assertEqual({str(r) for r in self.app.url_map.iter_rules() if 'desk' in str(r)}, {'/desk', '/desk/', '/desk.js'})
        expected = {
            ('/', frozenset({'GET'})),
            ('/desk', frozenset({'GET'})),
            ('/desk/', frozenset({'GET'})),
            ('/desk.js', frozenset({'GET'})),
            ('/health', frozenset({'GET'})),
            ('/ready', frozenset({'GET'})),
            ('/metrics', frozenset({'GET'})),
            ('/openapi.json', frozenset({'GET'})),
            ('/operator', frozenset({'GET'})),
            ('/demo-scenario', frozenset({'POST'})),
            ('/payouts/<payout_id>/<any(approve, hold, return, clear, block):action>', frozenset({'POST'})),
            ('/payouts/<payout_id>/review-reasons', frozenset({'GET'})),
            ('/payouts/reset', frozenset({'POST'})),
            ('/payouts/execution-replay', frozenset({'POST'})),
            ('/payouts/simulate', frozenset({'POST'})),
            ('/payouts/stream', frozenset({'GET'})),
            ('/reconciliation', frozenset({'POST'})),
            ('/stripe/webhook', frozenset({'POST'})),
            ('/v1/payouts', frozenset({'POST'})),
            ('/v1/payouts/<payout_id>/decisions', frozenset({'POST'})),
        }
        app_rules = [r for r in self.app.url_map.iter_rules() if r.endpoint != 'static']
        self.assertEqual(len(app_rules), 20)
        self.assertEqual({(str(r), frozenset(r.methods - {'HEAD', 'OPTIONS'})) for r in app_rules}, expected)
        self.assertEqual(client.get('/payouts', base_url='https://demo.example').status_code, 404)
        self.assertEqual(client.get('/payouts/reset', base_url='https://demo.example').status_code, 405)
        self.assertEqual(client.get('/payouts/simulate', base_url='https://demo.example').status_code, 405)
        self.assertEqual(client.get('/payouts/execution-replay', base_url='https://demo.example').status_code, 405)
        self.assertEqual(client.get('/payouts/stream', base_url='https://demo.example').status_code, 200)


# sha256 of the seed rows as sorted JSON; update only on purpose when the seed changes.
PINNED_FINGERPRINT = 'aa63fcc3867818fe31af81d958bd4348c37d2f3086bf63e0279858e0efbda690'
