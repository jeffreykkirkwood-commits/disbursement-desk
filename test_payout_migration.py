# Automated checks for the #54 schema migration: review_status and
# execution_status on payouts, the three new tables, backfill from today's
# status, a fresh seed matching an upgraded demo, no stream burst, and Reset.
"""Upgrade an existing demo and a fresh seed to the #54 payout state columns."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import desk
import payout_events
import payouts


SEED_COUNT = 40
PRE54_COLUMNS = (
    'payout_id', 'payee_name', 'payee_account', 'account_name', 'amount_cents', 'currency',
    'rail', 'status', 'flag_code', 'flag_text', 'payee_usual_cents', 'bank_details_changed_at',
    'note_text', 'note_source', 'note_fallback', 'created_at', 'updated_at')
NEW_COLUMNS = ('review_status', 'execution_status', 'execution_ref', 'execution_updated_at')
NEW_TABLES = ('execution_events', 'reconciliation_runs', 'reconciliation_items')
PRE54_SCHEMA = (
    """CREATE TABLE payouts (
        payout_id TEXT PRIMARY KEY,
        payee_name TEXT NOT NULL,
        payee_account TEXT NOT NULL CHECK(payee_account LIKE 'DEMO-%'),
        account_name TEXT NOT NULL,
        amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
        currency TEXT NOT NULL CHECK(currency = 'USD'),
        rail TEXT NOT NULL CHECK(rail IN ('ACH', 'RTP')),
        status TEXT NOT NULL CHECK(status IN ('PENDING', 'FLAGGED', 'APPROVED', 'HELD', 'RETURNED', 'PAID')),
        flag_code TEXT,
        flag_text TEXT,
        payee_usual_cents INTEGER CHECK(payee_usual_cents IS NULL OR payee_usual_cents > 0),
        bank_details_changed_at TEXT,
        note_text TEXT,
        note_source TEXT CHECK(note_source IS NULL OR note_source IN ('ai', 'rules')),
        note_fallback INTEGER CHECK(note_fallback IS NULL OR note_fallback IN (0, 1)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL)""",
    """CREATE TABLE payout_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        payout_id TEXT NOT NULL REFERENCES payouts(payout_id),
        action TEXT NOT NULL,
        from_status TEXT NOT NULL,
        to_status TEXT NOT NULL,
        actor TEXT NOT NULL,
        reason TEXT,
        idempotency_key TEXT UNIQUE,
        at TEXT NOT NULL)""",
)


def payout_schema(path):
    with sqlite3.connect(path) as db:
        tables = {name for (name,) in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        columns = [row[1] for row in db.execute('PRAGMA table_info(payouts)')]
        execution_event_cols = [row[1] for row in db.execute('PRAGMA table_info(execution_events)')]
    return tables, columns, execution_event_cols


def statuses(path):
    with sqlite3.connect(path) as db:
        return dict(db.execute('SELECT payout_id, status FROM payouts'))


def expected_state(status):
    return payouts.review_and_outcome(status)


def make_pre54_demo(path, keep_rows=False, extras=()):
    """Build a demo as it existed before #54 (today's payouts table, no new fields)."""
    with sqlite3.connect(path) as db:
        for statement in PRE54_SCHEMA:
            db.execute(statement)
        rows = payouts.seed_rows()
        db.executemany(
            f'INSERT INTO payouts ({", ".join(PRE54_COLUMNS)}) VALUES ({", ".join("?" * len(PRE54_COLUMNS))})',
            [tuple(row[c] for c in PRE54_COLUMNS) for row in rows])
        for extra in extras:
            db.execute(
                f'INSERT INTO payouts ({", ".join(PRE54_COLUMNS)}) VALUES ({", ".join("?" * len(PRE54_COLUMNS))})',
                tuple(extra[c] for c in PRE54_COLUMNS))
        if keep_rows:
            first = rows[0]
            db.execute(
                "INSERT INTO payout_events (payout_id, action, from_status, to_status, actor, at) "
                "VALUES (?, 'hold', 'FLAGGED', 'HELD', 'Test Operator', ?)",
                (first['payout_id'], first['updated_at']))
    return path


