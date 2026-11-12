# Replay a bundled file of synthetic payment-outcome events into one visitor demo (#56).
# Each event is fictional. There are no network calls and no real money, bank or provider.
"""Apply fixtures/execution_events_demo.json to one demo database, in order."""
import json
import logging
import storage
import sys
from datetime import datetime, timezone
from pathlib import Path

from payments import APIError
import payout_notes
import payouts


logger = logging.getLogger('execution_replay')

FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'execution_events_demo.json'
AT = '2026-11-11T17:00:00+00:00'

# Synthetic targets used only by this replay. Unique payee names, so seed flags
# and usual amounts stay as they are. current_queue and the live stream hide
# these ids, so the Desk does not change.
_APPROVED = dict(status=payouts.APPROVED, review_status=payouts.APPROVED,
                 execution_status=payouts.NOT_STARTED)
_UNAPPROVED = dict(status=payouts.FLAGGED, review_status=payouts.FLAGGED,
                   execution_status=payouts.NOT_STARTED, flag_code='NEW_PAYEE_OVER_1000',
                   flag_text='No paid payout to this payee yet; amount is over $1,000')


def _target(payout_id, payee, account, cents, extra):
    row = payouts._row(payout_id, payee, account, cents, 'ACH', extra['status'], AT)
    row.update(extra)
    return row


REPLAY_TARGETS = {
    'PO-REPLAY-SETTLE': _target(
        'PO-REPLAY-SETTLE', 'Replay Settle Payee', 'DEMO-RPLY-001', 25000, _APPROVED),
    'PO-REPLAY-FAIL': _target(
        'PO-REPLAY-FAIL', 'Replay Fail Payee', 'DEMO-RPLY-002', 26000, _APPROVED),
    'PO-REPLAY-UNKNOWN': _target(
        'PO-REPLAY-UNKNOWN', 'Replay Unknown Payee', 'DEMO-RPLY-003', 27000, _APPROVED),
    'PO-REPLAY-ORDER': _target(
        'PO-REPLAY-ORDER', 'Replay Order Payee', 'DEMO-RPLY-004', 28000, _APPROVED),
    'PO-REPLAY-UNAPPROVED': _target(
        'PO-REPLAY-UNAPPROVED', 'Replay Unapproved Payee', 'DEMO-RPLY-005', 100001, _UNAPPROVED),
}
REPLAY_PAYOUT_IDS = frozenset(REPLAY_TARGETS)


def load_fixture(path=None):
    raw = json.loads(Path(path or FIXTURE).read_text())
    if not isinstance(raw, list):
        raise APIError(422, 'The replay fixture must be a JSON list of events.')
    events = []
    for item in raw:
        if not isinstance(item, dict) or not item.get('event_id') or not item.get('payout_id'):
            raise APIError(422, 'Each replay event needs an event_id and a payout_id.')
        status = item.get('execution_status')
        if status not in payouts.EXECUTION_STATUSES:
            raise APIError(422, 'Unknown payment outcome.')
        events.append(dict(
            event_id=item['event_id'], payout_id=item['payout_id'],
            execution_status=status, execution_ref=item.get('execution_ref'),
            at=item.get('at') or datetime.now(timezone.utc).isoformat()))
    return events


def _prepare(db):
    existing = {row[0] for row in db.execute('SELECT payout_id FROM payouts')}
    added = 0
    for payout_id, row in REPLAY_TARGETS.items():
        if payout_id in existing:
            continue
        row = payout_notes.apply(dict(row))
        db.execute(
            f'INSERT INTO payouts ({", ".join(payouts.COLUMNS)}) VALUES '
            f'({", ".join("?" * len(payouts.COLUMNS))})',
            tuple(row[c] for c in payouts.COLUMNS))
        added += 1
    return added


def _already(db, event_id):
    return db.execute('SELECT 1 FROM execution_events WHERE event_id = ?', (event_id,)).fetchone()


def _apply(db, event):
    event_id, payout_id = event['event_id'], event['payout_id']
    if _already(db, event_id):
        return dict(event_id=event_id, payout_id=payout_id, outcome='skipped',
                    reason='This event was already applied.')
    row = db.execute(
        f'SELECT {", ".join(payouts.COLUMNS)} FROM payouts WHERE payout_id = ?',
        (payout_id,)).fetchone()
    if not row:
        reason = 'Payout not found.'
        logger.info('Replay event %s refused: %s', event_id, reason)
        return dict(event_id=event_id, payout_id=payout_id, outcome='refused', reason=reason)
    payout = dict(zip(payouts.COLUMNS, row))
    if payout['review_status'] != payouts.APPROVED:
        reason = 'A payment-outcome event on a payout that is not Approved is refused.'
        logger.info('Replay event %s for %s refused: %s', event_id, payout_id, reason)
        return dict(event_id=event_id, payout_id=payout_id, outcome='refused', reason=reason)
    current, target = payout['execution_status'], event['execution_status']
    if current == payouts.UNKNOWN and target == payouts.UNKNOWN:
        return dict(event_id=event_id, payout_id=payout_id, outcome='noop',
                    reason='A repeat UNKNOWN event is a no-op and is not a retry.')
    try:
        payouts.check_execution_transition(current, target)
    except APIError as error:
        logger.info('Replay event %s for %s refused: %s', event_id, payout_id, error.message)
        return dict(event_id=event_id, payout_id=payout_id, outcome='refused', reason=error.message)
    ref = event.get('execution_ref') or f'replay:{event_id}'
    at = event['at']
    db.execute(
        'UPDATE payouts SET execution_status = ?, execution_ref = ?, execution_updated_at = ? '
        'WHERE payout_id = ?',
        (target, ref, at, payout_id))
    db.execute(
        'INSERT INTO execution_events (event_id, payout_id, execution_status, execution_ref, at, source) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (event_id, payout_id, target, ref, at, payouts.REPLAY))
    return dict(event_id=event_id, payout_id=payout_id, outcome='applied',
                execution_status=target, execution_ref=ref, at=at)


def replay(database, fixture=None):
    """Prepare the synthetic targets if needed, then apply the bundled events in order.

    A second run changes nothing: events already stored under the same event_id are
    skipped. Refused events are logged and not applied. status is not rewritten, so
    the Desk queue, stream and Sent word stay as they are.
    """
    events = load_fixture(fixture)
    payouts.ensure_seeded(database)
    with storage.connect(str(database), timeout=10) as db:
        db.execute('PRAGMA foreign_keys = ON')
        db.execute('BEGIN IMMEDIATE')
        _prepare(db)
        results = [_apply(db, event) for event in events]
    return dict(
        results=results,
        applied=sum(1 for item in results if item['outcome'] == 'applied'),
        skipped=sum(1 for item in results if item['outcome'] == 'skipped'),
        refused=sum(1 for item in results if item['outcome'] == 'refused'),
        noop=sum(1 for item in results if item['outcome'] == 'noop'),
    )


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ('-h', '--help'):
        print('Usage: python -m execution_replay DATABASE [FIXTURE]', file=sys.stderr)
        return 2
    report = replay(args[0], args[1] if len(args) > 1 else None)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
