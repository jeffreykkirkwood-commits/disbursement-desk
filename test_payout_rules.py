# Automated checks for payout_rules.py: the in-process automation path that
# routes payouts through the exception rules (issue #44), including the
# new-payee limit (issues #86 and #89).
"""Exercise clean, flagged and repeat automation runs with temporary fictional databases."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import payout_notes
import payout_rules
import payouts
from payments import APIError

# Well after the seed window (late October 2026), so new payouts are not
# caught by the 24-hour duplicate rule against the starting set.
NOW = datetime(2026, 11, 10, 18, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(hours=1)


def iso(when):
    return when.isoformat()


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'payments.sqlite3'
        payouts.ensure_seeded(self.db)
        self.seeded = json.dumps(payouts.list_payouts(self.db), sort_keys=True)
        self.seed_events = payouts.list_events(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def create(self, payout_id, payee, account, cents, rail='ACH', changed=None, when=NOW):
        return payout_rules.create(
            self.db, payout_id, payee, account, cents, rail,
            bank_details_changed_at=None if changed is None else iso(changed),
            now=when)

    # FEATURE 01 — Scenario — a clean payout is paid automatically and logged
    def test_clean_payout_is_paid_and_logged(self):
        result = self.create('PO-TEST-CLEAN', 'Cedar Cleaning Co.', 'DEMO-CEDR-004', 64000)
        self.assertTrue(result['changed'])
        self.assertEqual(result['status'], 'PAID')
        payout = payouts.get_payout(self.db, 'PO-TEST-CLEAN')
        self.assertEqual(payout['status'], 'PAID')
        self.assertIsNone(payout['flag_code'])
        self.assertEqual(payout['note_text'], payout_notes.CLEAN_NOTE)
        self.assertEqual(payout['note_source'], 'rules')
        self.assertFalse(payout['note_fallback'])
        event = result['event']
        self.assertEqual(event['action'], payout_rules.ACTION)
        self.assertEqual(event['actor'], payout_rules.ACTOR)
        self.assertEqual(event['from_status'], 'PENDING')
        self.assertEqual(event['to_status'], 'PAID')
        self.assertEqual(event['reason'], None)
        self.assertEqual(event['idempotency_key'], 'route:PO-TEST-CLEAN')
        self.assertNotIn(event['action'], payouts.DECISIONS)
        events = payouts.list_events(self.db, 'PO-TEST-CLEAN')
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['action'], 'route')

    # FEATURE 02 — Scenario — each approved rule stops the payout at FLAGGED
    def test_flagged_payouts_stop_at_flagged(self):
        cases = (
            ('PO-TEST-AMOUNT', 'Harbor Hardware', 'DEMO-HRBR-006', 207550, None,
             'AMOUNT_OVER_3X_AVG'),
            ('PO-TEST-BANK', 'Bluebird Bakery', 'DEMO-BLUE-002', 31240, NOW - timedelta(days=2),
             'BANK_CHANGED_7D'),
            ('PO-TEST-DUP', 'Northwind Freight', 'DEMO-NWND-003', 120000, None,
             'DUPLICATE_24H'),
        )
        # The duplicate case needs a same-payee, same-amount payout already in the
        # 24-hour window. Seed Northwind rows are older than NOW, so pay one first.
        first = self.create('PO-TEST-DUP-FIRST', 'Northwind Freight', 'DEMO-NWND-003', 120000,
                            when=NOW)
        self.assertEqual(first['status'], 'PAID')
        for payout_id, payee, account, cents, changed, code in cases:
            with self.subTest(rule=code):
                when = LATER if code == 'DUPLICATE_24H' else NOW
                result = self.create(payout_id, payee, account, cents, changed=changed, when=when)
                self.assertTrue(result['changed'], code)
                self.assertEqual(result['status'], 'FLAGGED', code)
                payout = payouts.get_payout(self.db, payout_id)
                self.assertEqual(payout['status'], 'FLAGGED')
                self.assertEqual(payout['flag_code'], code)
                self.assertIsNotNone(payout['flag_text'])
                self.assertTrue(payout['note_text'].startswith(f'Rule {code}:') or
                                payout['note_text'] == payout_notes.BANK_CHANGED_NOTE)
                event = result['event']
                self.assertEqual(event['action'], 'route')
                self.assertEqual(event['actor'], 'automation')
                self.assertEqual(event['from_status'], 'PENDING')
                self.assertEqual(event['to_status'], 'FLAGGED')
                self.assertEqual(event['reason'], payout['flag_text'])
                self.assertNotIn(event['action'], ('approve', 'hold', 'return'))

    # FEATURE 03 — Scenario — running automation twice changes nothing
    def test_repeat_run_changes_nothing(self):
        first = self.create('PO-TEST-REPEAT', 'Cedar Cleaning Co.', 'DEMO-CEDR-004', 64000)
        self.assertTrue(first['changed'])
        self.assertEqual(first['status'], 'PAID')
        payout = payouts.get_payout(self.db, 'PO-TEST-REPEAT')
        events = payouts.list_events(self.db, 'PO-TEST-REPEAT')
        second = payout_rules.route(self.db, 'PO-TEST-REPEAT', now=LATER)
        third = payout_rules.route(self.db, now=LATER)
        self.assertFalse(second['changed'])
        self.assertIsNone(second['event'])
        self.assertEqual(second['status'], 'PAID')
        self.assertEqual(payouts.get_payout(self.db, 'PO-TEST-REPEAT'), payout)
        self.assertEqual(payouts.list_events(self.db, 'PO-TEST-REPEAT'), events)
        self.assertEqual(len(events), 1)
        self.assertEqual(third, [])
        # A flagged payout is also unchanged on a second run.
        flagged = self.create('PO-TEST-REPEAT-FLAG', 'Harbor Hardware', 'DEMO-HRBR-006', 207550)
        after = payout_rules.route(self.db, 'PO-TEST-REPEAT-FLAG')
        self.assertEqual(flagged['status'], 'FLAGGED')
        self.assertFalse(after['changed'])
        self.assertEqual(len(payouts.list_events(self.db, 'PO-TEST-REPEAT-FLAG')), 1)
        self.assertEqual(payouts.get_payout(self.db, 'PO-TEST-REPEAT-FLAG')['status'], 'FLAGGED')

    # FEATURE 04 — Scenario — automation cannot approve, hold or return a flagged payout
    def test_automation_cannot_decide_a_flagged_payout(self):
        originals = {row['payout_id']: row for row in payouts.list_payouts(self.db)}
        # Seed rows already past PENDING, including flagged, held, returned and paid.
        for payout_id in ('PO-1042', 'PO-1043', 'PO-1044', 'PO-1038', 'PO-1037', 'PO-1040'):
            result = payout_rules.route(self.db, payout_id)
            self.assertFalse(result['changed'], payout_id)
            self.assertIsNone(result['event'])
            self.assertEqual(payouts.get_payout(self.db, payout_id)['status'],
                             originals[payout_id]['status'], payout_id)
        batch = payout_rules.route(self.db)
        self.assertEqual(batch, [])
        self.assertEqual(payouts.list_events(self.db), self.seed_events)
        self.assertEqual(json.dumps(payouts.list_payouts(self.db), sort_keys=True), self.seeded)
        # A payout this path just flagged also cannot be decided by another route run.
        created = self.create('PO-TEST-NO-DECIDE', 'Harbor Hardware', 'DEMO-HRBR-006', 207550)
        self.assertEqual(created['status'], 'FLAGGED')
        self.assertEqual(created['event']['action'], 'route')
        again = payout_rules.route(self.db, 'PO-TEST-NO-DECIDE')
        self.assertFalse(again['changed'])
        self.assertEqual(payouts.get_payout(self.db, 'PO-TEST-NO-DECIDE')['status'], 'FLAGGED')
        actions = {event['action'] for event in payouts.list_events(self.db)}
        self.assertEqual(actions, {'route'})
        self.assertTrue(actions.isdisjoint(payouts.DECISIONS))

    # FEATURE 05 — Scenario — seed load and reset keep the starting set and add no events
    def test_seed_load_does_not_change_the_starting_set(self):
        other = Path(self.temp.name) / 'second.sqlite3'
        self.assertEqual(payouts.ensure_seeded(self.db), 0)
        self.assertEqual(payouts.ensure_seeded(other), 40)
        self.assertEqual(json.dumps(payouts.list_payouts(self.db), sort_keys=True), self.seeded)
        self.assertEqual(json.dumps(payouts.list_payouts(other), sort_keys=True), self.seeded)
        self.assertEqual(payouts.list_events(self.db), [])
        self.assertEqual(payouts.list_events(other), [])
        for row in payouts.list_payouts(other):
            self.assertNotEqual(row['status'], 'PENDING', row['payout_id'])
        # Held and returned seed rows stay as curated, even though rules would flag them.
        self.assertEqual(payouts.get_payout(other, 'PO-1038')['status'], 'HELD')
        self.assertEqual(payouts.get_payout(other, 'PO-1037')['status'], 'RETURNED')
        self.assertEqual(payouts.reset(self.db), 40)
        self.assertEqual(json.dumps(payouts.list_payouts(self.db), sort_keys=True), self.seeded)
        self.assertEqual(payouts.list_events(self.db), [])

    # FEATURE 06 — Scenario — an operator can still decide a payout automation flagged
    def test_operator_can_still_decide_an_auto_flagged_payout(self):
        created = self.create('PO-TEST-DECIDE', 'Harbor Hardware', 'DEMO-HRBR-006', 207550)
        self.assertEqual(created['status'], 'FLAGGED')
        status, decision = payouts.decide(
            self.db, 'PO-TEST-DECIDE', 'approve', 'Test Reviewer', 'key-1', now=LATER)
        self.assertEqual(status, 201)
        self.assertEqual(decision['action'], 'approve')
        self.assertEqual(decision['from_status'], 'FLAGGED')
        self.assertEqual(decision['to_status'], 'PAID')
        self.assertEqual(payouts.get_payout(self.db, 'PO-TEST-DECIDE')['status'], 'PAID')
        actions = [event['action'] for event in payouts.list_events(self.db, 'PO-TEST-DECIDE')]
        self.assertEqual(actions, ['route', 'approve'])

    # FEATURE 07 — Scenario — a missing payout is refused; a second create is refused
    def test_missing_and_duplicate_create_are_refused(self):
        with self.assertRaises(APIError) as missing:
            payout_rules.route(self.db, 'PO-MISSING')
        self.assertEqual(missing.exception.status, 404)
        self.create('PO-TEST-ONCE', 'Cedar Cleaning Co.', 'DEMO-CEDR-004', 64000)
        with self.assertRaises(APIError) as exists:
            self.create('PO-TEST-ONCE', 'Cedar Cleaning Co.', 'DEMO-CEDR-004', 64000)
        self.assertEqual(exists.exception.status, 409)
        self.assertEqual(json.dumps(payouts.list_payouts(self.db), sort_keys=True).count('PO-TEST-ONCE'), 1)

    # FEATURE 08 — Scenario — a new payee's first payout over $1,000 is flagged (#86)
    def test_new_payee_first_payout_over_1000_is_flagged(self):
        result = self.create('PO-TEST-NEW-OVER', 'Oak Street Books', 'DEMO-OAKS-010', 100001)
        self.assertTrue(result['changed'])
        self.assertEqual(result['status'], 'FLAGGED')
        payout = payouts.get_payout(self.db, 'PO-TEST-NEW-OVER')
        self.assertEqual(payout['status'], 'FLAGGED')
        self.assertEqual(payout['flag_code'], 'NEW_PAYEE_OVER_1000')
        self.assertEqual(payout['flag_text'], 'First payout to this payee is over $1,000')
        self.assertEqual(
            payout['note_text'],
            'Rule NEW_PAYEE_OVER_1000: First payout to this payee is over $1,000. '
            'Review before sending.')
        self.assertEqual(payout['note_source'], 'rules')
        self.assertFalse(payout['note_fallback'])
        self.assertNotIn('Written by AI', payout['note_text'])
        event = result['event']
        self.assertEqual(event['action'], 'route')
        self.assertEqual(event['actor'], 'automation')
        self.assertEqual(event['from_status'], 'PENDING')
        self.assertEqual(event['to_status'], 'FLAGGED')
        self.assertEqual(event['reason'], payout['flag_text'])

    # FEATURE 08b — Scenario — a new payee's first payout of exactly $1,000 auto-pays
    def test_new_payee_first_payout_of_1000_is_paid(self):
        result = self.create('PO-TEST-NEW-LIMIT', 'Oak Street Books', 'DEMO-OAKS-010', 100000)
        self.assertTrue(result['changed'])
        self.assertEqual(result['status'], 'PAID')
        payout = payouts.get_payout(self.db, 'PO-TEST-NEW-LIMIT')
        self.assertEqual(payout['status'], 'PAID')
        self.assertIsNone(payout['flag_code'])
        self.assertIsNone(payout['flag_text'])
        self.assertEqual(payout['note_text'], payout_notes.CLEAN_NOTE)

    # FEATURE 08c — Scenario — an existing payee is not flagged by the new-payee rule
    def test_existing_payee_is_unaffected_by_new_payee_rule(self):
        # Cedar's usual amount is $640, so $1,000.01 is under 3× and should auto-pay.
        result = self.create('PO-TEST-EXISTING', 'Cedar Cleaning Co.', 'DEMO-CEDR-004', 100001)
        self.assertEqual(result['status'], 'PAID')
        payout = payouts.get_payout(self.db, 'PO-TEST-EXISTING')
        self.assertIsNone(payout['flag_code'])
        self.assertEqual(payout['note_text'], payout_notes.CLEAN_NOTE)
        # Harbor's large amount still trips the usual-amount rule, not the new-payee rule.
        flagged = self.create('PO-TEST-EXISTING-AMT', 'Harbor Hardware', 'DEMO-HRBR-006', 207550)
        self.assertEqual(flagged['status'], 'FLAGGED')
        self.assertEqual(payouts.get_payout(self.db, 'PO-TEST-EXISTING-AMT')['flag_code'],
                         'AMOUNT_OVER_3X_AVG')

    # FEATURE 08d — Scenario — routing a new-payee flag twice changes nothing
    def test_new_payee_flag_repeat_run_changes_nothing(self):
        first = self.create('PO-TEST-NEW-REPEAT', 'Oak Street Books', 'DEMO-OAKS-010', 100001)
        self.assertEqual(first['status'], 'FLAGGED')
        payout = payouts.get_payout(self.db, 'PO-TEST-NEW-REPEAT')
        events = payouts.list_events(self.db, 'PO-TEST-NEW-REPEAT')
        second = payout_rules.route(self.db, 'PO-TEST-NEW-REPEAT', now=LATER)
        self.assertFalse(second['changed'])
        self.assertIsNone(second['event'])
        self.assertEqual(second['status'], 'FLAGGED')
        self.assertEqual(payouts.get_payout(self.db, 'PO-TEST-NEW-REPEAT'), payout)
        self.assertEqual(payouts.list_events(self.db, 'PO-TEST-NEW-REPEAT'), events)
        self.assertEqual(len(events), 1)

    # FEATURE 08e — Scenario — the seed still has no new-payee flags
    def test_seed_is_unchanged_by_the_new_payee_rule(self):
        self.assertFalse(any('NEW_PAYEE_OVER_1000' in payouts.split_flag_codes(row['flag_code'])
                             for row in payouts.list_payouts(self.db)))
        self.assertEqual(json.dumps(payouts.list_payouts(self.db), sort_keys=True), self.seeded)
        self.assertEqual(payouts.list_events(self.db), self.seed_events)
        self.assertEqual(payouts.reset(self.db), 40)
        self.assertEqual(json.dumps(payouts.list_payouts(self.db), sort_keys=True), self.seeded)
        self.assertEqual(payouts.list_events(self.db), [])

    # FEATURE 09 — Scenario — a second payout is still new until one is PAID (#89)
    def test_second_payout_is_flagged_until_payee_has_a_paid_payout(self):
        first = self.create('PO-TEST-SPLIT-1', 'Oak Street Books', 'DEMO-OAKS-010', 100001)
        self.assertEqual(first['status'], 'FLAGGED')
        self.assertEqual(payouts.get_payout(self.db, 'PO-TEST-SPLIT-1')['flag_code'],
                         'NEW_PAYEE_OVER_1000')
        second = self.create('PO-TEST-SPLIT-2', 'Oak Street Books', 'DEMO-OAKS-010', 200000,
                             when=LATER)
        self.assertEqual(second['status'], 'FLAGGED')
        payout = payouts.get_payout(self.db, 'PO-TEST-SPLIT-2')
        self.assertEqual(payout['flag_code'], 'NEW_PAYEE_OVER_1000')
        self.assertEqual(payout['flag_text'], 'First payout to this payee is over $1,000')
        self.assertIn('Rule NEW_PAYEE_OVER_1000:', payout['note_text'])
        self.assertEqual(payout['note_source'], 'rules')
        self.assertFalse(payout['note_fallback'])
        self.assertNotIn('Written by AI', payout['note_text'])
        # Exactly $1,000 still auto-pays even while the first payout is in review.
        exact = self.create('PO-TEST-SPLIT-EXACT', 'Oak Street Books', 'DEMO-OAKS-010', 100000,
                            when=LATER + timedelta(hours=1))
        self.assertEqual(exact['status'], 'PAID')
        self.assertIsNone(payouts.get_payout(self.db, 'PO-TEST-SPLIT-EXACT')['flag_code'])
        # After that paid payout, a later one over $1,000 is no longer a new payee.
        # Use $1,500 (not a 24-hour duplicate of the earlier $1,000.01 or $1,000).
        # Usual amount is the $1,000 paid payout, so $1,500 is under 3× and auto-pays.
        after = self.create('PO-TEST-SPLIT-AFTER', 'Oak Street Books', 'DEMO-OAKS-010', 150000,
                            when=LATER + timedelta(hours=2))
        self.assertEqual(after['status'], 'PAID')
        self.assertIsNone(payouts.get_payout(self.db, 'PO-TEST-SPLIT-AFTER')['flag_code'])

    # FEATURE 09b — Scenario — every tripped rule is stored, not just the first (#89)
    def test_new_payee_and_bank_change_both_show(self):
        result = self.create(
            'PO-TEST-MULTI', 'Oak Street Books', 'DEMO-OAKS-010', 150000,
            changed=NOW, when=NOW)
        self.assertEqual(result['status'], 'FLAGGED')
        payout = payouts.get_payout(self.db, 'PO-TEST-MULTI')
        self.assertEqual(payout['flag_code'], 'NEW_PAYEE_OVER_1000,BANK_CHANGED_7D')
        self.assertEqual(
            payout['flag_text'],
            'First payout to this payee is over $1,000. Payee bank details changed today')
        self.assertEqual(
            payout['note_text'],
            'Rule NEW_PAYEE_OVER_1000: First payout to this payee is over $1,000. '
            'Review before sending. ' + payout_notes.BANK_CHANGED_NOTE)
        self.assertEqual(payout['note_source'], 'rules')
        self.assertFalse(payout['note_fallback'])
        self.assertNotIn('Written by AI', payout['note_text'])
        self.assertEqual(result['event']['reason'], payout['flag_text'])


if __name__ == '__main__':
    unittest.main()
