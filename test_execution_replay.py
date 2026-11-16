# Automated checks for the bundled synthetic payment-event replay (#56).
# Temporary fictional databases only. No network, no real money, bank or provider.
"""Exercise one manual replay of fixtures/execution_events_demo.json."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import execution_replay
import payout_events
import payout_rules
import payouts


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


def seed_flags(database):
    return {payout['payout_id']: (payout['flag_code'], payout['flag_text'])
            for payout in payouts.list_payouts(database) if payout['flag_code']
            and payout['payout_id'] not in execution_replay.REPLAY_PAYOUT_IDS}


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'payouts.sqlite3'
        payouts.ensure_seeded(self.db)

    def tearDown(self):
        self.temp.cleanup()

    # FEATURE 01 — Scenario — one manual run applies the bundled events in order
    def test_one_run_applies_bundled_events_in_order(self):
        report = execution_replay.replay(self.db)
        outcomes = [(item['event_id'], item['outcome']) for item in report['results']]
        self.assertEqual(outcomes, [
            ('evt-replay-settle-submitted', 'applied'),
            ('evt-replay-settle-submitted', 'skipped'),
            ('evt-replay-settle-settled', 'applied'),
            ('evt-replay-fail-submitted', 'applied'),
            ('evt-replay-fail-failed', 'applied'),
            ('evt-replay-unknown-submitted', 'applied'),
            ('evt-replay-unknown-unknown', 'applied'),
            ('evt-replay-unknown-retry', 'refused'),
            ('evt-replay-unapproved-submitted', 'refused'),
            ('evt-replay-order-settled', 'refused'),
        ])
        self.assertEqual((report['applied'], report['skipped'], report['refused'], report['noop']),
                         (6, 1, 3, 0))
        settle = payouts.get_payout(self.db, 'PO-REPLAY-SETTLE')
        self.assertEqual((settle['execution_status'], settle['execution_ref'], settle['status'],
                          settle['review_status']),
                         ('SETTLED', 'replay:settle:settled', 'APPROVED', 'APPROVED'))
        self.assertEqual(payouts.get_payout(self.db, 'PO-REPLAY-FAIL')['execution_status'], 'FAILED')
        self.assertEqual(payouts.get_payout(self.db, 'PO-REPLAY-UNKNOWN')['execution_status'], 'UNKNOWN')
        self.assertEqual(payouts.get_payout(self.db, 'PO-REPLAY-ORDER')['execution_status'], 'NOT_STARTED')
        self.assertEqual(payouts.get_payout(self.db, 'PO-REPLAY-UNAPPROVED')['execution_status'],
                         'NOT_STARTED')
        events = payouts.list_execution_events(self.db)
        self.assertEqual([event['event_id'] for event in events], [
            'evt-replay-settle-submitted', 'evt-replay-settle-settled',
            'evt-replay-fail-submitted', 'evt-replay-fail-failed',
            'evt-replay-unknown-submitted', 'evt-replay-unknown-unknown',
        ])
        self.assertTrue(all(event['source'] == 'replay' for event in events))
        self.assertEqual(len({event['event_id'] for event in events}), len(events))

    # FEATURE 02 — Scenario — running it twice changes nothing (matched by event id)
    def test_second_run_changes_nothing(self):
        first = execution_replay.replay(self.db)
        before = json.dumps(payouts.list_execution_events(self.db), sort_keys=True)
        payouts_before = json.dumps(
            [payouts.get_payout(self.db, payout_id) for payout_id in sorted(execution_replay.REPLAY_PAYOUT_IDS)],
            sort_keys=True)
        second = execution_replay.replay(self.db)
        self.assertEqual(second['applied'], 0)
        self.assertEqual(second['skipped'], first['applied'] + first['skipped'])
        self.assertEqual(json.dumps(payouts.list_execution_events(self.db), sort_keys=True), before)
        self.assertEqual(json.dumps(
            [payouts.get_payout(self.db, payout_id) for payout_id in sorted(execution_replay.REPLAY_PAYOUT_IDS)],
            sort_keys=True), payouts_before)

    # FEATURE 03 — Scenario — an event on a payout that is not Approved is refused and logged
    def test_unapproved_event_is_refused_and_logged(self):
        with self.assertLogs('execution_replay', level='INFO') as logs:
            report = execution_replay.replay(self.db)
        refused = next(item for item in report['results']
                       if item['event_id'] == 'evt-replay-unapproved-submitted')
        self.assertEqual(refused['outcome'], 'refused')
        self.assertIn('not Approved', refused['reason'])
        self.assertTrue(any('evt-replay-unapproved-submitted' in line and 'not Approved' in line
                            for line in logs.output))
        self.assertEqual(payouts.get_payout(self.db, 'PO-REPLAY-UNAPPROVED')['execution_status'],
                         'NOT_STARTED')
        self.assertFalse(any(event['payout_id'] == 'PO-REPLAY-UNAPPROVED'
                             for event in payouts.list_execution_events(self.db)))

    # FEATURE 04 — Scenario — UNKNOWN stays UNKNOWN; no retry is sent
    def test_unknown_is_not_retried(self):
        report = execution_replay.replay(self.db)
        retry = next(item for item in report['results'] if item['event_id'] == 'evt-replay-unknown-retry')
        self.assertEqual(retry['outcome'], 'refused')
        self.assertIn('cannot be submitted or retried automatically', retry['reason'])
        self.assertEqual(payouts.get_payout(self.db, 'PO-REPLAY-UNKNOWN')['execution_status'], 'UNKNOWN')
        self.assertFalse(any(event['event_id'] == 'evt-replay-unknown-retry'
                             for event in payouts.list_execution_events(self.db)))

    # FEATURE 05 — Scenario — out-of-order settle is refused; a later submit is applied
    def test_out_of_order_event_is_refused(self):
        report = execution_replay.replay(self.db)
        skipped_settle = next(item for item in report['results']
                              if item['event_id'] == 'evt-replay-order-settled')
        self.assertEqual(skipped_settle['outcome'], 'refused')
        self.assertIn('cannot move to SETTLED', skipped_settle['reason'])
        self.assertEqual(payouts.get_payout(self.db, 'PO-REPLAY-ORDER')['execution_status'],
                         'NOT_STARTED')
        path = Path(self.temp.name) / 'later-submit.json'
        path.write_text(json.dumps([{
            'event_id': 'evt-replay-order-submitted',
            'payout_id': 'PO-REPLAY-ORDER',
            'execution_status': 'SUBMITTED',
            'execution_ref': 'replay:order:submitted',
            'at': '2026-11-11T18:09:00+00:00',
        }]))
        later = execution_replay.replay(self.db, path)
        self.assertEqual(later['results'][0]['outcome'], 'applied')
        self.assertEqual(payouts.get_payout(self.db, 'PO-REPLAY-ORDER')['execution_status'],
                         'SUBMITTED')

    # FEATURE 06 — Scenario — no network calls
    def test_replay_makes_no_network_calls(self):
        def blocked(*args, **kwargs):
            raise AssertionError('replay must not open a network connection')

        with mock.patch('socket.socket', side_effect=blocked), \
             mock.patch('socket.create_connection', side_effect=blocked), \
             mock.patch('urllib.request.urlopen', side_effect=blocked):
            execution_replay.replay(self.db)
        source = Path(execution_replay.__file__).read_text()
        self.assertNotIn('urllib', source)
        self.assertNotIn('requests', source)
        self.assertNotIn('httpx', source)
        self.assertNotIn('http.client', source)

    # FEATURE 07 — Scenario — seed flags, queue, Sent words and stream stay as they are
    def test_desk_flags_queue_words_and_stream_unchanged(self):
        payout_events.sync(self.db)
        before_stream = json.dumps(payout_events.events_after(self.db, 0), sort_keys=True)
        before_queue = [payout['payout_id'] for payout in payouts.current_queue(self.db)]
        execution_replay.replay(self.db)
        self.assertEqual(seed_flags(self.db), MAIN_SEED_FLAGS)
        self.assertEqual([payout['payout_id'] for payout in payouts.current_queue(self.db)],
                         MAIN_QUEUE_IDS)
        self.assertEqual(before_queue, MAIN_QUEUE_IDS)
        self.assertTrue(execution_replay.REPLAY_PAYOUT_IDS.isdisjoint(
            payout['payout_id'] for payout in payouts.current_queue(self.db)))
        sent = next(payout for payout in payouts.current_queue(self.db)
                    if payout['payout_id'] == 'PO-1040')
        self.assertEqual(sent['status'], 'PAID')  # The Desk's 'Sent' wording is checked in step 4.
        added = payout_events.sync(self.db)
        self.assertEqual(added, [])
        self.assertEqual(json.dumps(payout_events.events_after(self.db, 0), sort_keys=True),
                         before_stream)
        later = payout_rules.create(
            self.db, 'PO-SIM-CEDAR-AFTER', 'Cedar Cleaning Co.', 'DEMO-CEDR-004', 200000, 'ACH',
            now=datetime_utc(2026, 11, 10, 19))
        self.assertEqual(payouts.get_payout(self.db, 'PO-SIM-CEDAR-AFTER')['flag_code'],
                         'AMOUNT_OVER_3X_AVG')
        self.assertEqual(payouts.get_payout(self.db, 'PO-SIM-CEDAR-AFTER')['flag_text'],
                         'Amount is 3× this payee’s usual payout ($640 avg)')
        self.assertEqual(later['status'], 'FLAGGED')

    # FEATURE 08 — Scenario — Reset demo restores the starting set and clears replay rows
    def test_reset_clears_replay_payouts_and_events(self):
        execution_replay.replay(self.db)
        self.assertTrue(any(payout['payout_id'] in execution_replay.REPLAY_PAYOUT_IDS
                            for payout in payouts.list_payouts(self.db)))
        self.assertTrue(payouts.list_execution_events(self.db))
        payouts.reset(self.db)
        ids = {payout['payout_id'] for payout in payouts.list_payouts(self.db)}
        self.assertEqual(len(ids), 40)
        self.assertTrue(ids.isdisjoint(execution_replay.REPLAY_PAYOUT_IDS))
        self.assertEqual(payouts.list_execution_events(self.db), [])
        self.assertEqual(payouts.list_events(self.db), [])
        self.assertEqual(seed_flags(self.db), MAIN_SEED_FLAGS)
        self.assertEqual([payout['payout_id'] for payout in payouts.current_queue(self.db)],
                         MAIN_QUEUE_IDS)

    # FEATURE 09 — Scenario — a repeat UNKNOWN event is a no-op and is not a retry
    def test_repeat_unknown_is_a_noop(self):
        execution_replay.replay(self.db)
        extra = [{
            'event_id': 'evt-replay-unknown-again',
            'payout_id': 'PO-REPLAY-UNKNOWN',
            'execution_status': 'UNKNOWN',
            'execution_ref': 'replay:unknown:again',
            'at': '2026-11-11T18:10:00+00:00',
        }]
        path = Path(self.temp.name) / 'unknown-again.json'
        path.write_text(json.dumps(extra))
        report = execution_replay.replay(self.db, path)
        self.assertEqual(report['results'][0]['outcome'], 'noop')
        self.assertEqual(report['noop'], 1)
        self.assertEqual(payouts.get_payout(self.db, 'PO-REPLAY-UNKNOWN')['execution_status'],
                         'UNKNOWN')
        self.assertFalse(any(event['event_id'] == 'evt-replay-unknown-again'
                             for event in payouts.list_execution_events(self.db)))


def datetime_utc(year, month, day, hour):
    from datetime import datetime, timezone
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


if __name__ == '__main__':
    unittest.main()
