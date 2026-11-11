# Compare our payment outcomes with a separately written synthetic settlement file (#57).
# The settlement file is hand-written. It is not generated from the #56 event file.
# There are no network calls and no real money, bank or provider.
"""Run one reconciliation of fixtures/settlement_demo.csv against one demo database."""
import csv
import io
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from payments import APIError
import payouts


logger = logging.getLogger('reconciliation')

FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'settlement_demo.csv'

MATCHED = 'matched'
STATUS_MISMATCH = 'status_mismatch'
AMOUNT_MISMATCH = 'amount_mismatch'
MISSING_FROM_SETTLEMENT = 'missing_from_settlement'
UNEXPECTED_SETTLEMENT = 'unexpected_settlement'
RESOLVED = 'resolved'
MISMATCH_RESULTS = frozenset({STATUS_MISMATCH, AMOUNT_MISMATCH,
                              MISSING_FROM_SETTLEMENT, UNEXPECTED_SETTLEMENT})
RUN_COLUMNS = ('run_id', 'started_at', 'ended_at', 'fixture_name',
               'matched_count', 'mismatched_count', 'unknown_count')
ITEM_COLUMNS = ('item_id', 'run_id', 'payout_id', 'our_status', 'settlement_status',
                'result', 'reason')


def load_settlement(path=None):
    """Load the hand-written settlement CSV. Comment lines starting with # are ignored."""
    raw = Path(path or FIXTURE).read_text()
    lines = [line for line in raw.splitlines() if line.strip() and not line.lstrip().startswith('#')]
    rows = list(csv.DictReader(io.StringIO('\n'.join(lines))))
    if not rows:
        raise APIError(422, 'The settlement file has no rows.')
    settlement = {}
    for row in rows:
        payout_id = (row.get('payout_id') or '').strip()
        status = (row.get('execution_status') or '').strip()
        if not payout_id or status not in payouts.EXECUTION_STATUSES:
            raise APIError(422, 'Each settlement line needs a payout_id and a payment outcome.')
        try:
            amount_cents = int(row.get('amount_cents'))
        except (TypeError, ValueError):
            raise APIError(422, f'Settlement amount for {payout_id} is not a whole number of cents.') from None
        if payout_id in settlement:
            raise APIError(422, f'The settlement file has two lines for {payout_id}.')
        settlement[payout_id] = dict(
            payout_id=payout_id, execution_status=status, amount_cents=amount_cents,
            settlement_ref=(row.get('settlement_ref') or '').strip() or None,
            at=(row.get('at') or '').strip() or None)
    return settlement


def _open(database):
    db = sqlite3.connect(str(database), timeout=10)
    db.execute('PRAGMA foreign_keys = ON')
    for statement in payouts.SCHEMA:
        db.execute(statement)
    payouts._upgrade(db)
    return db


def _load_ours(db):
    rows = db.execute(f'SELECT {", ".join(payouts.COLUMNS)} FROM payouts').fetchall()
    return {row[0]: dict(zip(payouts.COLUMNS, row)) for row in rows}


def _item(run_id, payout_id, our_status, settlement_status, result, reason):
    return dict(run_id=run_id, payout_id=payout_id, our_status=our_status,
                settlement_status=settlement_status, result=result, reason=reason)


