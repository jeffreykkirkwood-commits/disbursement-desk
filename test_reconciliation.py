# Automated checks for reconciliation against a hand-written settlement file (#57).
# Temporary fictional databases only. No network, no real money, bank or provider.
"""Exercise one reconciliation run of fixtures/settlement_demo.csv."""
import io
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import desk
import execution_replay
import payout_events
import payouts
import reconciliation
from hosted_app import create_app


SITE = 'https://demo.example'
NOW = datetime(2026, 11, 11, 20, 0, tzinfo=timezone.utc)
MAIN_QUEUE_IDS = ['PO-1042', 'PO-1043', 'PO-1044', 'PO-1038', 'PO-1037',
                  'PO-1036', 'PO-1039', 'PO-1040', 'PO-1041', 'PO-1045']
MAIN_SEED_FLAGS = {
    'PO-1037': ('BANK_CHANGED_7D', 'Payee bank details changed 1 day ago'),
    'PO-1038': ('AMOUNT_OVER_3X_AVG', 'Amount is 4× this payee’s usual payout ($520 avg)'),
    'PO-1042': ('AMOUNT_OVER_3X_AVG', 'Amount is 6× this payee’s usual payout ($790 avg)'),
    'PO-1043': ('BANK_CHANGED_7D', 'Payee bank details changed 2 days ago'),
    'PO-1044': ('DUPLICATE_24H', 'Same payee and amount as yesterday (possible duplicate)'),
}
EXPECTED_RESULTS = {
    'PO-RECON-MATCH': 'matched',
    'PO-RECON-STATUS': 'status_mismatch',
    'PO-RECON-AMOUNT': 'amount_mismatch',
    'PO-RECON-MISSING': 'missing_from_settlement',
    'PO-RECON-UNEXPECTED': 'unexpected_settlement',
    'PO-RECON-UNKNOWN': 'resolved',
}
EXPECTED_COUNTS = (2, 4, 0)


def seed_flags(database):
    return {payout['payout_id']: (payout['flag_code'], payout['flag_text'])
            for payout in payouts.list_payouts(database) if payout['flag_code']}


def seed_state(database):
    return {payout['payout_id']: (payout['review_status'], payout['status'],
                                 payout['execution_status'])
            for payout in payouts.list_payouts(database)
            if payout['payout_id'] not in reconciliation.RECON_PAYOUT_IDS}


def public_items(items):
    return [{key: item[key] for key in ('payout_id', 'our_status', 'settlement_status',
                                        'result', 'reason')}
            for item in items]