def extra_row(payout_id, status, payee='Migration Payee', account='DEMO-MIG-001',
              cents=100001, when='2026-11-10T18:00:00+00:00'):
    return dict(
        payout_id=payout_id, payee_name=payee, payee_account=account, account_name=payee,
        amount_cents=cents, currency='USD', rail='ACH', status=status, flag_code=None,
        flag_text=None, payee_usual_cents=None, bank_details_changed_at=None,
        note_text=None, note_source=None, note_fallback=None, created_at=when, updated_at=when)


def write_remembered_stream(database, status_map):
    events = []
    for index, payout_id in enumerate(sorted(status_map), start=1):
        events.append({
            'id': index, 'type': 'created', 'payout_id': payout_id,
            'status': status_map[payout_id], 'payee_name': 'Seed',
            'amount': '0.00', 'currency': 'USD', 'rail': 'ACH',
            'at': '2026-10-01T14:00:00+00:00',
        })
    state = {'next_id': len(events) + 1, 'statuses': dict(status_map), 'events': events}
    payout_events._path(database).write_text(json.dumps(state))
    return state


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'payments.sqlite3'

    def tearDown(self):
        self.temp.cleanup()

    def fresh(self):
        path = Path(self.temp.name) / 'fresh.sqlite3'
        payouts.ensure_seeded(path)
        return path

    def counts(self, path):
        with sqlite3.connect(path) as db:
            return {
                'payouts': db.execute('SELECT COUNT(*) FROM payouts').fetchone()[0],
                'execution_events': db.execute('SELECT COUNT(*) FROM execution_events').fetchone()[0],
                'reconciliation_runs': db.execute('SELECT COUNT(*) FROM reconciliation_runs').fetchone()[0],
                'reconciliation_items': db.execute('SELECT COUNT(*) FROM reconciliation_items').fetchone()[0],
            }

    def assert_schema(self, path):
        tables, columns, event_cols = payout_schema(path)
        self.assertTrue(set(NEW_TABLES).issubset(tables))
        for name in NEW_COLUMNS:
            self.assertEqual(columns.count(name), 1, name)
        self.assertEqual(event_cols[0], 'event_id')
        with sqlite3.connect(path) as db:
            sql = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='execution_events'"
            ).fetchone()[0]
        self.assertIn('event_id TEXT PRIMARY KEY', sql)

    def assert_mapped(self, path):
        for payout in payouts.list_payouts(path):
            review, outcome = expected_state(payout['status'])
            self.assertEqual(payout['review_status'], review, payout['payout_id'])
            self.assertEqual(payout['execution_status'], outcome, payout['payout_id'])
            self.assertIsNone(payout['execution_ref'])
            self.assertIsNone(payout['execution_updated_at'])

    # FEATURE 01 — Scenario — the status mapping matches the state contract
    def test_mapping(self):
        self.assertEqual(payouts.review_and_outcome('PAID'), ('APPROVED', 'SETTLED'))
        self.assertEqual(payouts.review_and_outcome('FLAGGED'), ('FLAGGED', 'NOT_STARTED'))
        self.assertEqual(payouts.review_and_outcome('APPROVED'), ('APPROVED', 'NOT_STARTED'))
        self.assertEqual(payouts.review_and_outcome('PENDING'), ('PENDING', 'NOT_STARTED'))
        self.assertEqual(payouts.review_and_outcome('HELD'), ('HELD', 'NOT_STARTED'))
        self.assertEqual(payouts.review_and_outcome('RETURNED'), ('RETURNED', 'NOT_STARTED'))

    # FEATURE 02 — Scenario — a fresh boot has the new schema, mapped seed, and empty new tables
    def test_fresh_seed_has_mapped_fields(self):
        self.assertEqual(payouts.ensure_seeded(self.db), SEED_COUNT)
        self.assert_schema(self.db)
        self.assertEqual(self.counts(self.db)['payouts'], SEED_COUNT)
        self.assertEqual(self.counts(self.db)['execution_events'], 0)
        self.assertEqual(self.counts(self.db)['reconciliation_runs'], 0)
        self.assertEqual(self.counts(self.db)['reconciliation_items'], 0)
        self.assert_mapped(self.db)
        paid = [p for p in payouts.list_payouts(self.db) if p['status'] == 'PAID']
        flagged = [p for p in payouts.list_payouts(self.db) if p['status'] == 'FLAGGED']
        self.assertEqual(len(paid), 35)  # 30 history + 5 current-queue Sent
        self.assertTrue(all(p['review_status'] == 'APPROVED' and p['execution_status'] == 'SETTLED'
                            for p in paid))
        self.assertTrue(all(p['review_status'] == 'FLAGGED' and p['execution_status'] == 'NOT_STARTED'
                            for p in flagged))
        held = payouts.get_payout(self.db, 'PO-1038')
        returned = payouts.get_payout(self.db, 'PO-1037')
        self.assertEqual((held['review_status'], held['execution_status']),
                         ('HELD', 'NOT_STARTED'))
        self.assertEqual((returned['review_status'], returned['execution_status']),
                         ('RETURNED', 'NOT_STARTED'))

    # FEATURE 03 — Scenario — two fresh boots are identical, including the new fields
    def test_two_boots_are_identical(self):
        payouts.ensure_seeded(self.db)
        other = self.fresh()
        self.assertEqual(json.dumps(payouts.list_payouts(self.db), sort_keys=True),
                         json.dumps(payouts.list_payouts(other), sort_keys=True))
        self.assertEqual(payout_schema(self.db), payout_schema(other))
        self.assertEqual(self.counts(self.db), self.counts(other))

    # FEATURE 04 — Scenario — a fresh boot and an upgraded existing demo share the same schema
    def test_existing_demo_and_fresh_boot_share_schema(self):
        make_pre54_demo(self.db, keep_rows=True)
        self.assertEqual(payouts.ensure_seeded(self.db), 0)
        fresh = self.fresh()
        _, old_cols, old_events = payout_schema(self.db)
        _, new_cols, new_events = payout_schema(fresh)
        self.assertEqual(old_cols, new_cols)
        self.assertEqual(old_events, new_events)
        self.assertEqual(payout_schema(self.db)[0] & set(NEW_TABLES),
                         payout_schema(fresh)[0] & set(NEW_TABLES))

    # FEATURE 05 — Scenario — opening an existing demo backfills without rewriting status
    def test_existing_demo_is_backfilled_and_status_is_unchanged(self):
        extras = (
            extra_row('PO-1046', 'PAID', payee='Auto Paid Payee', cents=64000),
            extra_row('PO-1047', 'FLAGGED', payee='Flagged Payee'),
            extra_row('PO-1048', 'APPROVED', payee='Approved Payee', cents=64000),
        )
        make_pre54_demo(self.db, keep_rows=True, extras=extras)
        before = statuses(self.db)
        self.assertNotIn('review_status', payout_schema(self.db)[1])
        self.assertEqual(payouts.ensure_seeded(self.db), 0)
        self.assertEqual(payouts.ensure_seeded(self.db), 0)  # running it twice changes nothing
        self.assertEqual(statuses(self.db), before)
        self.assert_schema(self.db)
        self.assert_mapped(self.db)
        auto = payouts.get_payout(self.db, 'PO-1046')
        flagged = payouts.get_payout(self.db, 'PO-1047')
        approved = payouts.get_payout(self.db, 'PO-1048')
        self.assertEqual((auto['status'], auto['review_status'], auto['execution_status']),
                         ('PAID', 'APPROVED', 'SETTLED'))
        self.assertEqual((flagged['status'], flagged['review_status'], flagged['execution_status']),
                         ('FLAGGED', 'FLAGGED', 'NOT_STARTED'))
        self.assertEqual((approved['status'], approved['review_status'], approved['execution_status']),
                         ('APPROVED', 'APPROVED', 'NOT_STARTED'))
        first = json.dumps(payouts.list_payouts(self.db), sort_keys=True)
        self.assertEqual(json.dumps(payouts.list_payouts(self.db), sort_keys=True), first)

    # FEATURE 06 — Scenario — migration does not emit a burst of stream events
    def test_migration_does_not_emit_stream_events(self):
        make_pre54_demo(self.db, keep_rows=True)
        before = statuses(self.db)
        remembered = write_remembered_stream(self.db, before)
        before_ids = [event['id'] for event in remembered['events']]
        self.assertEqual(payouts.ensure_seeded(self.db), 0)
        self.assertEqual(statuses(self.db), before)
        added = payout_events.sync(self.db)
        self.assertEqual(added, [])
        state = json.loads(payout_events._path(self.db).read_text())
        self.assertEqual(state['statuses'], remembered['statuses'])
        self.assertEqual([event['id'] for event in state['events']], before_ids)
        self.assertFalse(any(event['type'] == 'status-changed' for event in state['events']))

    # FEATURE 07 — Scenario — Reset demo restores the seed and clears the new tables
    def test_reset_clears_new_tables_and_restores_the_seed(self):
        payouts.ensure_seeded(self.db)
        with sqlite3.connect(self.db) as db:
            db.execute(
                "INSERT INTO execution_events (event_id, payout_id, execution_status, at, source) "
                "VALUES ('evt-1', 'PO-1040', 'SUBMITTED', '2026-11-10T18:00:00+00:00', 'replay')")
            db.execute(
                "INSERT INTO reconciliation_runs (started_at, ended_at, fixture_name, "
                "matched_count, mismatched_count, unknown_count) "
                "VALUES ('2026-11-10T18:00:00+00:00', '2026-11-10T18:01:00+00:00', 'settlement_demo.csv', 1, 0, 0)")
            run_id = db.execute('SELECT run_id FROM reconciliation_runs').fetchone()[0]
            db.execute(
                "INSERT INTO reconciliation_items (run_id, payout_id, our_status, settlement_status, "
                "result, reason) VALUES (?, 'PO-1040', 'SUBMITTED', 'SETTLED', 'status_mismatch', "
                "'outcome does not match settlement')",
                (run_id,))
        self.assertEqual(self.counts(self.db)['execution_events'], 1)
        self.assertEqual(payouts.reset(self.db), SEED_COUNT)
        self.assertEqual(self.counts(self.db), {
            'payouts': SEED_COUNT, 'execution_events': 0,
            'reconciliation_runs': 0, 'reconciliation_items': 0})
        self.assertEqual(payouts.list_events(self.db), [])
        self.assert_mapped(self.db)
        self.assertEqual(json.dumps(payouts.list_payouts(self.db), sort_keys=True),
                         json.dumps(payouts.list_payouts(self.fresh()), sort_keys=True))

    # FEATURE 08 — Scenario — event_id in execution_events cannot be stored twice
    def test_execution_event_id_is_unique(self):
        payouts.ensure_seeded(self.db)
        row = ('evt-dup', 'PO-1040', 'SUBMITTED', '2026-11-10T18:00:00+00:00', 'replay')
        with sqlite3.connect(self.db) as db:
            db.execute(
                "INSERT INTO execution_events (event_id, payout_id, execution_status, at, source) "
                "VALUES (?, ?, ?, ?, ?)", row)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    "INSERT INTO execution_events (event_id, payout_id, execution_status, at, source) "
                    "VALUES (?, ?, ?, ?, ?)", row)

    # FEATURE 09 — Scenario — the queue order and Sent word still come from today's status
    def test_queue_and_sent_word_still_read_status(self):
        payouts.ensure_seeded(self.db)
        queue = payouts.current_queue(self.db)
        self.assertEqual(
            [p['payout_id'] for p in queue],
            ['PO-1042', 'PO-1043', 'PO-1044', 'PO-1038', 'PO-1037',
             'PO-1036', 'PO-1039', 'PO-1040', 'PO-1041', 'PO-1045'])
        sent = next(p for p in queue if p['payout_id'] == 'PO-1040')
        self.assertEqual(sent['status'], 'PAID')
        self.assertEqual(desk.STATUS_WORDS[sent['status']], ('Sent', 's-sent'))
        self.assertEqual(desk.STATUS_WORDS['APPROVED'], ('Sent', 's-sent'))
        # The screen word is taken from status, not from the payment outcome.
        self.assertEqual(sent['execution_status'], 'SETTLED')
        self.assertEqual(desk.STATUS_WORDS[sent['status']][0], 'Sent')


if __name__ == '__main__':
    unittest.main()