def _resolve(db, payout, settlement, run_id, at):
    """Move UNKNOWN to SETTLED or FAILED only when settlement confirms it, and log the event."""
    current = payout['execution_status']
    target = settlement['execution_status']
    payouts.check_execution_transition(current, target)
    ref = settlement.get('settlement_ref') or f'recon:{payout["payout_id"]}'
    when = settlement.get('at') or at
    db.execute(
        'UPDATE payouts SET execution_status = ?, execution_ref = ?, execution_updated_at = ? '
        'WHERE payout_id = ?',
        (target, ref, when, payout['payout_id']))
    event_id = f'recon:{run_id}:{payout["payout_id"]}'
    db.execute(
        'INSERT INTO execution_events (event_id, payout_id, execution_status, execution_ref, at, source) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (event_id, payout['payout_id'], target, ref, when, payouts.RECONCILIATION))
    logger.info('Reconciliation run %s resolved %s from UNKNOWN to %s',
                run_id, payout['payout_id'], target)
    payout['execution_status'] = target
    payout['execution_ref'] = ref
    payout['execution_updated_at'] = when
    return target


def _compare(payout, settlement):
    if payout is None:
        return UNEXPECTED_SETTLEMENT, 'Settlement has a line for a payout we do not have.'
    if settlement is None:
        return MISSING_FROM_SETTLEMENT, 'This payout is missing from the settlement file.'
    our_status = payout['execution_status']
    their_status = settlement['execution_status']
    our_amount = payout['amount_cents']
    their_amount = settlement['amount_cents']
    if (our_status == payouts.UNKNOWN
            and their_status in (payouts.SETTLED, payouts.FAILED)
            and payout['review_status'] == payouts.APPROVED):
        if our_amount == their_amount:
            return RESOLVED, (
                f'Settlement confirmed {their_status} for ${their_amount / 100:,.2f}.')
        return AMOUNT_MISMATCH, (
            f'Settlement says {their_status}, but our amount is ${our_amount / 100:,.2f} '
            f'and settlement says ${their_amount / 100:,.2f}. UNKNOWN was not changed.')
    if our_status != their_status:
        return STATUS_MISMATCH, (
            f'Our payment outcome is {our_status}; settlement says {their_status}.')
    if our_amount != their_amount:
        return AMOUNT_MISMATCH, (
            f'Our amount is ${our_amount / 100:,.2f}; settlement says ${their_amount / 100:,.2f}.')
    return MATCHED, 'Our payment outcome and amount match the settlement file.'


def reconcile(database, fixture=None, now=None):
    """Compare this database's payment outcomes with the settlement file and store one run.

    UNKNOWN moves to SETTLED or FAILED only when settlement confirms it (same amount,
    payout is Approved). The move is logged in execution_events. review_status and
    status are never rewritten, so the Desk queue, stream and Sent word stay as they are.
    """
    path = Path(fixture or FIXTURE)
    settlement = load_settlement(path)
    started = (now or datetime.now(timezone.utc)).isoformat()
    db = _open(database)
    try:
        db.execute('BEGIN IMMEDIATE')
        ours = _load_ours(db)
        cursor = db.execute(
            'INSERT INTO reconciliation_runs (started_at, fixture_name, matched_count, '
            'mismatched_count, unknown_count) VALUES (?, ?, 0, 0, 0)',
            (started, path.name))
        run_id = cursor.lastrowid
        items = []
        for payout_id in sorted(set(ours) | set(settlement)):
            payout, line = ours.get(payout_id), settlement.get(payout_id)
            result, reason = _compare(payout, line)
            if result == RESOLVED:
                _resolve(db, payout, line, run_id, started)
            our_status = None if payout is None else payout['execution_status']
            settlement_status = None if line is None else line['execution_status']
            item = _item(run_id, payout_id, our_status, settlement_status, result, reason)
            db.execute(
                'INSERT INTO reconciliation_items (run_id, payout_id, our_status, '
                'settlement_status, result, reason) VALUES (?, ?, ?, ?, ?, ?)',
                (run_id, payout_id, our_status, settlement_status, result, reason))
            item['item_id'] = db.execute('SELECT last_insert_rowid()').fetchone()[0]
            items.append(item)
        ended = (now or datetime.now(timezone.utc)).isoformat()
        matched = sum(1 for item in items if item['result'] in (MATCHED, RESOLVED))
        mismatched = sum(1 for item in items if item['result'] in MISMATCH_RESULTS)
        unknown = sum(1 for item in items
                      if (ours.get(item['payout_id']) or {}).get('execution_status')
                      == payouts.UNKNOWN)
        db.execute(
            'UPDATE reconciliation_runs SET ended_at = ?, matched_count = ?, '
            'mismatched_count = ?, unknown_count = ? WHERE run_id = ?',
            (ended, matched, mismatched, unknown, run_id))
        db.commit()
    finally:
        db.close()
    run = dict(run_id=run_id, started_at=started, ended_at=ended, fixture_name=path.name,
               matched_count=matched, mismatched_count=mismatched, unknown_count=unknown)
    return dict(run=run, items=items)


def list_runs(database):
    with sqlite3.connect(str(database)) as db:
        return [dict(zip(RUN_COLUMNS, row))
                for row in db.execute(
                    f'SELECT {", ".join(RUN_COLUMNS)} FROM reconciliation_runs ORDER BY run_id')]


def list_items(database, run_id=None):
    query = f'SELECT {", ".join(ITEM_COLUMNS)} FROM reconciliation_items'
    args = ()
    if run_id is not None:
        query, args = query + ' WHERE run_id = ?', (run_id,)
    with sqlite3.connect(str(database)) as db:
        return [dict(zip(ITEM_COLUMNS, row))
                for row in db.execute(query + ' ORDER BY run_id, payout_id', args)]


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ('-h', '--help'):
        print('Usage: python -m reconciliation DATABASE [FIXTURE]', file=sys.stderr)
        return 2
    report = reconcile(args[0], args[1] if len(args) > 1 else None)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