def by_result(report):
    return {item['payout_id']: item['result'] for item in report['items']}


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'payouts.sqlite3'

    def tearDown(self):
        self.temp.cleanup()

    def results(self, report=None):
        report = report or reconciliation.reconcile(self.db, now=NOW)
        return {item['payout_id']: item for item in report['items']}, report

    # FEATURE 01 — Scenario — the settlement file is a separate hand-written file
    def test_settlement_file_is_written_separately(self):
        text = reconciliation.FIXTURE.read_text()
        self.assertIn('Hand-written settlement file', text)
        self.assertIn('not generated', text.lower())
        event_file = Path(__file__).resolve().parent / 'fixtures' / 'execution_events_demo.json'
        self.assertTrue(event_file.exists())
        self.assertNotEqual(text, event_file.read_text())
        self.assertNotIn('evt-replay', text)
        self.assertNotIn('PO-REPLAY', text)

    # FEATURE 02 — Scenario — each required case produces the expected result row
    def test_each_case_produces_the_expected_row(self):
        by_id, report = self.results()
        self.assertEqual(by_id['PO-RECON-MATCH']['result'], 'matched')
        self.assertEqual(by_id['PO-RECON-MATCH']['our_status'], 'SETTLED')
        self.assertEqual(by_id['PO-RECON-MATCH']['settlement_status'], 'SETTLED')
        self.assertEqual(by_id['PO-RECON-STATUS']['result'], 'status_mismatch')
        self.assertEqual((by_id['PO-RECON-STATUS']['our_status'],
                          by_id['PO-RECON-STATUS']['settlement_status']),
                         ('SETTLED', 'FAILED'))
        self.assertEqual(by_id['PO-RECON-AMOUNT']['result'], 'amount_mismatch')
        self.assertIn('$270.00', by_id['PO-RECON-AMOUNT']['reason'])
        self.assertIn('$999.99', by_id['PO-RECON-AMOUNT']['reason'])
        self.assertEqual(by_id['PO-RECON-MISSING']['result'], 'missing_from_settlement')
        self.assertIsNone(by_id['PO-RECON-MISSING']['settlement_status'])
        self.assertEqual(by_id['PO-RECON-UNEXPECTED']['result'], 'unexpected_settlement')
        self.assertIsNone(by_id['PO-RECON-UNEXPECTED']['our_status'])
        self.assertEqual(by_id['PO-RECON-UNKNOWN']['result'], 'resolved')
        self.assertEqual(by_id['PO-RECON-UNKNOWN']['our_status'], 'SETTLED')
        self.assertEqual(by_id['PO-RECON-UNKNOWN']['settlement_status'], 'SETTLED')
        self.assertEqual(report['run']['fixture_name'], 'settlement_demo.csv')
        self.assertEqual((report['run']['matched_count'], report['run']['mismatched_count'],
                          report['run']['unknown_count']),
                         EXPECTED_COUNTS)
        self.assertEqual([item['payout_id'] for item in report['items']],
                         ['PO-RECON-AMOUNT', 'PO-RECON-MATCH', 'PO-RECON-MISSING',
                          'PO-RECON-STATUS', 'PO-RECON-UNEXPECTED', 'PO-RECON-UNKNOWN'])

    # FEATURE 03 — Scenario — UNKNOWN moves only when settlement confirms it, and the move is logged
    def test_unknown_is_resolved_and_logged(self):
        with self.assertLogs('reconciliation', level='INFO') as logs:
            report = reconciliation.reconcile(self.db, now=NOW)
        payout = payouts.get_payout(self.db, 'PO-RECON-UNKNOWN')
        self.assertEqual((payout['execution_status'], payout['execution_ref'],
                          payout['review_status'], payout['status']),
                         ('SETTLED', 'stl-unknown-1', 'APPROVED', 'APPROVED'))
        [event] = [row for row in payouts.list_execution_events(self.db)
                   if row['payout_id'] == 'PO-RECON-UNKNOWN']
        self.assertEqual(event['event_id'], f'recon:{report["run"]["run_id"]}:PO-RECON-UNKNOWN')
        self.assertEqual((event['execution_status'], event['source'], event['execution_ref']),
                         ('SETTLED', 'reconciliation', 'stl-unknown-1'))
        self.assertTrue(any('PO-RECON-UNKNOWN' in line and 'SETTLED' in line for line in logs.output))

    # FEATURE 04 — Scenario — an amount mismatch does not resolve UNKNOWN
    def test_unknown_is_not_resolved_when_the_amount_differs(self):
        path = Path(self.temp.name) / 'amount-unknown.csv'
        path.write_text(
            'payout_id,execution_status,amount_cents,settlement_ref\n'
            'PO-RECON-UNKNOWN,SETTLED,1,stl-wrong-amount\n')
        report = reconciliation.reconcile(self.db, path, now=NOW)
        by_id = {item['payout_id']: item for item in report['items']}
        self.assertEqual(by_id['PO-RECON-UNKNOWN']['result'], 'amount_mismatch')
        self.assertEqual(payouts.get_payout(self.db, 'PO-RECON-UNKNOWN')['execution_status'],
                         'UNKNOWN')
        self.assertEqual(payouts.list_execution_events(self.db, 'PO-RECON-UNKNOWN'), [])

    # FEATURE 05 — Scenario — re-running on the same starting data gives identical results
    def test_rerun_on_the_same_data_is_identical(self):
        copy = Path(self.temp.name) / 'copy.sqlite3'
        payouts.ensure_seeded(self.db)
        shutil.copy(self.db, copy)
        first = reconciliation.reconcile(self.db, now=NOW)
        second = reconciliation.reconcile(copy, now=NOW)
        self.assertEqual(public_items(first['items']), public_items(second['items']))
        self.assertEqual(
            {key: first['run'][key] for key in ('fixture_name', 'matched_count',
                                                'mismatched_count', 'unknown_count')},
            {key: second['run'][key] for key in ('fixture_name', 'matched_count',
                                                 'mismatched_count', 'unknown_count')})

    # FEATURE 06 — Scenario — a second run on the updated data does not change review or re-log
    def test_second_run_does_not_change_review_or_relog(self):
        first = reconciliation.reconcile(self.db, now=NOW)
        reviews = {payout['payout_id']: (payout['review_status'], payout['status'])
                   for payout in payouts.list_payouts(self.db)}
        events = payouts.list_execution_events(self.db)
        second = reconciliation.reconcile(self.db, now=NOW)
        self.assertEqual({payout['payout_id']: (payout['review_status'], payout['status'])
                          for payout in payouts.list_payouts(self.db)}, reviews)
        self.assertEqual(payouts.list_execution_events(self.db), events)
        by_id = {item['payout_id']: item for item in second['items']}
        self.assertEqual(by_id['PO-RECON-UNKNOWN']['result'], 'matched')
        self.assertEqual(by_id['PO-RECON-STATUS']['result'], 'status_mismatch')
        self.assertEqual(second['run']['run_id'], first['run']['run_id'] + 1)

    # FEATURE 07 — Scenario — mismatches never change a payout's review decision
    def test_mismatches_never_change_review_decision(self):
        reconciliation.reconcile(self.db, now=NOW)
        after = {payout['payout_id']: payout['review_status']
                 for payout in payouts.list_payouts(self.db)}
        # A first run prepares the hidden targets, so compare a second run against
        # this snapshot: mismatches still do not rewrite review_status or status.
        reconciliation.reconcile(self.db, now=NOW)
        self.assertEqual({payout['payout_id']: payout['review_status']
                          for payout in payouts.list_payouts(self.db)}, after)
        self.assertEqual(after['PO-RECON-MISSING'], 'APPROVED')
        self.assertEqual(after['PO-RECON-STATUS'], 'APPROVED')
        self.assertEqual(payouts.get_payout(self.db, 'PO-RECON-MISSING')['status'], 'PAID')
        self.assertEqual(payouts.get_payout(self.db, 'PO-RECON-STATUS')['status'], 'PAID')

    # FEATURE 08 — Scenario — no network calls
    def test_reconcile_makes_no_network_calls(self):
        def blocked(*args, **kwargs):
            raise AssertionError('reconciliation must not open a network connection')

        with mock.patch('socket.socket', side_effect=blocked), \
             mock.patch('socket.create_connection', side_effect=blocked), \
             mock.patch('urllib.request.urlopen', side_effect=blocked):
            reconciliation.reconcile(self.db, now=NOW)
        source = Path(reconciliation.__file__).read_text()
        self.assertNotIn('urllib', source)
        self.assertNotIn('requests', source)
        self.assertNotIn('httpx', source)
        self.assertNotIn('http.client', source)

    def _assert_six_cases(self, report, database):
        self.assertEqual(by_result(report), EXPECTED_RESULTS)
        self.assertEqual((report['run']['matched_count'], report['run']['mismatched_count'],
                          report['run']['unknown_count']),
                         EXPECTED_COUNTS)
        item_ids = {item['payout_id'] for item in report['items']}
        self.assertEqual(item_ids, set(EXPECTED_RESULTS))
        seed_ids = {payout['payout_id'] for payout in payouts.list_payouts(database)
                    if payout['payout_id'] not in reconciliation.RECON_PAYOUT_IDS}
        self.assertTrue(item_ids.isdisjoint(seed_ids))
        self.assertTrue(item_ids.isdisjoint(execution_replay.REPLAY_PAYOUT_IDS))
        for payout in payouts.list_payouts(database):
            if payout['payout_id'] not in item_ids:
                if payout['execution_status'] == payouts.NOT_STARTED:
                    continue
                if (payout['execution_status'] == payouts.SETTLED
                        and not payout['execution_ref']):
                    continue
                if payout['payout_id'] in execution_replay.REPLAY_PAYOUT_IDS:
                    continue
                self.fail(f'{payout["payout_id"]} should have been reconciled')
        self.assertFalse(any(item['our_status'] == payouts.NOT_STARTED
                             and item['result'] == 'missing_from_settlement'
                             for item in report['items']))
        ours = {payout['payout_id']: payout for payout in payouts.list_payouts(database)}
        for item in report['items']:
            payout = ours.get(item['payout_id'])
            if payout and payout['execution_status'] == payouts.SETTLED:
                self.assertTrue(payout['execution_ref'],
                                f'{item["payout_id"]} was compared with no execution_ref')

    # FEATURE 09 — Scenario — a real seeded demo produces the six cases and leaves the Desk alone
    def test_real_seeded_demo_produces_the_six_cases(self):
        payouts.ensure_seeded(self.db)
        self.assertEqual(len(payouts.list_payouts(self.db)), 40)
        self.assertTrue({payout['payout_id'] for payout in payouts.list_payouts(self.db)
                         }.isdisjoint(reconciliation.RECON_PAYOUT_IDS))
        payout_events.sync(self.db)
        before_stream = json.dumps(payout_events.events_after(self.db, 0), sort_keys=True)
        before_flags = seed_flags(self.db)
        before_queue = [payout['payout_id'] for payout in payouts.current_queue(self.db)]
        before_state = seed_state(self.db)
        not_started = [payout['payout_id'] for payout in payouts.list_payouts(self.db)
                       if payout['execution_status'] == payouts.NOT_STARTED]
        backfilled = [payout['payout_id'] for payout in payouts.list_payouts(self.db)
                      if payout['execution_status'] == payouts.SETTLED
                      and not payout['execution_ref']]
        self.assertTrue(not_started)
        self.assertTrue(backfilled)
        report = reconciliation.reconcile(self.db, now=NOW)
        self._assert_six_cases(report, self.db)
        item_ids = {item['payout_id'] for item in report['items']}
        self.assertTrue(item_ids.isdisjoint(not_started))
        self.assertTrue(item_ids.isdisjoint(backfilled))
        self.assertEqual(seed_flags(self.db), MAIN_SEED_FLAGS)
        self.assertEqual(seed_flags(self.db), before_flags)
        self.assertEqual([payout['payout_id'] for payout in payouts.current_queue(self.db)],
                         MAIN_QUEUE_IDS)
        self.assertEqual(before_queue, MAIN_QUEUE_IDS)
        sent = next(payout for payout in payouts.current_queue(self.db)
                    if payout['payout_id'] == 'PO-1040')
        self.assertEqual(desk.STATUS_WORDS[sent['status']], ('Sent', 's-sent'))
        self.assertEqual(desk.STATUS_WORDS['FLAGGED'], ('Needs review', 's-review'))
        self.assertEqual(seed_state(self.db), before_state)
        self.assertTrue(reconciliation.RECON_PAYOUT_IDS.issubset(
            {payout['payout_id'] for payout in payouts.list_payouts(self.db)}))
        self.assertTrue(reconciliation.RECON_PAYOUT_IDS.isdisjoint(
            {payout['payout_id'] for payout in payouts.current_queue(self.db)}))
        recon_events = [row for row in payouts.list_execution_events(self.db)
                        if row['payout_id'] == 'PO-RECON-UNKNOWN']
        self.assertEqual(len(recon_events), 1)
        self.assertEqual(
            [row for row in payouts.list_execution_events(self.db)
             if row['payout_id'] not in reconciliation.RECON_PAYOUT_IDS],
            [])
        added = payout_events.sync(self.db)
        self.assertEqual(added, [])
        self.assertEqual(json.dumps(payout_events.events_after(self.db, 0), sort_keys=True),
                         before_stream)

    # FEATURE 09b — Scenario — the same counts hold after a reset, and after a #56 replay
    def test_reset_and_replay_leave_the_same_six_cases(self):
        payouts.reset(self.db)
        report = reconciliation.reconcile(self.db, now=NOW)
        self._assert_six_cases(report, self.db)
        fresh = Path(self.temp.name) / 'after-replay.sqlite3'
        payouts.ensure_seeded(fresh)
        execution_replay.replay(fresh)
        after_replay = reconciliation.reconcile(fresh, now=NOW)
        self._assert_six_cases(after_replay, fresh)
        self.assertTrue(any(payout['payout_id'] in execution_replay.REPLAY_PAYOUT_IDS
                            and payout['execution_ref']
                            for payout in payouts.list_payouts(fresh)))

    # FEATURE 10 — Scenario — Reset demo restores the starting set and clears runs
    def test_reset_clears_reconciliation_rows(self):
        payouts.ensure_seeded(self.db)
        reconciliation.reconcile(self.db, now=NOW)
        self.assertTrue(reconciliation.list_runs(self.db))
        self.assertTrue(reconciliation.list_items(self.db))
        self.assertTrue(reconciliation.RECON_PAYOUT_IDS.issubset(
            {payout['payout_id'] for payout in payouts.list_payouts(self.db)}))
        payouts.reset(self.db)
        self.assertEqual(len(payouts.list_payouts(self.db)), 40)
        self.assertTrue({payout['payout_id'] for payout in payouts.list_payouts(self.db)
                         }.isdisjoint(reconciliation.RECON_PAYOUT_IDS))
        self.assertEqual(reconciliation.list_runs(self.db), [])
        self.assertEqual(reconciliation.list_items(self.db), [])
        self.assertEqual(payouts.list_execution_events(self.db), [])
        self.assertEqual(seed_flags(self.db), MAIN_SEED_FLAGS)
        self.assertEqual([payout['payout_id'] for payout in payouts.current_queue(self.db)],
                         MAIN_QUEUE_IDS)

    # FEATURE 10b — Scenario — the command line prepares targets on a real seeded demo
    def test_cli_on_a_seeded_demo_produces_the_six_cases(self):
        payouts.ensure_seeded(self.db)
        with mock.patch('sys.stdout', new_callable=io.StringIO) as out:
            code = reconciliation.main([str(self.db)])
        self.assertEqual(code, 0)
        report = json.loads(out.getvalue())
        self._assert_six_cases(report, self.db)


class HostedReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(self.temp.name, SITE)
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.extensions['close']()
        self.temp.cleanup()

    def open_desk(self, client=None):
        return (client or self.client).get('/desk', base_url=SITE)

    def reconcile(self, client=None, **headers):
        client = client or self.client
        extra = {'Origin': SITE, **headers}
        return client.post('/reconciliation', base_url=SITE, headers=extra)

    def database(self):
        self.open_desk()
        files = sorted(Path(self.temp.name).rglob('payments.sqlite3'))
        return files[0]

    # FEATURE 11 — Scenario — the JSON API returns the report; the Desk does not show it
    def test_json_api_returns_the_report_and_desk_is_unchanged(self):
        html_before = self.open_desk().get_data(as_text=True)
        self.assertIn('All <b>10</b>', html_before)
        refused = self.app.test_client().post('/reconciliation', base_url=SITE,
                                              headers={'Origin': SITE})
        self.assertEqual(refused.status_code, 409)
        reply = self.reconcile()
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.json['run']['fixture_name'], 'settlement_demo.csv')
        self.assertEqual((reply.json['run']['matched_count'],
                          reply.json['run']['mismatched_count'],
                          reply.json['run']['unknown_count']),
                         EXPECTED_COUNTS)
        self.assertEqual(by_result(reply.json), EXPECTED_RESULTS)
        html = self.open_desk().get_data(as_text=True)
        self.assertIn('All <b>10</b>', html)
        self.assertIn('Needs review', html)
        self.assertIn('Sent', html)
        self.assertNotIn('PO-RECON-', html)
        self.assertNotIn('settlement_demo', html)
        self.assertNotIn('status_mismatch', html)
        self.assertNotIn('Recon Match Payee', html)
        self.assertEqual(seed_flags(self.database()), MAIN_SEED_FLAGS)
        self.assertEqual([payout['payout_id'] for payout in payouts.current_queue(self.database())],
                         MAIN_QUEUE_IDS)
        foreign = self.reconcile(**{'Origin': 'https://different.example'})
        self.assertEqual(foreign.status_code, 403)

    # FEATURE 12 — Scenario — another visitor's demo is unchanged
    def test_only_this_visitors_demo_changes(self):
        other = self.app.test_client()
        self.open_desk()
        mine = self.database()
        self.open_desk(other)
        self.reconcile()
        files = sorted(Path(self.temp.name).rglob('payments.sqlite3'))
        self.assertEqual(len(files), 2)
        theirs = next(path for path in files if path != mine)
        self.assertTrue(reconciliation.list_runs(mine))
        self.assertEqual(reconciliation.list_runs(theirs), [])
        self.assertEqual(len(payouts.list_payouts(theirs)), 40)
        self.assertTrue({payout['payout_id'] for payout in payouts.list_payouts(theirs)
                         }.isdisjoint(reconciliation.RECON_PAYOUT_IDS))
        self.assertEqual(seed_flags(theirs), MAIN_SEED_FLAGS)


if __name__ == '__main__':
    unittest.main()
