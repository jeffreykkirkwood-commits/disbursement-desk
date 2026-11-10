# Automated checks for payout_notes.py: a deterministic rules note per payout
# (issue #37). Notes are stored; the Desk does not render them here.
"""Exercise the rules-note templates with the fictional seed and temporary databases."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
import payout_notes
import payouts


HARBOR_AMOUNT_NOTE = (
    'Rule AMOUNT_OVER_3X_AVG: Amount is 4× this payee’s usual payout ($520 avg). '
    'Review before sending.')
ACME_AMOUNT_NOTE = (
    'Rule AMOUNT_OVER_3X_AVG: Amount is 6× this payee’s usual payout ($790 avg). '
    'Review before sending.')
NORTHWIND_DUPLICATE_NOTE = (
    'Rule DUPLICATE_24H: Same payee and amount as yesterday (possible duplicate). '
    'Review before sending.')
CLEAN_IDS = ('PO-1036', 'PO-1039', 'PO-1040', 'PO-1041', 'PO-1045')


def by_id(payout_id):
    return next(row for row in payouts.seed_rows() if row['payout_id'] == payout_id)


class RulesNoteTests(unittest.TestCase):
    # FEATURE 01 — Scenario — the same payout always produces the same note
    def test_same_payout_same_note(self):
        acme = by_id('PO-1042')
        self.assertEqual(payout_notes.rules_note(acme), payout_notes.rules_note(acme))
        self.assertEqual(payouts.seed_rows(), payouts.seed_rows())
        for row in payouts.seed_rows():
            self.assertEqual(payout_notes.rules_note(row),
                             (row['note_text'], row['note_source'], row['note_fallback']))

    # FEATURE 02 — Scenario — each approved rule has its own wording
    def test_each_rule_has_its_own_wording(self):
        amount = payout_notes.rules_note(by_id('PO-1042'))[0]
        bank = payout_notes.rules_note(by_id('PO-1043'))[0]
        duplicate = payout_notes.rules_note(by_id('PO-1044'))[0]
        clean = payout_notes.rules_note(by_id('PO-1040'))[0]
        self.assertEqual(amount, ACME_AMOUNT_NOTE)
        self.assertEqual(bank, payout_notes.BANK_CHANGED_NOTE)
        self.assertEqual(duplicate, NORTHWIND_DUPLICATE_NOTE)
        self.assertEqual(clean, payout_notes.CLEAN_NOTE)
        self.assertEqual(len({amount, bank, duplicate, clean}), 4)
        self.assertTrue(amount.startswith('Rule AMOUNT_OVER_3X_AVG:'))
        self.assertTrue(bank.startswith('Rule BANK_CHANGED_7D:'))
        self.assertTrue(duplicate.startswith('Rule DUPLICATE_24H:'))
        self.assertNotIn('Rule ', clean)

    # FEATURE 03 — Scenario — clean payouts, including unlisted queue rows, get the short line
    def test_clean_payouts_get_no_exceptions_note(self):
        self.assertEqual(payout_notes.rules_note({'flag_code': None, 'flag_text': None}),
                         (payout_notes.CLEAN_NOTE, 'rules', False))
        for payout_id in CLEAN_IDS:
            row = by_id(payout_id)
            self.assertIsNone(row['flag_code'], payout_id)
            self.assertEqual(row['note_text'], payout_notes.CLEAN_NOTE, payout_id)
            self.assertEqual((row['note_source'], row['note_fallback']), ('rules', False), payout_id)
        # History rows exist only for usual-amount math and are also clean.
        history = [row for row in payouts.seed_rows() if row['payout_id'] not in payouts.CURRENT_IDS]
        self.assertEqual(len(history), 30)
        self.assertTrue(all(row['note_text'] == payout_notes.CLEAN_NOTE for row in history))

    # FEATURE 04 — Scenario — Harbor's note matches its amount flag, not an operator hold
    def test_harbor_note_matches_amount_seed(self):
        harbor = by_id('PO-1038')
        self.assertEqual(harbor['status'], 'HELD')
        self.assertEqual(harbor['flag_code'], 'AMOUNT_OVER_3X_AVG')
        self.assertEqual(harbor['note_text'], HARBOR_AMOUNT_NOTE)
        self.assertNotIn('MANUAL_HOLD', harbor['note_text'])
        self.assertNotIn('operator', harbor['note_text'].lower())
        # Maple starts Returned because of a bank-details flag, not a manual return.
        maple = by_id('PO-1037')
        self.assertEqual(maple['status'], 'RETURNED')
        self.assertEqual(maple['flag_code'], 'BANK_CHANGED_7D')
        self.assertEqual(maple['note_text'], payout_notes.BANK_CHANGED_NOTE)
        self.assertNotIn('MANUAL_RETURN', maple['note_text'])

    # FEATURE 05 — Scenario — every seed row stores a rules note, never an AI note
    def test_every_seed_row_stores_a_rules_note(self):
        rows = payouts.seed_rows()
        self.assertEqual(len(rows), 40)
        for row in rows:
            with self.subTest(payout=row['payout_id']):
                self.assertTrue(row['note_text'])
                self.assertEqual(row['note_source'], 'rules')
                self.assertIs(row['note_fallback'], False)
                self.assertNotIn('Written by AI', row['note_text'])


class StoredNoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'payments.sqlite3'

    def tearDown(self):
        self.temp.cleanup()

    # FEATURE 06 — Scenario — seed and reset write the same stored notes
    def test_seed_and_reset_store_notes(self):
        self.assertEqual(payouts.ensure_seeded(self.db), 40)
        harbor = payouts.get_payout(self.db, 'PO-1038')
        self.assertEqual(harbor['note_text'], HARBOR_AMOUNT_NOTE)
        self.assertEqual(harbor['note_source'], 'rules')
        self.assertIs(harbor['note_fallback'], False)
        cedar = payouts.get_payout(self.db, 'PO-1040')
        self.assertEqual(cedar['note_text'], payout_notes.CLEAN_NOTE)
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE payouts SET status = 'PAID', note_text = 'temporary' "
                       "WHERE payout_id = 'PO-1038'")
        self.assertEqual(payouts.reset(self.db), 40)
        restored = payouts.get_payout(self.db, 'PO-1038')
        self.assertEqual(restored['status'], 'HELD')
        self.assertEqual(restored['note_text'], HARBOR_AMOUNT_NOTE)

    # FEATURE 07 — Scenario — an older demo with empty note columns is filled in place
    # Status and flags stay as they were. Only null notes are written. Running it
    # again changes nothing.
    def test_backfill_empty_notes_without_resetting_status(self):
        payouts.ensure_seeded(self.db)
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE payouts SET status = 'HELD', note_text = NULL, "
                       "note_source = NULL, note_fallback = NULL WHERE payout_id = 'PO-1042'")
            db.execute("INSERT INTO payout_events (payout_id, action, from_status, to_status, actor, at) "
                       "VALUES ('PO-1042', 'hold', 'FLAGGED', 'HELD', 'Test Operator', "
                       "'2026-10-28T12:00:00+00:00')")
        self.assertEqual(payouts.ensure_seeded(self.db), 0)
        acme = payouts.get_payout(self.db, 'PO-1042')
        self.assertEqual(acme['status'], 'HELD')
        self.assertEqual(acme['note_text'], ACME_AMOUNT_NOTE)
        self.assertEqual(acme['note_source'], 'rules')
        self.assertIs(acme['note_fallback'], False)
        self.assertEqual(payouts.list_events(self.db, 'PO-1042')[0]['action'], 'hold')
        self.assertEqual(payouts.ensure_seeded(self.db), 0)
        self.assertEqual(payouts.get_payout(self.db, 'PO-1042')['note_text'], ACME_AMOUNT_NOTE)
