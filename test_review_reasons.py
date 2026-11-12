# Automated checks for the structured "why this is in review" reasons (#32).
# Each stored hold reason is covered on its own and in combination. Payees and
# accounts used here are synthetic; snapshot names are public SDN entries.
"""Exercise review-reason lists for payouts held on the Desk."""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import demo_scenario
import ofac
import payout_rules
import payouts
import review_reasons
from hosted_app import create_app
from payments import APIError

NOW = datetime(2026, 11, 12, 16, 0, tzinfo=timezone.utc)
CODES = lambda items: [item['code'] for item in items]
PO_4101_CENTS = 150000  # $1,500: new-payee-over-$1,000 plus the sanctions match


def stored_flag_codes(database, payout_id):
    payout = payouts.get_payout(database, payout_id)
    return payouts.split_flag_codes(payout.get('flag_code'))


class ReviewReasonsSeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'payments.sqlite3'
        payouts.ensure_seeded(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def reasons(self, payout_id):
        return review_reasons.for_payout(self.db, payout_id)

    def assert_codes_match_stored(self, payout_id):
        self.assertEqual(set(CODES(self.reasons(payout_id))),
                         set(stored_flag_codes(self.db, payout_id)))

    def assert_in_review_codes_match_stored(self):
        checked = 0
        for payout in payouts.list_payouts(self.db):
            if payout['status'] not in review_reasons.IN_REVIEW:
                continue
            self.assert_codes_match_stored(payout['payout_id'])
            checked += 1
        self.assertGreater(checked, 0)

    # FEATURE 01 — Scenario — amount vs history on a known seed payout
    def test_amount_vs_history_on_acme(self):
        items = self.reasons('PO-1042')
        self.assertEqual(CODES(items), [payouts.AMOUNT_OVER_3X_AVG])
        detail = items[0]['details']
        self.assertEqual(items[0]['label'],
                         'Amount is 6× this payee’s usual payout ($790 avg)')
        self.assertEqual(detail['amount_cents'], 485000)
        self.assertEqual(detail['usual_cents'], 79000)
        self.assertEqual(detail['multiplier'], 3)
        self.assertEqual(detail['ratio'], 6)
        self.assertGreater(detail['amount_cents'],
                           detail['multiplier'] * detail['usual_cents'])

    def test_held_harbor_also_uses_amount_vs_history(self):
        items = self.reasons('PO-1038')
        self.assertEqual(CODES(items), [payouts.AMOUNT_OVER_3X_AVG])
        self.assertEqual(items[0]['details']['usual_cents'], 52000)
        self.assertGreater(items[0]['details']['amount_cents'],
                           3 * items[0]['details']['usual_cents'])
        self.assertEqual(items[0]['label'],
                         'Amount is 4× this payee’s usual payout ($520 avg)')

    # FEATURE 02 — Scenario — a recent bank-details change is its own reason
    def test_bank_details_change_on_bluebird(self):
        items = self.reasons('PO-1043')
        self.assertEqual(CODES(items), [payouts.BANK_CHANGED_7D])
        self.assertEqual(items[0]['label'], 'Payee bank details changed 2 days ago')
        self.assertEqual(items[0]['details']['days_ago'], 2)
        self.assertTrue(items[0]['details']['changed_at'])

    # FEATURE 03 — Scenario — a same-payee same-amount payout is a possible duplicate
    def test_possible_duplicate_on_northwind(self):
        items = self.reasons('PO-1044')
        self.assertEqual(CODES(items), [payouts.DUPLICATE_24H])
        self.assertEqual(items[0]['label'],
                         'Same payee and amount as yesterday (possible duplicate)')
        self.assertEqual(items[0]['details']['amount_cents'], 120000)
        self.assertEqual(items[0]['details']['other_payout_id'], 'PO-1039')

    # FEATURE 04 — Scenario — a payout not in review has no reasons
    def test_sent_or_returned_payouts_have_no_reasons(self):
        self.assertEqual(self.reasons('PO-1039'), [])
        self.assertEqual(self.reasons('PO-1037'), [])
        self.assertEqual(self.reasons('PO-1045'), [])

    def test_unknown_payout_is_404(self):
        with self.assertRaises(APIError) as caught:
            self.reasons('PO-9999')
        self.assertEqual(caught.exception.status, 404)

    # FEATURE 05 — Scenario — reason codes equal the stored flags
    def test_reason_codes_equal_stored_flags_for_seed_scenario_and_ofac(self):
        self.assert_in_review_codes_match_stored()
        demo_scenario.run(self.db)
        self.assert_in_review_codes_match_stored()
        payout_rules.create(
            self.db, 'PO-4100', ofac.DEMO_PAYEE_NAME, ofac.DEMO_PAYEE_ACCOUNT,
            ofac.DEMO_AMOUNT_CENTS, 'ACH', now=NOW)
        payout_rules.create(
            self.db, 'PO-4101', ofac.DEMO_PAYEE_NAME, ofac.DEMO_PAYEE_ACCOUNT,
            PO_4101_CENTS, 'ACH', now=NOW)
        self.assert_in_review_codes_match_stored()
        self.assertEqual(CODES(self.reasons('PO-4100')),
                         [payouts.POSSIBLE_SANCTIONS_MATCH])
        self.assertEqual(CODES(self.reasons('PO-4101')),
                         [payouts.NEW_PAYEE_OVER_1000, payouts.POSSIBLE_SANCTIONS_MATCH])
        self.assert_codes_match_stored('PO-4100')
        self.assert_codes_match_stored('PO-4101')


class ReviewReasonsCreateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'payments.sqlite3'
        payouts.ensure_seeded(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def create(self, payout_id, name, account, cents, rail='ACH', changed=None):
        return payout_rules.create(
            self.db, payout_id, name, account, cents, rail,
            bank_details_changed_at=changed, now=NOW)

    def reasons(self, payout_id):
        return review_reasons.for_payout(self.db, payout_id)

    # FEATURE 06 — Scenario — a possible sanctions match uses the stored flag data
    def test_sanctions_match_includes_entry_score_and_dates(self):
        self.create('PO-4100', ofac.DEMO_PAYEE_NAME, ofac.DEMO_PAYEE_ACCOUNT,
                    ofac.DEMO_AMOUNT_CENTS)
        items = self.reasons('PO-4100')
        self.assertEqual(CODES(items), [payouts.POSSIBLE_SANCTIONS_MATCH])
        self.assertEqual(stored_flag_codes(self.db, 'PO-4100'),
                         [payouts.POSSIBLE_SANCTIONS_MATCH])
        sanctions = items[0]
        details = sanctions['details']
        self.assertIn('Possible sanctions match:', sanctions['label'])
        self.assertEqual(details['entry_name'].upper(), 'BANCO NACIONAL DE CUBA')
        self.assertEqual(details['matched_name'].upper(), 'BANCO NACIONAL DE CUBA')
        self.assertEqual(details['score'], 100)
        self.assertEqual(details['list_published'], '2026-11-09')
        self.assertEqual(details['snapshot_taken'], '2026-11-12')
        self.assertNotIn('uid', details)

    def test_sanctions_and_new_payee_over_1000_on_po_4101(self):
        self.create('PO-4101', ofac.DEMO_PAYEE_NAME, ofac.DEMO_PAYEE_ACCOUNT,
                    PO_4101_CENTS)
        items = self.reasons('PO-4101')
        self.assertEqual(CODES(items),
                         [payouts.NEW_PAYEE_OVER_1000, payouts.POSSIBLE_SANCTIONS_MATCH])
        self.assertEqual(items[0]['label'],
                         payouts.FLAG_TEXT[payouts.NEW_PAYEE_OVER_1000])
        self.assertEqual(items[1]['details']['score'], 100)

    def test_labels_stay_whole_when_a_listed_name_contains_the_separator(self):
        name = 'MSP KALA NAFT CO. TEHRAN'
        self.create('PO-5151', name, 'DEMO-KALA-010', ofac.DEMO_AMOUNT_CENTS)
        self.create('PO-5153', name, 'DEMO-KALA-010', PO_4101_CENTS)
        cases = (
            ('PO-5151', [payouts.POSSIBLE_SANCTIONS_MATCH]),
            ('PO-5153', [payouts.NEW_PAYEE_OVER_1000, payouts.POSSIBLE_SANCTIONS_MATCH]),
        )
        for payout_id, codes in cases:
            with self.subTest(payout_id=payout_id):
                items = self.reasons(payout_id)
                payout = payouts.get_payout(self.db, payout_id)
                self.assertEqual(CODES(items), codes)
                self.assertEqual(
                    payouts.FLAG_TEXT_SEP.join(item['label'] for item in items),
                    payout['flag_text'])
                sanctions = next(item for item in items
                                 if item['code'] == payouts.POSSIBLE_SANCTIONS_MATCH)
                self.assertIn(name, sanctions['label'])
                details = sanctions['details']
                self.assertEqual(details['entry_name'], name)
                self.assertEqual(details['score'], 100)
                self.assertEqual(details['list_published'], '2026-11-09')
                self.assertEqual(details['snapshot_taken'], '2026-11-12')
                self.assertNotIn('flag_text', details)

    # FEATURE 07 — Scenario — a new payee over $1,000 uses the Desk's stored code
    def test_new_payee_over_1000(self):
        self.create('PO-5100', 'Oak Street Books', 'DEMO-OAKS-010', 100001)
        items = self.reasons('PO-5100')
        self.assertEqual(CODES(items), [payouts.NEW_PAYEE_OVER_1000])
        self.assertEqual(items[0]['label'],
                         'No paid payout to this payee yet; amount is over $1,000')

    def test_first_time_under_1000_is_not_held_and_has_no_reasons(self):
        result = self.create('PO-5101', 'Oak Street Books', 'DEMO-OAKS-010', 50000)
        self.assertEqual(result['status'], 'PAID')
        self.assertEqual(self.reasons('PO-5101'), [])

    # FEATURE 08 — Scenario — combinations keep every stored reason
    def test_sanctions_and_new_payee_together(self):
        self.create('PO-4108', ofac.DEMO_PAYEE_NAME, ofac.DEMO_PAYEE_ACCOUNT, 200000)
        items = self.reasons('PO-4108')
        self.assertEqual(CODES(items),
                         [payouts.NEW_PAYEE_OVER_1000, payouts.POSSIBLE_SANCTIONS_MATCH])

    def test_new_payee_and_bank_change_together(self):
        self.create('PO-5102', 'Oak Street Books', 'DEMO-OAKS-010', 100001,
                    changed=NOW.isoformat())
        items = self.reasons('PO-5102')
        self.assertEqual(CODES(items),
                         [payouts.NEW_PAYEE_OVER_1000, payouts.BANK_CHANGED_7D])
        self.assertEqual(items[1]['details']['days_ago'], 0)

    def test_amount_and_bank_change_together(self):
        # Acme has usual $790; $4,850 is already over 3×. Adding a bank change
        # on a new payout to the same payee keeps both reasons.
        self.create('PO-1048', 'Acme Supplies', 'DEMO-ACME-001', 485000,
                    changed=NOW.isoformat())
        items = self.reasons('PO-1048')
        self.assertEqual(CODES(items),
                         [payouts.AMOUNT_OVER_3X_AVG, payouts.BANK_CHANGED_7D])

    # FEATURE 09 — Scenario — a later payout to a settled payee is not a new payee
    def test_later_payout_to_a_settled_payee_is_not_a_new_payee(self):
        self.create('PO-5103', 'Oak Street Books', 'DEMO-OAKS-010', 100001)
        payouts.decide(self.db, 'PO-5103', 'approve', 'Desk operator',
                       'approve:PO-5103', now=NOW)
        later = datetime(2026, 11, 13, 16, 0, tzinfo=timezone.utc)
        payout_rules.create(self.db, 'PO-5105', 'Oak Street Books', 'DEMO-OAKS-010',
                            100001, 'ACH', now=later)
        items = self.reasons('PO-5105')
        self.assertNotIn(payouts.NEW_PAYEE_OVER_1000, CODES(items))
        # Same payee and amount within 24 hours, so it is held as a duplicate.
        self.assertIn(payouts.DUPLICATE_24H, CODES(items))
        self.assertEqual(set(CODES(items)),
                         set(stored_flag_codes(self.db, 'PO-5105')))


class ReviewReasonsHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(self.temp.name, 'https://demo.example')
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.extensions['close']()
        self.temp.cleanup()

    def open_desk(self):
        return self.client.get('/desk', base_url='https://demo.example')

    def get_reasons(self, payout_id):
        return self.client.get(f'/payouts/{payout_id}/review-reasons',
                               base_url='https://demo.example')

    # FEATURE 10 — Scenario — the GET endpoint returns the structured list
    def test_get_review_reasons_for_a_seed_payout(self):
        self.open_desk()
        reply = self.get_reasons('PO-1042')
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.json['payout_id'], 'PO-1042')
        codes = CODES(reply.json['reasons'])
        self.assertEqual(codes, [payouts.AMOUNT_OVER_3X_AVG])
        self.assertEqual(reply.json['reasons'][0]['details']['ratio'], 6)
        self.assertEqual(reply.json['reasons'][0]['label'],
                         'Amount is 6× this payee’s usual payout ($790 avg)')

    def test_get_review_reasons_for_sanctions_demo_payee(self):
        self.open_desk()
        created = self.client.post(
            '/payouts/simulate',
            json={'payee_name': ofac.DEMO_PAYEE_NAME, 'payee_account': ofac.DEMO_PAYEE_ACCOUNT,
                  'amount_cents': ofac.DEMO_AMOUNT_CENTS, 'rail': 'ACH', 'payout_id': 'PO-4100'},
            base_url='https://demo.example',
            headers={'Origin': 'https://demo.example'})
        self.assertEqual(created.status_code, 201)
        reply = self.get_reasons('PO-4100')
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(CODES(reply.json['reasons']),
                         [payouts.POSSIBLE_SANCTIONS_MATCH])
        details = reply.json['reasons'][0]['details']
        self.assertEqual(details['score'], 100)
        self.assertEqual(details['list_published'], '2026-11-09')
        self.assertEqual(details['snapshot_taken'], '2026-11-12')
        self.assertNotIn('uid', details)

    def test_get_missing_payout_is_404(self):
        self.open_desk()
        reply = self.get_reasons('PO-9999')
        self.assertEqual(reply.status_code, 404)

    def test_get_sent_payout_returns_an_empty_list(self):
        self.open_desk()
        reply = self.get_reasons('PO-1039')
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.json['reasons'], [])

    def test_valid_routing_number_is_not_a_stored_reason(self):
        self.open_desk()
        created = self.client.post(
            '/payouts/simulate',
            json={'payee_name': 'Oak Street Books', 'payee_account': 'DEMO-OAKS-010',
                  'amount_cents': 100001, 'rail': 'ACH', 'payout_id': 'PO-5110',
                  'routing_number': '110000000'},
            base_url='https://demo.example',
            headers={'Origin': 'https://demo.example'})
        self.assertEqual(created.status_code, 201)
        reply = self.get_reasons('PO-5110')
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(CODES(reply.json['reasons']), [payouts.NEW_PAYEE_OVER_1000])
        blob = str(reply.json)
        self.assertNotIn('routing', blob.lower())
        self.assertNotIn('ABA', blob)


if __name__ == '__main__':
    unittest.main()
