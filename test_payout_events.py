# Automated checks for payout_events.py: the per-visitor live update stream of
# payout created and status-changed events (issue #43).
"""Exercise event order, workspace scoping and reconnect with temporary fictional demos."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import payout_events
import payouts
from hosted_app import create_app


def parse_sse(body):
    """Turn an SSE body into a list of (id, event, data-dict) tuples, skipping comments."""
    items = []
    event_id = name = None
    data_lines = []
    for line in body.splitlines():
        if line.startswith(':'):
            continue
        if line == '':
            if event_id is not None and name is not None:
                items.append((int(event_id), name, json.loads('\n'.join(data_lines) or '{}')))
            event_id = name = None
            data_lines = []
            continue
        field, _, value = line.partition(':')
        value = value[1:] if value.startswith(' ') else value
        if field == 'id':
            event_id = value
        elif field == 'event':
            name = value
        elif field == 'data':
            data_lines.append(value)
    if event_id is not None and name is not None:
        items.append((int(event_id), name, json.loads('\n'.join(data_lines) or '{}')))
    return items


class StreamBase(unittest.TestCase):
    # FEATURE 01 — Test support — setUp
    # Isolated temporary demos and a one-shot stream (HOLD_SECONDS=0) so tests do
    # not hold a thread.
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hold = mock.patch.object(payout_events, 'HOLD_SECONDS', 0)
        self.hold.start()
        self.app = create_app(self.temp.name, 'https://demo.example')
        self.client = self.app.test_client()

    # FEATURE 02 — Test support — tearDown
    def tearDown(self):
        self.hold.stop()
        self.app.extensions['close']()
        self.temp.cleanup()

    def get(self, path, client=None, **headers):
        return (client or self.client).get(path, base_url='https://demo.example', headers=headers)

    def start(self, client=None):
        return self.get('/dashboard', client)

    def stream(self, client=None, last_id=None, **headers):
        if last_id is not None:
            headers['Last-Event-ID'] = str(last_id)
        return self.get('/payouts/stream', client, **headers)

    def decide(self, payout_id, action, key, reviewer='Test Reviewer', client=None):
        return (client or self.client).post(
            f'/payouts/{payout_id}/{action}', json={'reviewer': reviewer},
            base_url='https://demo.example',
            headers={'Idempotency-Key': key, 'Origin': 'https://demo.example'})

    def reset(self, client=None):
        return (client or self.client).post(
            '/payouts/reset', base_url='https://demo.example',
            headers={'Origin': 'https://demo.example'})


class StreamTests(StreamBase):
    # FEATURE 03 — Scenario — the first visit records every seeded payout as created, in order
    def test_seed_emits_created_events_in_order(self):
        self.start()
        database = Path(self.temp.name).rglob('payments.sqlite3')
        database = next(database)
        events = payout_events.events_after(database, 0)
        rows = sorted(payouts.list_payouts(database),
                      key=lambda row: (row['created_at'], row['payout_id']))
        self.assertEqual(len(events), 40)
        self.assertEqual([event['id'] for event in events], list(range(1, 41)))
        self.assertTrue(all(event['type'] == 'created' for event in events))
        self.assertEqual([event['payout_id'] for event in events],
                         [row['payout_id'] for row in rows])
        for event, row in zip(events, rows):
            self.assertEqual(event['status'], row['status'])
            self.assertEqual(event['payee_name'], row['payee_name'])
            self.assertEqual(event['amount'], row['amount'])
            self.assertEqual(event['at'], row['created_at'])
        self.assertEqual(payout_events.sync(database), [])

    # FEATURE 04 — Scenario — a status change is recorded after that payout's created event
    def test_status_change_follows_created_for_that_payout(self):
        self.start()
        decision = self.decide('PO-1042', 'hold', 'hold-1042')
        self.assertEqual(decision.status_code, 201)
        body = parse_sse(self.stream().get_data(as_text=True))
        created = [item for item in body if item[1] == 'created' and item[2]['payout_id'] == 'PO-1042']
        changed = [item for item in body if item[1] == 'status-changed' and item[2]['payout_id'] == 'PO-1042']
        self.assertEqual(len(created), 1)
        self.assertEqual(len(changed), 1)
        self.assertLess(created[0][0], changed[0][0])
        self.assertEqual(changed[0][2]['from_status'], 'FLAGGED')
        self.assertEqual(changed[0][2]['to_status'], 'HELD')
        ids = [item[0] for item in body]
        self.assertEqual(ids, sorted(ids))

    # FEATURE 05 — Scenario — events stay inside one visitor workspace
    def test_events_are_scoped_to_one_workspace(self):
        self.start()
        self.decide('PO-1042', 'hold', 'hold-a')
        other = self.app.test_client()
        self.start(other)
        mine = parse_sse(self.stream().get_data(as_text=True))
        theirs = parse_sse(self.stream(other).get_data(as_text=True))
        self.assertTrue(any(item[1] == 'status-changed' and item[2]['payout_id'] == 'PO-1042'
                            for item in mine))
        self.assertFalse(any(item[1] == 'status-changed' for item in theirs))
        self.assertTrue(all(item[1] == 'created' for item in theirs))
        self.assertEqual(sum(1 for item in theirs if item[1] == 'created'), 40)

    # FEATURE 06 — Scenario — a reconnect resumes after the last event id
    def test_reconnect_resumes_from_last_event_id(self):
        self.start()
        first = parse_sse(self.stream().get_data(as_text=True))
        self.assertEqual(len(first), 40)
        last_id = first[-1][0]
        self.assertEqual(parse_sse(self.stream(last_id=last_id).get_data(as_text=True)), [])
        self.decide('PO-1043', 'return', 'return-1043')
        replay = parse_sse(self.stream(last_id=last_id).get_data(as_text=True))
        self.assertEqual(len(replay), 1)
        self.assertEqual(replay[0][0], last_id + 1)
        self.assertEqual(replay[0][1], 'status-changed')
        self.assertEqual(replay[0][2]['payout_id'], 'PO-1043')
        self.assertEqual(replay[0][2]['from_status'], 'FLAGGED')
        self.assertEqual(replay[0][2]['to_status'], 'RETURNED')
        by_query = parse_sse(self.get('/payouts/stream?lastEventId=%s' % last_id).get_data(as_text=True))
        self.assertEqual(by_query, replay)

    # FEATURE 07 — Scenario — a new payout (without #79) is created, then a status change
    def test_new_payout_is_created_then_status_changed(self):
        self.start()
        first = parse_sse(self.stream().get_data(as_text=True))
        last_id = first[-1][0]
        database = next(Path(self.temp.name).rglob('payments.sqlite3'))
        at = '2026-11-10T18:00:00+00:00'
        with closing(sqlite3.connect(str(database))) as db, db:
            db.execute(
                "INSERT INTO payouts (payout_id, payee_name, payee_account, account_name, "
                "amount_cents, currency, rail, status, created_at, updated_at) "
                "VALUES ('PO-TEST-NEW', 'Cedar Cleaning Co.', 'DEMO-CEDR-004', "
                "'Cedar Cleaning Co.', 64000, 'USD', 'ACH', 'PENDING', ?, ?)", (at, at))
        created_only = payout_events.sync(database)
        self.assertEqual(len(created_only), 1)
        self.assertEqual(created_only[0]['type'], 'created')
        self.assertEqual(created_only[0]['payout_id'], 'PO-TEST-NEW')
        self.assertEqual(created_only[0]['status'], 'PENDING')
        with closing(sqlite3.connect(str(database))) as db, db:
            db.execute("UPDATE payouts SET status = 'FLAGGED', updated_at = ? WHERE payout_id = 'PO-TEST-NEW'",
                       (at,))
        changed = payout_events.sync(database)
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]['type'], 'status-changed')
        self.assertEqual(changed[0]['from_status'], 'PENDING')
        self.assertEqual(changed[0]['to_status'], 'FLAGGED')
        self.assertLess(created_only[0]['id'], changed[0]['id'])
        replay = parse_sse(self.stream(last_id=last_id).get_data(as_text=True))
        self.assertEqual([(item[1], item[2]['payout_id']) for item in replay],
                         [('created', 'PO-TEST-NEW'), ('status-changed', 'PO-TEST-NEW')])

    # FEATURE 08 — Scenario — Reset demo appends a fresh created set; reconnect sees them
    def test_reset_appends_created_events(self):
        self.start()
        self.decide('PO-1042', 'hold', 'hold-reset')
        before = parse_sse(self.stream().get_data(as_text=True))
        last_id = before[-1][0]
        self.assertEqual(self.reset().status_code, 200)
        after = parse_sse(self.stream(last_id=last_id).get_data(as_text=True))
        self.assertEqual(len(after), 40)
        self.assertTrue(all(item[1] == 'created' for item in after))
        self.assertEqual(after[0][0], last_id + 1)
        held = [item for item in after if item[2]['payout_id'] == 'PO-1042']
        self.assertEqual(held[0][2]['status'], 'FLAGGED')

    # FEATURE 09 — Scenario — the stream is SSE, scoped, and the Desk does not subscribe
    def test_stream_headers_and_desk_does_not_subscribe(self):
        self.start()
        response = self.stream()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content_type.startswith('text/event-stream'))
        self.assertEqual(response.headers.get('X-Accel-Buffering'), 'no')
        html = self.get('/desk').get_data(as_text=True)
        script = self.get('/desk.js').get_data(as_text=True)
        for source in (html, script):
            self.assertNotIn('/payouts/stream', source)
            self.assertNotIn('EventSource', source)
            self.assertNotIn('text/event-stream', source)
        self.assertEqual(payout_events.parse_last_id('12'), 12)
        self.assertEqual(payout_events.parse_last_id('nope'), 0)
        self.assertEqual(payout_events.parse_last_id('-3'), 0)
        self.assertEqual(payout_events.parse_last_id(None), 0)


if __name__ == '__main__':
    unittest.main()
