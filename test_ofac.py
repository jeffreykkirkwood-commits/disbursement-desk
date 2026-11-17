# Automated checks for OFAC SDN screening (#111): exact, alias and fuzzy name
# matches, a clear (no match), holding a possible match, and the reviewer clear
# or block path. Creation and pre-payout screening are both covered. Payees and
# accounts used here are synthetic; the snapshot names are public SDN entries.
"""Exercise dated OFAC SDN screening and the Desk clear/block path."""
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import ofac
import payout_notes
import payout_rules
import payouts
from hosted_app import create_app
from desk_common import APIError

NOW = datetime(2026, 11, 12, 15, 0, tzinfo=timezone.utc)
DEMO = {
    'payee_name': ofac.DEMO_PAYEE_NAME,
    'payee_account': ofac.DEMO_PAYEE_ACCOUNT,
    'amount_cents': ofac.DEMO_AMOUNT_CENTS,
    'rail': 'ACH',
    'payout_id': 'PO-4100',
}


class OfacScreeningTests(unittest.TestCase):
    # FEATURE 01 — Scenario — the committed snapshot is dated and names its source
    def test_snapshot_records_source_and_date(self):
        snap = ofac.load_snapshot()
        self.assertEqual(snap['source_url'],
                         'https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.XML')
        self.assertEqual(snap['source_page'], 'https://ofac.treasury.gov/sanctions-list-service')
        self.assertEqual(snap['list_publish_date'], '2026-11-09')
        self.assertEqual(snap['snapshot_date'], '2026-11-12')
        self.assertGreaterEqual(len(snap['entries']), 20)
        banco = next(entry for entry in snap['entries'] if entry['uid'] == '306')
        self.assertEqual(banco['name'].upper(), 'BANCO NACIONAL DE CUBA')
        aliases = {alias.upper() for alias in banco['aliases']}
        self.assertIn('NATIONAL BANK OF CUBA', aliases)
        self.assertIn('BNC', aliases)

    # FEATURE 02 — Scenario — exact, alias, fuzzy and non-matching names
    def test_exact_alias_fuzzy_and_non_match(self):
        exact = ofac.screen('Banco Nacional de Cuba')
        self.assertIsNotNone(exact)
        self.assertEqual(exact['entry_name'].upper(), 'BANCO NACIONAL DE CUBA')
        self.assertEqual(exact['score'], 100)
        self.assertEqual(exact['list_published'], '2026-11-09')
        self.assertEqual(exact['snapshot_taken'], '2026-11-12')
        self.assertNotIn('snapshot_date', exact)
        self.assertIn('BANCO NACIONAL DE CUBA', ofac.flag_sentence(exact).upper())
        self.assertIn('score 100', ofac.flag_sentence(exact))
        self.assertIn('OFAC SDN list published 2026-11-09', ofac.flag_sentence(exact))
        self.assertIn('snapshot taken 2026-11-12', ofac.flag_sentence(exact))
        self.assertNotIn('snapshot dated', ofac.flag_sentence(exact))

        alias = ofac.screen('National Bank of Cuba')
        self.assertIsNotNone(alias)
        self.assertEqual(alias['matched_name'].upper(), 'NATIONAL BANK OF CUBA')
        self.assertEqual(alias['entry_name'].upper(), 'BANCO NACIONAL DE CUBA')
        self.assertEqual(alias['score'], 100)
        self.assertIn('listed as', ofac.flag_sentence(alias))

        fuzzy = ofac.screen('Banco Nasional de Cuba')
        self.assertIsNotNone(fuzzy)
        self.assertGreaterEqual(fuzzy['score'], int(ofac.MATCH_THRESHOLD * 100))
        self.assertLess(fuzzy['score'], 100)
        self.assertEqual(fuzzy['entry_name'].upper(), 'BANCO NACIONAL DE CUBA')

        self.assertIsNone(ofac.screen('Oak Street Books'))
        self.assertIsNone(ofac.screen('Acme Supplies'))
        self.assertIsNone(ofac.screen(''))
        self.assertIsNone(ofac.screen(None))
        # A short alias only matches exactly, so a near miss does not flag.
        self.assertIsNotNone(ofac.screen('BNC'))
        self.assertIsNone(ofac.screen('BND'))

    # FEATURE 03 — Scenario — "Last, First" and reversed word order also match
    def test_last_first_name_order_matches(self):
        listed = ofac.screen('Abdelmalek DROUKDEL')
        self.assertIsNotNone(listed)
        self.assertEqual(listed['score'], 100)
        self.assertEqual(listed['entry_name'], 'Abdelmalek DROUKDEL')

        comma = ofac.screen('DROUKDEL, Abdelmalek')
        self.assertIsNotNone(comma)
        self.assertEqual(comma['score'], 100)
        self.assertEqual(comma['entry_name'], 'Abdelmalek DROUKDEL')

        reversed_order = ofac.screen('DROUKDEL Abdelmalek')
        self.assertIsNotNone(reversed_order)
        self.assertEqual(reversed_order['score'], 100)
        self.assertEqual(reversed_order['entry_name'], 'Abdelmalek DROUKDEL')

    # FEATURE 04 — Scenario — none of the starting seed payees is a possible match
    def test_seed_payees_are_not_matches(self):
        names = {row['payee_name'] for row in payouts.seed_rows()}
        names.add('Oak Street Books')
        for name in sorted(names):
            with self.subTest(payee=name):
                self.assertIsNone(ofac.screen(name))


class OfacPayoutFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'payments.sqlite3'
        payouts.ensure_seeded(self.db)
        self.seeded = json.dumps(payouts.list_payouts(self.db), sort_keys=True)

    def tearDown(self):
        self.temp.cleanup()

    def create(self, payout_id, name, account, cents, rail='ACH'):
        return payout_rules.create(self.db, payout_id, name, account, cents, rail, now=NOW)

    def decide(self, payout_id, action, key=None):
        return payouts.decide(self.db, payout_id, action, 'Desk operator',
                              key or f'{action}:{payout_id}', now=NOW)

    # FEATURE 04 — Scenario — creating a matching payee holds the payout
    def test_creation_screens_and_holds_a_match(self):
        result = self.create('PO-4100', ofac.DEMO_PAYEE_NAME, ofac.DEMO_PAYEE_ACCOUNT,
                             ofac.DEMO_AMOUNT_CENTS)
        self.assertEqual(result['status'], 'FLAGGED')
        payout = payouts.get_payout(self.db, 'PO-4100')
        self.assertEqual(payout['status'], 'FLAGGED')
        self.assertEqual(payout['review_status'], 'FLAGGED')
        self.assertEqual(payout['execution_status'], 'NOT_STARTED')
        self.assertEqual(payout['flag_code'], ofac.FLAG_CODE)
        self.assertIn('Possible sanctions match', payout['flag_text'])
        self.assertIn('BANCO NACIONAL DE CUBA', payout['flag_text'])
        self.assertIn('score 100', payout['flag_text'])
        self.assertIn('OFAC SDN list published 2026-11-09', payout['flag_text'])
        self.assertIn('snapshot taken 2026-11-12', payout['flag_text'])
        self.assertIn(f'Rule {ofac.FLAG_CODE}:', payout['note_text'])
        self.assertEqual(payout['note_source'], 'rules')
        self.assertFalse(payout['note_fallback'])
        # Starting set is unchanged besides this new row.
        remaining = [row for row in payouts.list_payouts(self.db) if row['payout_id'] != 'PO-4100']
        self.assertEqual(json.dumps(remaining, sort_keys=True), self.seeded)

    def test_creation_screens_alias_and_fuzzy_names(self):
        alias = self.create('PO-4101', 'National Bank of Cuba', 'DEMO-OFAC-012', 50000)
        self.assertEqual(alias['status'], 'FLAGGED')
        payout = payouts.get_payout(self.db, 'PO-4101')
        self.assertEqual(payout['flag_code'], ofac.FLAG_CODE)
        self.assertIn('NATIONAL BANK OF CUBA', payout['flag_text'])
        self.assertIn('listed as BANCO NACIONAL DE CUBA', payout['flag_text'])

        fuzzy = self.create('PO-4102', 'Banco Nasional de Cuba', 'DEMO-OFAC-013', 50000)
        self.assertEqual(fuzzy['status'], 'FLAGGED')
        self.assertEqual(payouts.get_payout(self.db, 'PO-4102')['flag_code'], ofac.FLAG_CODE)

    # FEATURE 05 — Scenario — a name that does not match is paid when no other rule fires
    def test_creation_clears_a_non_match(self):
        result = self.create('PO-4103', 'Oak Street Books', 'DEMO-OAKS-010', 50000)
        self.assertEqual(result['status'], 'PAID')
        payout = payouts.get_payout(self.db, 'PO-4103')
        self.assertIsNone(payout['flag_code'])
        self.assertEqual(payout['note_text'], payout_notes.CLEAN_NOTE)

    # FEATURE 06 — Scenario — a reviewer can clear or block a possible match
    def test_reviewer_can_clear_a_possible_match(self):
        self.create('PO-4100', ofac.DEMO_PAYEE_NAME, ofac.DEMO_PAYEE_ACCOUNT, ofac.DEMO_AMOUNT_CENTS)
        status, event = self.decide('PO-4100', 'clear')
        self.assertEqual(status, 201)
        self.assertEqual(event['action'], 'clear')
        self.assertEqual(event['from_status'], 'FLAGGED')
        self.assertEqual(event['to_status'], 'PAID')
        payout = payouts.get_payout(self.db, 'PO-4100')
        self.assertEqual(payout['status'], 'PAID')
        self.assertEqual(payout['review_status'], 'APPROVED')
        self.assertEqual(payout['execution_status'], 'SETTLED')
        self.assertEqual(payout['flag_code'], ofac.FLAG_CODE)

    def test_reviewer_can_block_a_possible_match(self):
        self.create('PO-4100', ofac.DEMO_PAYEE_NAME, ofac.DEMO_PAYEE_ACCOUNT, ofac.DEMO_AMOUNT_CENTS)
        status, event = self.decide('PO-4100', 'block')
        self.assertEqual(status, 201)
        self.assertEqual(event['action'], 'block')
        self.assertEqual(event['from_status'], 'FLAGGED')
        self.assertEqual(event['to_status'], 'RETURNED')
        payout = payouts.get_payout(self.db, 'PO-4100')
        self.assertEqual(payout['status'], 'RETURNED')
        self.assertEqual(payout['review_status'], 'RETURNED')
        self.assertEqual(payout['execution_status'], 'NOT_STARTED')

    # FEATURE 07 — Scenario — a payout is screened again before it is sent
    def test_pre_payout_screen_holds_an_unscreened_match(self):
        at = NOW.isoformat()
        with sqlite3.connect(self.db) as db:
            db.execute(
                f'INSERT INTO payouts ({", ".join(payouts.COLUMNS)}) VALUES '
                f'({", ".join("?" * len(payouts.COLUMNS))})',
                ('PO-4110', ofac.DEMO_PAYEE_NAME, ofac.DEMO_PAYEE_ACCOUNT, ofac.DEMO_PAYEE_NAME,
                 ofac.DEMO_AMOUNT_CENTS, 'USD', 'ACH', 'FLAGGED', None, None, None, None,
                 None, None, None, at, at, 'FLAGGED', 'NOT_STARTED', None, None, None))
        status, event = self.decide('PO-4110', 'approve', key='approve-unscreened-4110')
        self.assertEqual(status, 201)
        self.assertEqual(event['action'], 'screen')
        self.assertEqual(event['to_status'], 'FLAGGED')
        payout = payouts.get_payout(self.db, 'PO-4110')
        self.assertEqual(payout['status'], 'FLAGGED')
        self.assertEqual(payout['execution_status'], 'NOT_STARTED')
        self.assertEqual(payout['flag_code'], ofac.FLAG_CODE)
        self.assertIn('Possible sanctions match', payout['flag_text'])
        self.assertNotEqual(payout['status'], 'PAID')

    # FEATURE 08 — Scenario — Approve cannot send a possible sanctions match
    def test_approve_on_possible_match_returns_409(self):
        self.create('PO-4100', ofac.DEMO_PAYEE_NAME, ofac.DEMO_PAYEE_ACCOUNT, ofac.DEMO_AMOUNT_CENTS)
        before = payouts.get_payout(self.db, 'PO-4100')
        events_before = payouts.list_events(self.db, 'PO-4100')
        with self.assertRaises(APIError) as caught:
            self.decide('PO-4100', 'approve')
        self.assertEqual(caught.exception.status, 409)
        self.assertIn('use Clear or Block', caught.exception.message)
        after = payouts.get_payout(self.db, 'PO-4100')
        self.assertEqual(after, before)
        self.assertEqual(after['status'], 'FLAGGED')
        self.assertEqual(after['execution_status'], 'NOT_STARTED')
        self.assertEqual(payouts.list_events(self.db, 'PO-4100'), events_before)

    # FEATURE 09 — Scenario — Clear and Block require a possible sanctions match flag
    def test_clear_and_block_without_sanctions_flag_return_409(self):
        for payout_id, action in (('PO-1042', 'clear'), ('PO-1043', 'block')):
            with self.subTest(payout_id=payout_id, action=action):
                before = payouts.get_payout(self.db, payout_id)
                self.assertNotIn(ofac.FLAG_CODE, payouts.split_flag_codes(before['flag_code']))
                with self.assertRaises(APIError) as caught:
                    self.decide(payout_id, action)
                self.assertEqual(caught.exception.status, 409)
                self.assertIn('possible sanctions match', caught.exception.message)
                self.assertEqual(payouts.get_payout(self.db, payout_id), before)
                self.assertEqual(payouts.list_events(self.db, payout_id), [])


class OfacDeskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(self.temp.name, 'https://demo.example')
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.extensions['close']()
        self.temp.cleanup()

    def open_desk(self):
        return self.client.get('/desk', base_url='https://demo.example')

    def simulate(self, body=None):
        payout_id = (body or DEMO).get('payout_id', 'PO-4100')
        return self.client.post('/payouts/simulate', json=body or DEMO,
                                base_url='https://demo.example',
                                headers={'Origin': 'https://demo.example',
                                         'Idempotency-Key': f'ofac-sim-{payout_id}'})

    def decide(self, payout_id, action):
        return self.client.post(f'/payouts/{payout_id}/{action}',
                                json={},
                                base_url='https://demo.example',
                                headers={'Origin': 'https://demo.example',
                                         'Idempotency-Key': f'desk-{action}-{payout_id}'})

    # FEATURE 08 — Scenario — the Desk shows the flag and Clear / Block
    def test_desk_shows_possible_match_flag_and_clear_block(self):
        seed = self.open_desk().get_data(as_text=True)
        self.assertIn('Exceptions <b>4</b>', seed)
        self.assertIn('All <b>10</b>', seed)
        self.assertNotIn('Possible sanctions match', seed)
        reply = self.simulate()
        self.assertEqual(reply.status_code, 201)
        self.assertEqual(reply.json['status'], 'FLAGGED')
        html = self.open_desk().get_data(as_text=True)
        self.assertIn('Exceptions <b>5</b>', html)
        self.assertIn('All <b>11</b>', html)
        self.assertIn('Banco Nacional de Cuba', html)
        self.assertIn('Possible sanctions match', html)
        self.assertIn('BANCO NACIONAL DE CUBA', html)
        self.assertIn('score 100', html)
        self.assertIn('OFAC SDN list published 2026-11-09', html)
        self.assertIn('snapshot taken 2026-11-12', html)
        self.assertIn('data-act="clear"', html)
        self.assertIn('>Clear</button>', html)
        self.assertIn('data-act="block"', html)
        self.assertIn('>Block</button>', html)
        # The new payout's panel does not keep Approve / Hold / Return.
        panel = html.split('<div class="panel" data-id="PO-4100"', 1)[1].split('<div class="panel"', 1)[0]
        self.assertNotIn('data-act="approve"', panel)
        self.assertNotIn('data-act="hold"', panel)
        self.assertNotIn('data-act="return"', panel)
        self.assertIn('data-act="clear"', panel)
        self.assertIn('data-act="block"', panel)
        # The starting exceptions still use Approve / Hold / Return.
        acme = html.split('<div class="panel" data-id="PO-1042"', 1)[1].split('<div class="panel"', 1)[0]
        self.assertIn('data-act="approve"', acme)
        self.assertIn('data-act="hold"', acme)
        self.assertIn('data-act="return"', acme)

    def test_desk_clear_sends_and_block_cancels(self):
        self.open_desk()
        self.simulate()
        cleared = self.decide('PO-4100', 'clear')
        self.assertEqual(cleared.status_code, 201)
        self.assertEqual(cleared.json['payout']['status'], 'PAID')
        html = self.open_desk().get_data(as_text=True)
        self.assertIn('Exceptions <b>4</b>', html)
        self.assertIn('>Sent</span>', html.split('data-id="PO-4100"', 1)[1][:800])

        reset = self.client.post('/payouts/reset', base_url='https://demo.example',
                                 headers={'Origin': 'https://demo.example'})
        self.assertEqual(reset.status_code, 200)
        self.open_desk()
        self.simulate({'payee_name': ofac.DEMO_PAYEE_NAME, 'payee_account': ofac.DEMO_PAYEE_ACCOUNT,
                       'amount_cents': ofac.DEMO_AMOUNT_CENTS, 'rail': 'ACH', 'payout_id': 'PO-4105'})
        blocked = self.decide('PO-4105', 'block')
        self.assertEqual(blocked.status_code, 201)
        self.assertEqual(blocked.json['payout']['status'], 'RETURNED')
        html = self.open_desk().get_data(as_text=True)
        self.assertIn('>Returned</span>', html.split('data-id="PO-4105"', 1)[1][:800])


if __name__ == '__main__':
    unittest.main()
