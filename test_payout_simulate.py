# Automated checks for the behind-the-scenes POST /payouts/simulate route (issue #91).
# It creates a synthetic payee payout in the visitor's own cookie-scoped demo only.
"""Exercise the demo-only payout create route with temporary fictional demos."""
import unittest
from pathlib import Path
import tempfile

import aba
from hosted_app import create_app
import payouts


NEW_PAYEE = {
    'payee_name': 'Oak Street Books',
    'payee_account': 'DEMO-OAKS-010',
    'amount_cents': 100001,
    'rail': 'ACH',
}


class SimulatePayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(self.temp.name, 'https://demo.example')
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.extensions['close']()
        self.temp.cleanup()

    def open_desk(self, client=None):
        client = client or self.client
        return client.get('/desk', base_url='https://demo.example')

    def simulate(self, body=None, client=None, idempotency_key=None, **headers):
        client = client or self.client
        key = idempotency_key or f'simulate-{getattr(self, "_sim_n", 0)}'
        self._sim_n = getattr(self, '_sim_n', 0) + 1
        extra = {'Origin': 'https://demo.example', 'Idempotency-Key': key, **headers}
        return client.post('/payouts/simulate', json=body or NEW_PAYEE,
                           base_url='https://demo.example', headers=extra)

    def reset(self, client=None):
        client = client or self.client
        return client.post('/payouts/reset', base_url='https://demo.example',
                           headers={'Origin': 'https://demo.example'})

    def database(self, client=None):
        self.open_desk(client or self.client)
        files = sorted(Path(self.temp.name).rglob('payments.sqlite3'))
        if client is None or client is self.client:
            return files[0] if len(files) == 1 else max(files, key=lambda p: p.stat().st_mtime)
        return files

    # FEATURE 01 — Scenario — a missing session or foreign origin is refused
    def test_requires_this_visitors_session(self):
        refused = self.simulate()
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.json['code'], 'DEMO_SESSION_CHANGED')
        self.open_desk()
        foreign = self.simulate(**{'Origin': 'https://different.example'})
        self.assertEqual(foreign.status_code, 403)
        self.assertEqual(len(payouts.list_payouts(self.database())), 40)

    # FEATURE 02 — Scenario — a first payout over $1,000 to a new payee is flagged
    def test_new_payee_over_1000_is_flagged_and_shows_on_desk(self):
        self.open_desk()
        reply = self.simulate(dict(NEW_PAYEE, payout_id='PO-3002'))
        self.assertEqual(reply.status_code, 201)
        self.assertEqual(reply.json['status'], 'FLAGGED')
        payout = reply.json['payout']
        self.assertEqual(payout['payout_id'], 'PO-3002')
        self.assertEqual(payout['payee_name'], 'Oak Street Books')
        self.assertEqual(payout['amount'], '1000.01')
        self.assertEqual(payout['status'], 'FLAGGED')
        self.assertEqual(payout['flag_code'], 'NEW_PAYEE_OVER_1000')
        self.assertIn('Rule NEW_PAYEE_OVER_1000:', payout['note_text'])
        self.assertEqual(payout['note_source'], 'rules')
        self.assertFalse(payout['note_fallback'])
        self.assertNotIn('Written by AI', payout['note_text'])
        html = self.open_desk().get_data(as_text=True)
        self.assertIn('Exceptions <b>5</b>', html)
        self.assertIn('All <b>11</b>', html)
        self.assertIn('Oak Street Books', html)
        self.assertIn('PO-3002', html)

    # FEATURE 03 — Scenario — a first payout of exactly $1,000 follows the normal rules
    def test_new_payee_of_1000_follows_normal_rules(self):
        self.open_desk()
        reply = self.simulate(dict(NEW_PAYEE, amount_cents=100000, payout_id='PO-3005'))
        self.assertEqual(reply.status_code, 201)
        self.assertEqual(reply.json['status'], 'PAID')
        self.assertIsNone(reply.json['payout']['flag_code'])
        html = self.open_desk().get_data(as_text=True)
        self.assertIn('Exceptions <b>4</b>', html)
        self.assertIn('All <b>11</b>', html)
        self.assertIn('PO-3005', html)

    # FEATURE 04 — Scenario — Reset demo removes the added payout
    def test_reset_removes_simulated_payout(self):
        self.open_desk()
        self.assertEqual(self.simulate(dict(NEW_PAYEE, payout_id='PO-3002')).status_code, 201)
        self.assertIn('PO-3002', self.open_desk().get_data(as_text=True))
        reply = self.reset()
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.json['restored'], 40)
        html = self.open_desk().get_data(as_text=True)
        self.assertIn('Exceptions <b>4</b>', html)
        self.assertIn('All <b>10</b>', html)
        self.assertNotIn('PO-3002', html)
        self.assertNotIn('Oak Street Books', html)
        self.assertEqual(len(payouts.list_payouts(self.database())), 40)

    # FEATURE 05 — Scenario — another visitor's demo is unchanged
    def test_only_this_visitors_demo_changes(self):
        other = self.app.test_client()
        self.open_desk()
        self.open_desk(other)
        self.assertEqual(self.simulate(dict(NEW_PAYEE, payout_id='PO-3002')).status_code, 201)
        mine = self.open_desk().get_data(as_text=True)
        theirs = self.open_desk(other).get_data(as_text=True)
        self.assertIn('Exceptions <b>5</b>', mine)
        self.assertIn('PO-3002', mine)
        self.assertIn('Exceptions <b>4</b>', theirs)
        self.assertIn('All <b>10</b>', theirs)
        self.assertNotIn('PO-3002', theirs)
        self.assertNotIn('Oak Street Books', theirs)

    # FEATURE 06 — Scenario — a payout id is assigned when none is given; bad accounts are refused
    def test_assigns_id_and_refuses_non_demo_accounts(self):
        self.open_desk()
        reply = self.simulate()
        self.assertEqual(reply.status_code, 201)
        self.assertEqual(reply.json['payout']['payout_id'], 'PO-1046')
        refused = self.simulate({'payee_name': 'Oak Street Books',
                                 'payee_account': 'REAL-BANK-001',
                                 'amount_cents': 100001})
        self.assertEqual(refused.status_code, 422)
        self.assertEqual(payouts.get_payout(self.database(), 'PO-1046')['payee_account'],
                         'DEMO-OAKS-010')

    # FEATURE 07 — Scenario — HTML has no simulate link; Desk script posts with Idempotency-Key
    def test_no_viewer_facing_control(self):
        html = self.open_desk().get_data(as_text=True)
        self.assertEqual(self.client.get('/dashboard', base_url='https://demo.example').status_code, 404)
        root = self.client.get('/', base_url='https://demo.example', follow_redirects=True)
        self.assertEqual(root.status_code, 200)
        desk_slash = self.client.get('/desk/', base_url='https://demo.example')
        self.assertEqual(desk_slash.status_code, 200)
        script = self.client.get('/desk.js', base_url='https://demo.example').get_data(as_text=True)
        for page in (html, root.get_data(as_text=True), desk_slash.get_data(as_text=True)):
            self.assertNotIn('/payouts/simulate', page)
            self.assertNotIn('simulate payout', page.lower())
        self.assertIn("fetch('/payouts/simulate'", script)
        self.assertIn('Idempotency-Key', script)
        self.assertNotIn('simulate payout', script.lower())

    # FEATURE 08 — Scenario — a bad bank-details time is 422, not 500, and saves nothing
    def test_invalid_bank_details_changed_at_is_refused(self):
        self.open_desk()
        db = self.database()
        before = payouts.list_payouts(db)
        before_events = payouts.list_events(db)
        for bad in ('nope', '2026-11-10', '2026-11-10T00:00:00'):
            with self.subTest(value=bad):
                reply = self.simulate(dict(NEW_PAYEE, payout_id='PO-3006',
                                           bank_details_changed_at=bad))
                self.assertEqual(reply.status_code, 422)
                self.assertIn('timezone offset', reply.json['error'])
                self.assertNotIn('idempotency', reply.json['error'].lower())
        self.assertEqual(payouts.list_payouts(db), before)
        self.assertEqual(payouts.list_events(db), before_events)
        ok = self.simulate(dict(
            NEW_PAYEE, payout_id='PO-3006',
            bank_details_changed_at='2026-11-10T18:00:00+00:00'))
        self.assertEqual(ok.status_code, 201)
        self.assertEqual(ok.json['payout']['bank_details_changed_at'],
                         '2026-11-10T18:00:00+00:00')
        self.assertEqual(ok.json['payout']['flag_code'], 'NEW_PAYEE_OVER_1000,BANK_CHANGED_7D')



    # FEATURE 09 — Scenario — a valid routing number is accepted; a bad check digit is 422 (#112)
    def test_valid_routing_number_is_accepted(self):
        self.open_desk()
        reply = self.simulate(dict(NEW_PAYEE, payout_id='PO-3010', amount_cents=50000,
                                   routing_number=aba.WELL_KNOWN_TEST))
        self.assertEqual(reply.status_code, 201)
        self.assertEqual(reply.json['payout']['payout_id'], 'PO-3010')
        self.assertEqual(reply.json['status'], 'PAID')

    def test_invalid_routing_check_digit_is_refused(self):
        self.open_desk()
        db = self.database()
        before = payouts.list_payouts(db)
        reply = self.simulate(dict(NEW_PAYEE, payout_id='PO-3011',
                                   routing_number='110000001'))
        self.assertEqual(reply.status_code, 422)
        self.assertEqual(reply.json['error'], aba.CHECK_MESSAGE)
        self.assertEqual(payouts.list_payouts(db), before)
        short = self.simulate(dict(NEW_PAYEE, payout_id='PO-3012',
                                   routing_number='11000000'))
        self.assertEqual(short.status_code, 422)
        self.assertEqual(short.json['error'], aba.LENGTH_MESSAGE)
        self.assertEqual(payouts.list_payouts(db), before)

    def test_non_ascii_routing_digits_are_refused(self):
        self.open_desk()
        db = self.database()
        before = payouts.list_payouts(db)
        cases = (('PO-3013', '²²²²²²²²²'), ('PO-3014', '١١٠٠٠٠٠٠٠'))
        for payout_id, number in cases:
            with self.subTest(routing_number=number):
                reply = self.simulate(dict(NEW_PAYEE, payout_id=payout_id,
                                           routing_number=number))
                self.assertEqual(reply.status_code, 422)
                self.assertNotEqual(reply.status_code, 500)
                self.assertEqual(reply.json['error'], aba.LENGTH_MESSAGE)
                self.assertEqual(payouts.list_payouts(db), before)

    # FEATURE 10 — Scenario — Idempotency-Key required; same key replays create
    def test_simulate_requires_idempotency_key(self):
        self.open_desk()
        reply = self.client.post(
            '/payouts/simulate', json=dict(NEW_PAYEE, payout_id='PO-3020'),
            base_url='https://demo.example', headers={'Origin': 'https://demo.example'})
        self.assertEqual(reply.status_code, 422)
        self.assertIn('Idempotency-Key', reply.json['error'])

    def test_simulate_idempotent_retry(self):
        self.open_desk()
        body = dict(NEW_PAYEE, payout_id='PO-3021', amount_cents=50000)
        first = self.simulate(body, idempotency_key='sim-retry-1')
        self.assertEqual(first.status_code, 201)
        self.assertFalse(first.json['replayed'])
        self.assertEqual(first.json['create_event']['action'], 'create')
        again = self.simulate(body, idempotency_key='sim-retry-1')
        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.json['replayed'])
        self.assertEqual(again.json['payout']['payout_id'], 'PO-3021')
        db = self.database()
        self.assertEqual(sum(1 for p in payouts.list_payouts(db) if p['payout_id'] == 'PO-3021'), 1)
        conflict = self.simulate(dict(body, amount_cents=50001), idempotency_key='sim-retry-1')
        self.assertEqual(conflict.status_code, 409)


if __name__ == '__main__':
    unittest.main()
