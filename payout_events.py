# START HERE — Live payout change stream (issue #43).
# Records payout created and status-changed events for one visitor's demo and
# serves them as server-sent events from the existing Flask app. No new hosted
# service, env var or key. The Desk page does not subscribe yet, so nothing on
# screen changes. A later hook from payout_rules (#79) can reuse sync(); this
# file does not import that unmerged code.
"""Per-visitor payout created and status-changed events, served as SSE."""
import json
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

import payouts

# FEATURE 01 — Name the two event types and the short connection hold
# "created" is a payout that just appeared in this visitor's database.
# "status-changed" is a later status move on a payout already seen. The stream
# holds a Gunicorn thread, and this service runs one worker with four threads,
# so each connection closes after HOLD_SECONDS and EventSource reconnects
# with Last-Event-ID. Heartbeats keep proxies from buffering a quiet stream.
CREATED = 'created'
STATUS_CHANGED = 'status-changed'
HOLD_SECONDS = 20
POLL_SECONDS = 0.5
HEARTBEAT_SECONDS = 3
STREAM_FILE = 'payout_stream.json'

_locks = {}
_locks_guard = threading.Lock()


def _lock(database):
    key = str(Path(database).resolve())
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _path(database):
    return Path(database).with_name(STREAM_FILE)


# FEATURE 02 — Load and save this visitor's event log
# The log sits next to the demo SQLite file, not in a new table, so this is not
# a schema change. Event ids only increase. A missing file starts at id 1.
def _load(path):
    if not path.is_file():
        return {'next_id': 1, 'statuses': {}, 'events': []}
    return json.loads(path.read_text())


def _save(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(state, separators=(',', ':')))
    tmp.replace(path)


def parse_last_id(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number >= 0 else 0


def _payload(event):
    return {key: value for key, value in event.items() if key not in ('id', 'type')}


# FEATURE 03 — Record new payouts and status moves for this visitor only
# Compares the current payouts table to the last statuses we stored. A new
# payout_id becomes a created event; a later status change becomes
# status-changed. Works on the one database file it is given, so workspaces
# stay isolated. Calling it twice with no change adds nothing.
def sync(database):
    with _lock(database):
        path = _path(database)
        state = _load(path)
        seen = state['statuses']
        with closing(sqlite3.connect(str(database))) as db:
            raw = db.execute(
                f'SELECT {", ".join(payouts.COLUMNS)} FROM payouts').fetchall()
        rows = sorted((payouts._public(row) for row in raw),
                      key=lambda row: (row['created_at'], row['payout_id']))
        present = {row['payout_id'] for row in rows}
        added = []
        for payout in rows:
            payout_id = payout['payout_id']
            if payout_id not in seen:
                event = dict(id=state['next_id'], type=CREATED, payout_id=payout_id,
                             status=payout['status'], payee_name=payout['payee_name'],
                             amount=payout['amount'], currency=payout['currency'],
                             rail=payout['rail'], at=payout['created_at'])
                state['next_id'] += 1
                state['events'].append(event)
                seen[payout_id] = payout['status']
                added.append(event)
            elif seen[payout_id] != payout['status']:
                event = dict(id=state['next_id'], type=STATUS_CHANGED, payout_id=payout_id,
                             from_status=seen[payout_id], to_status=payout['status'],
                             payee_name=payout['payee_name'], amount=payout['amount'],
                             currency=payout['currency'], rail=payout['rail'],
                             at=payout['updated_at'])
                state['next_id'] += 1
                state['events'].append(event)
                seen[payout_id] = payout['status']
                added.append(event)
        for payout_id in [payout_id for payout_id in seen if payout_id not in present]:
            del seen[payout_id]
        _save(path, state)
        return added


# FEATURE 04 — After Reset demo, treat the restored seed as new created events
# reset() deletes and reseeds the same payout ids. Forgetting the remembered
# statuses makes the next sync emit created events for the starting set, with
# ids that keep counting up so a reconnect can still resume.
def after_reset(database):
    with _lock(database):
        path = _path(database)
        state = _load(path)
        state['statuses'] = {}
        _save(path, state)
    return sync(database)


# FEATURE 05 — Events after a given id, after the log is synced
# Reconnect uses this with the Last-Event-ID the browser already saw.
def events_after(database, last_id=0):
    sync(database)
    with _lock(database):
        return [event for event in _load(_path(database))['events'] if event['id'] > last_id]


# FEATURE 06 — Format one event as a server-sent events chunk
# The event name is the type (created or status-changed). Data is JSON with
# only synthetic payout fields. The id line is what Last-Event-ID resumes from.
def format_sse(event):
    body = json.dumps(dict(_payload(event), type=event['type']), separators=(',', ':'))
    return f"id: {event['id']}\nevent: {event['type']}\ndata: {body}\n\n"


# FEATURE 07 — Yield SSE chunks, then close so a Gunicorn thread is released
# First any events after last_id, then poll until HOLD_SECONDS. Heartbeats are
# comment lines. Tests patch HOLD_SECONDS to 0 for a one-shot response.
def stream(database, last_id=0, hold_seconds=None, poll_seconds=None, clock=time.monotonic,
           sleep=time.sleep):
    hold = HOLD_SECONDS if hold_seconds is None else hold_seconds
    poll = POLL_SECONDS if poll_seconds is None else poll_seconds
    deadline = clock() + hold
    last_beat = clock()
    for event in events_after(database, last_id):
        last_id = event['id']
        yield format_sse(event)
    while clock() < deadline:
        sleep(min(poll, max(0, deadline - clock())))
        for event in events_after(database, last_id):
            last_id = event['id']
            last_beat = clock()
            yield format_sse(event)
        if clock() >= deadline:
            break
        if clock() - last_beat >= HEARTBEAT_SECONDS:
            last_beat = clock()
            yield ': keepalive\n\n'
