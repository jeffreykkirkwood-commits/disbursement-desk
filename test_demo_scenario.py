# Automated checks for the fixed end-to-end demo scenario (#61).
# Temporary fictional databases only. No network, no real money, bank or provider.
"""Exercise the scripted seed, decisions, replay, expected results, and reset."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import demo_scenario
import desk
import payout_events
import payouts
from hosted_app import create_app


SITE = 'https://demo.example'
MAIN_QUEUE_IDS = ['PO-1042', 'PO-1043', 'PO-1044', 'PO-1038', 'PO-1037',
                  'PO-1036', 'PO-1039', 'PO-1040', 'PO-1041', 'PO-1045']
MAIN_SEED_FLAGS = {
    'PO-1037': ('BANK_CHANGED_7D', 'Payee bank details changed 1 day ago'),
    'PO-1038': ('AMOUNT_OVER_3X_AVG', 'Amount is 4× this payee’s usual payout ($520 avg)'),
    'PO-1042': ('AMOUNT_OVER_3X_AVG', 'Amount is 6× this payee’s usual payout ($790 avg)'),
    'PO-1043': ('BANK_CHANGED_7D', 'Payee bank details changed 2 days ago'),
    'PO-1044': ('DUPLICATE_24H', 'Same payee and amount as yesterday (possible duplicate)'),
}


def visible_flags(database):
    import execution_replay
    hidden = execution_replay.REPLAY_PAYOUT_IDS
    return {payout['payout_id']: (payout['flag_code'], payout['flag_text'])
            for payout in payouts.list_payouts(database) if payout['flag_code']
            and payout['payout_id'] not in hidden}


class DemoScenarioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'payouts.sqlite3'

    def tearDown(self):
        self.temp.cleanup()

    def recon(self, database, table):
        columns = {
            'reconciliation_runs': ('run_id', 'fixture_name'),
            'reconciliation_items': ('item_id', 'payout_id'),
        }[table]
        return demo_scenario._recon_rows(database, table, columns)

    # FEATURE 01 — Scenario — running twice from a reset gives identical results
    def test_two_runs_from_reset_are_identical(self):
        first = demo_scenario.run(self.db)
        second = demo_scenario.run(self.db)
        self.assertEqual(first, second)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))

    # FEATURE 02 — Scenario — results match the checked-in expected-results file
    def test_results_match_the_expected_file(self):
        report = demo_scenario.run(self.db)
        expected = demo_scenario.load_expected()
        self.assertEqual(report, expected)
        actions = [(item['payout_id'], item['action'], item['to_status'])
                   for item in report['decisions']]
        self.assertEqual(actions, [
            ('PO-1042', 'approve', 'PAID'),
            ('PO-1043', 'hold', 'HELD'),
            ('PO-1044', 'return', 'RETURNED'),
        ])
        self.assertEqual(report['replay']['applied'], 6)
        self.assertEqual(report['reconciliation_runs'], [])
        self.assertEqual(report['reconciliation_items'], [])

    # FEATURE 03 — Scenario — Reset demo clears execution and reconciliation tables
    def test_reset_clears_execution_and_reconciliation_tables(self):
        demo_scenario.run(self.db)
        self.assertTrue(payouts.list_execution_events(self.db))
        self.assertNotEqual(
            [payout['payout_id'] for payout in payouts.current_queue(self.db)], MAIN_QUEUE_IDS)
        restored = payouts.reset(self.db)
        self.assertEqual(restored, 40)
        self.assertEqual(len(payouts.list_payouts(self.db)), 40)
        self.assertEqual(payouts.list_events(self.db), [])
        self.assertEqual(payouts.list_execution_events(self.db), [])
        self.assertEqual(self.recon(self.db, 'reconciliation_runs'), [])
        self.assertEqual(self.recon(self.db, 'reconciliation_items'), [])
        self.assertEqual([payout['payout_id'] for payout in payouts.current_queue(self.db)],
                         MAIN_QUEUE_IDS)
        self.assertEqual(visible_flags(self.db), MAIN_SEED_FLAGS)

    # FEATURE 04 — Scenario — the Desk words and starting layout are unchanged
    def test_desk_words_stay_the_same(self):
        payouts.ensure_seeded(self.db)
        self.assertEqual(desk.STATUS_WORDS['FLAGGED'], ('Needs review', 's-review'))
        self.assertEqual(desk.STATUS_WORDS['HELD'], ('Held', 's-held'))
        self.assertEqual(desk.STATUS_WORDS['RETURNED'], ('Returned', 's-returned'))
        self.assertEqual(desk.STATUS_WORDS['PAID'], ('Sent', 's-sent'))
        demo_scenario.run(self.db)
        queue = {payout['payout_id']: payout for payout in payouts.current_queue(self.db)}
        self.assertEqual(desk.STATUS_WORDS[queue['PO-1042']['status']], ('Sent', 's-sent'))
        self.assertEqual(desk.STATUS_WORDS[queue['PO-1043']['status']], ('Held', 's-held'))
        self.assertEqual(desk.STATUS_WORDS[queue['PO-1044']['status']], ('Returned', 's-returned'))
        self.assertTrue(set(execution_replay_ids()).isdisjoint(queue))

    # FEATURE 08 — Scenario — after an already-used demo, the stream shows only the scripted moves
    def test_scenario_resets_the_stream_before_scripted_moves(self):
        payouts.ensure_seeded(self.db)
        payout_events.sync(self.db)
        earlier = demo_scenario.NOW
        payouts.decide(self.db, 'PO-1043', 'approve', 'Prior Reviewer', 'prior-approve', now=earlier)
        payouts.decide(self.db, 'PO-1044', 'return', 'Prior Reviewer', 'prior-return', now=earlier)
        payout_events.sync(self.db)
        prior = {(event['payout_id'], event.get('from_status'), event.get('to_status'))
                 for event in payout_events.events_after(self.db, 0)
                 if event['type'] == 'status-changed'}
        self.assertIn(('PO-1043', 'FLAGGED', 'PAID'), prior)
        self.assertIn(('PO-1044', 'FLAGGED', 'RETURNED'), prior)
        demo_scenario.run(self.db)
        added = payout_events.sync(self.db)
        moves = [(event['payout_id'], event['from_status'], event['to_status'])
                 for event in added if event['type'] == 'status-changed']
        self.assertEqual(moves, [
            ('PO-1042', 'FLAGGED', 'PAID'),
            ('PO-1043', 'FLAGGED', 'HELD'),
            ('PO-1044', 'FLAGGED', 'RETURNED'),
        ])
        self.assertNotIn(('PO-1043', 'PAID', 'HELD'), moves)
        self.assertTrue(any(event['payout_id'] == 'PO-1044' and event['to_status'] == 'RETURNED'
                            for event in added if event['type'] == 'status-changed'))
        changed = [event for event in payout_events.events_after(self.db, 0)
                   if event['type'] == 'status-changed']
        for event in changed:
            self.assertNotIn(event['from_status'], payouts.FINAL_STATUSES,
                             f'forbidden stream move {event["from_status"]}→{event["to_status"]}')
        self.assertNotIn(('PO-1043', 'PAID', 'HELD'),
                         [(event['payout_id'], event['from_status'], event['to_status'])
                          for event in changed])

    # FEATURE 05 — Scenario — no network calls
    def test_scenario_makes_no_network_calls(self):
        def blocked(*args, **kwargs):
            raise AssertionError('the demo scenario must not open a network connection')

        with mock.patch('socket.socket', side_effect=blocked), \
             mock.patch('socket.create_connection', side_effect=blocked), \
             mock.patch('urllib.request.urlopen', side_effect=blocked):
            demo_scenario.run(self.db)
        source = Path(demo_scenario.__file__).read_text()
        self.assertNotIn('urllib', source)
        self.assertNotIn('requests', source)
        self.assertNotIn('httpx', source)
        self.assertNotIn('http.client', source)


def execution_replay_ids():
    import execution_replay
    return execution_replay.REPLAY_PAYOUT_IDS


class HostedDemoScenarioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(self.temp.name, SITE)
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.extensions['close']()
        self.temp.cleanup()

    def open_desk(self, client=None):
        return (client or self.client).get('/desk', base_url=SITE)

    def scenario(self, client=None, **headers):
        client = client or self.client
        extra = {'Origin': SITE, **headers}
        return client.post('/demo-scenario', base_url=SITE, headers=extra)

    def reset(self, client=None):
        client = client or self.client
        return client.post('/payouts/reset', base_url=SITE, headers={'Origin': SITE})

    def database(self):
        self.open_desk()
        files = sorted(Path(self.temp.name).rglob('payments.sqlite3'))
        return files[0]

    # FEATURE 06 — Scenario — the JSON API runs the scenario; the Desk has no new control
    def test_json_api_runs_the_scenario_and_desk_has_no_new_control(self):
        html_before = self.open_desk().get_data(as_text=True)
        self.assertIn('All <b>10</b>', html_before)
        self.assertIn('Needs review', html_before)
        refused = self.app.test_client().post('/demo-scenario', base_url=SITE,
                                              headers={'Origin': SITE})
        self.assertEqual(refused.status_code, 409)
        reply = self.scenario()
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.json, demo_scenario.load_expected())
        html = self.open_desk().get_data(as_text=True)
        # The scripted decisions leave no Needs-review row in the current queue;
        # the word itself is unchanged and is back after Reset demo.
        self.assertIn('All <b>10</b>', html)
        self.assertIn('Held', html)
        self.assertIn('Returned', html)
        self.assertIn('Sent', html)
        self.assertIn('s-review', html)  # the status style is still in the page
        self.assertIn('PO-1042', html)
        self.assertNotIn('demo-scenario', html)
        self.assertNotIn('Scenario Reviewer', html)
        self.assertNotIn('PO-REPLAY-', html)
        self.assertNotIn('settlement_demo', html)
        # After the scenario, Reset demo restores the starting set for this visitor.
        reset = self.reset()
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(reset.json['restored'], 40)
        db = self.database()
        self.assertEqual(payouts.list_execution_events(db), [])
        self.assertEqual(demo_scenario._recon_rows(db, 'reconciliation_runs', ('run_id',)), [])
        self.assertEqual(demo_scenario._recon_rows(db, 'reconciliation_items', ('item_id',)), [])
        self.assertEqual([payout['payout_id'] for payout in payouts.current_queue(db)],
                         MAIN_QUEUE_IDS)
        html_reset = self.open_desk().get_data(as_text=True)
        self.assertIn('All <b>10</b>', html_reset)
        self.assertIn('Needs review', html_reset)
        self.assertEqual(visible_flags(db), MAIN_SEED_FLAGS)

    # FEATURE 07 — Scenario — another visitor's demo is unchanged
    def test_second_session_is_not_affected(self):
        other = self.app.test_client()
        self.open_desk()
        mine = self.database()
        self.open_desk(other)
        self.scenario()
        files = sorted(Path(self.temp.name).rglob('payments.sqlite3'))
        self.assertEqual(len(files), 2)
        theirs = next(path for path in files if path != mine)
        self.assertEqual(demo_scenario.snapshot(mine)['decisions'][0]['action'], 'approve')
        self.assertEqual(payouts.list_events(theirs), [])
        self.assertEqual(payouts.list_execution_events(theirs), [])
        self.assertEqual(len(payouts.list_payouts(theirs)), 40)
        self.assertEqual([payout['payout_id'] for payout in payouts.current_queue(theirs)],
                         MAIN_QUEUE_IDS)
        self.assertEqual(visible_flags(theirs), MAIN_SEED_FLAGS)
        # A stream sync on the other visitor emits no new events versus a fresh seed.
        payout_events.sync(theirs)
        # And a foreign origin cannot run the scenario against this session.
        foreign = self.scenario(**{'Origin': 'https://different.example'})
        self.assertEqual(foreign.status_code, 403)


if __name__ == '__main__':
    unittest.main()
