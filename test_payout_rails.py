# Automated checks for the payout-rail schema: ACH, SAME_DAY_ACH, RTP, FEDNOW, and WIRE
# are stored; ACH is the default; an older demo with only ACH and RTP is rebuilt
# without dropping operator work. Settlement timing and return paths are not in
# this pull request.
"""Expand the payout rail column and keep existing demos usable."""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from payments import APIError
import payout_rules
import payouts
from test_payout_migration import make_pre54_demo

SEED_COUNT = 40
NOW = datetime(2026, 11, 12, 17, 0, tzinfo=timezone.utc)
AT = NOW.isoformat()

# Today's payouts table, still limited to ACH and RTP — the schema on main
# before this change.
CURRENT_OLD_RAIL_SCHEMA = payouts.SCHEMA[0].replace(payouts.RAIL_CHECK, "('ACH', 'RTP')")


def rail_sql(path):
    with sqlite3.connect(path) as db:
        row = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='payouts'").fetchone()
    return row[0] if row else ''


def make_current_old_rail_demo(path, keep_rows=False):
    """Build a current (#54) demo whose rail CHECK still allows only ACH and RTP."""
    payouts.ensure_seeded(path)
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA foreign_keys = OFF')
        db.execute('BEGIN IMMEDIATE')
        db.execute(CURRENT_OLD_RAIL_SCHEMA.replace(
            'CREATE TABLE IF NOT EXISTS payouts', 'CREATE TABLE payouts_oldrail', 1))
        cols = [row[1] for row in db.execute('PRAGMA table_info(payouts)')]
        db.execute(
            f'INSERT INTO payouts_oldrail ({", ".join(cols)}) SELECT {", ".join(cols)} FROM payouts')
        db.execute('DROP TABLE payouts')
        db.execute('ALTER TABLE payouts_oldrail RENAME TO payouts')
        if keep_rows:
            db.execute(
                "INSERT INTO payout_events (payout_id, action, from_status, to_status, actor, at) "
                "VALUES ('PO-1042', 'hold', 'FLAGGED', 'HELD', 'Test Operator', ?)",
                (AT,))
        db.commit()
    return path


class PayoutRailSchemaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'payments.sqlite3'

    def tearDown(self):
        self.temp.cleanup()

    def create(self, payout_id='PO-4100', rail=payouts.ACH, **extra):
        return payout_rules.create(
            self.db, payout_id, 'Rail Test Payee', 'DEMO-RAIL-001', 25000, rail,
            now=NOW, **extra)

    # FEATURE 01 — Scenario — a fresh demo stores all four rails and defaults to ACH
    def test_fresh_schema_accepts_each_rail_and_defaults_to_ach(self):
        payouts.ensure_seeded(self.db)
        self.assertIn('SAME_DAY_ACH', rail_sql(self.db))
        self.assertIn('FEDNOW', rail_sql(self.db))
        self.assertIn('WIRE', rail_sql(self.db))
        self.assertIn('FEDNOW', rail_sql(self.db))
        for index, rail in enumerate(payouts.RAILS):
            payout = self.create(f'PO-41{index:02d}', rail=rail)
            stored = payouts.get_payout(self.db, payout['payout_id'])
            self.assertEqual(stored['rail'], rail)
        defaulted = self.create('PO-4199', rail=None)
        self.assertEqual(payouts.get_payout(self.db, defaulted['payout_id'])['rail'],
                         payouts.DEFAULT_RAIL)
        blank = self.create('PO-4198', rail='   ')
        self.assertEqual(payouts.get_payout(self.db, blank['payout_id'])['rail'],
                         payouts.DEFAULT_RAIL)

    # FEATURE 02 — Scenario — an unknown rail is refused and nothing is saved
    def test_unknown_rail_is_refused(self):
        payouts.ensure_seeded(self.db)
        before = {p['payout_id'] for p in payouts.list_payouts(self.db)}
        with self.assertRaises(APIError) as raised:
            self.create('PO-4200', rail='SWIFT')
        self.assertEqual(raised.exception.status, 422)
        self.assertIn('FEDNOW', raised.exception.message)
        self.assertNotIn('PO-4200', {p['payout_id'] for p in payouts.list_payouts(self.db)})
        self.assertEqual({p['payout_id'] for p in payouts.list_payouts(self.db)}, before)

    # FEATURE 03 — Scenario — the starting set is unchanged and still only ACH or RTP
    def test_seed_rails_are_unchanged(self):
        payouts.ensure_seeded(self.db)
        current = [p for p in payouts.list_payouts(self.db)
                   if p['payout_id'] in {row[0] for row in payouts.CURRENT}]
        self.assertEqual({p['rail'] for p in current}, {'ACH', 'RTP'})
        self.assertEqual(len(payouts.list_payouts(self.db)), SEED_COUNT)

    # FEATURE 04 — Scenario — an existing demo keeps its rows and then accepts a wire
    def test_existing_demo_is_rebuilt_and_keeps_operator_rows(self):
        make_current_old_rail_demo(self.db, keep_rows=True)
        self.assertNotIn('SAME_DAY_ACH', rail_sql(self.db))
        before = {p['payout_id'] for p in payouts.list_payouts(self.db)}
        self.assertEqual(payouts.ensure_seeded(self.db), 0)
        self.assertEqual(payouts.ensure_seeded(self.db), 0)
        self.assertIn('SAME_DAY_ACH', rail_sql(self.db))
        self.assertIn('FEDNOW', rail_sql(self.db))
        self.assertEqual({p['payout_id'] for p in payouts.list_payouts(self.db)}, before)
        events = payouts.list_events(self.db)
        self.assertTrue(any(event['payout_id'] == 'PO-1042' and event['action'] == 'hold'
                            for event in events))
        wire = self.create('PO-4300', rail=payouts.WIRE)
        self.assertEqual(payouts.get_payout(self.db, wire['payout_id'])['rail'], payouts.WIRE)
        fednow = self.create('PO-4303', rail=payouts.FEDNOW)
        self.assertEqual(payouts.get_payout(self.db, fednow['payout_id'])['rail'], payouts.FEDNOW)
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])

    # FEATURE 05 — Scenario — a pre-#54 demo still upgrades and then accepts same-day ACH
    def test_pre54_demo_still_upgrades(self):
        make_pre54_demo(self.db, keep_rows=True)
        self.assertEqual(payouts.ensure_seeded(self.db), 0)
        self.assertIn('FEDNOW', rail_sql(self.db))
        payout = self.create('PO-4301', rail=payouts.FEDNOW)
        stored = payouts.get_payout(self.db, payout['payout_id'])
        self.assertEqual(stored['rail'], payouts.FEDNOW)
        self.assertIsNotNone(stored['review_status'])

    # FEATURE 06 — Scenario — Reset demo applies the new CHECK and restores the seed
    def test_reset_applies_new_check_and_restores_the_seed(self):
        make_current_old_rail_demo(self.db, keep_rows=True)
        self.assertEqual(payouts.reset(self.db), SEED_COUNT)
        self.assertIn('WIRE', rail_sql(self.db))
        ids = {p['payout_id'] for p in payouts.list_payouts(self.db)}
        self.assertEqual(len(ids), SEED_COUNT)
        self.assertEqual(payouts.list_events(self.db), [])
        payout = self.create('PO-4302', rail=payouts.WIRE)
        self.assertEqual(payouts.get_payout(self.db, payout['payout_id'])['rail'], payouts.WIRE)
        self.assertEqual(payouts.reset(self.db), SEED_COUNT)
        self.assertNotIn('PO-4302', {p['payout_id'] for p in payouts.list_payouts(self.db)})


if __name__ == '__main__':
    unittest.main()
