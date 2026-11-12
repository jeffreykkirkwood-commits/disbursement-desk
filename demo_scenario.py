# One fixed end-to-end demo scenario: seed, scripted decisions, one replay, and one reconciliation (#61).
# Synthetic data only. There are no network calls and no real money, bank or provider.
"""Load the seed, apply scripted decisions, replay once, reconcile once, and return a stable snapshot."""
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import execution_replay
import payout_events
import payouts
import reconciliation


ACTOR = 'Scenario Reviewer'
NOW = datetime(2026, 11, 11, 21, 0, tzinfo=timezone.utc)
# One of each person decision, on the starting Needs-review payouts.
DECISIONS = (
    ('PO-1042', 'approve', 'scenario-approve', None),
    ('PO-1043', 'hold', 'scenario-hold', None),
    ('PO-1044', 'return', 'scenario-return', None),
)
EXPECTED = Path(__file__).resolve().parent / 'fixtures' / 'demo_scenario_expected.json'
PAYOUT_FIELDS = (
    'payout_id', 'status', 'review_status', 'execution_status', 'execution_ref',
    'execution_updated_at', 'flag_code', 'flag_text',
)


def _public_decision(event):
    return {key: event[key] for key in (
        'payout_id', 'action', 'from_status', 'to_status', 'actor', 'reason',
        'idempotency_key', 'at', 'evidence')}


def _public_payout(payout):
    return {key: payout[key] for key in PAYOUT_FIELDS}


def _public_execution(event):
    row = {key: event[key] for key in (
        'event_id', 'payout_id', 'execution_status', 'execution_ref', 'at', 'source')}
    # Reconciliation logs recon:{run_id}:{payout_id}. run_id is an
    # autoincrement that survives DELETE, so two resets would otherwise
    # differ. The snapshot already omits autoincrement ids.
    event_id = row['event_id'] or ''
    if event.get('source') == payouts.RECONCILIATION and event_id.startswith('recon:'):
        parts = event_id.split(':')
        if len(parts) >= 3 and parts[1].isdigit():
            row['event_id'] = ':'.join(['recon', *parts[2:]])
    return row


def _recon_rows(database, table, columns):
    with sqlite3.connect(str(database)) as db:
        db.execute('PRAGMA foreign_keys = ON')
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not exists:
            return []
        return [dict(zip(columns, row)) for row in db.execute(
            f'SELECT {", ".join(columns)} FROM {table} ORDER BY 1')]


def snapshot(database, replay_report=None):
    """Stable JSON view of the scenario results (no autoincrement ids or file paths)."""
    decided = [payouts.get_payout(database, payout_id) for payout_id, *_ in DECISIONS]
    replay_ids = sorted(execution_replay.REPLAY_PAYOUT_IDS)
    replayed = [payouts.get_payout(database, payout_id) for payout_id in replay_ids]
    report = replay_report or {}
    return {
        'decisions': [_public_decision(event) for event in payouts.list_events(database)],
        'payouts': [_public_payout(payout) for payout in decided + replayed],
        'queue_ids': [payout['payout_id'] for payout in payouts.current_queue(database)],
        'replay': {
            'results': report.get('results'),
            'applied': report.get('applied'),
            'skipped': report.get('skipped'),
            'refused': report.get('refused'),
            'noop': report.get('noop'),
        },
        'execution_events': [_public_execution(event)
                             for event in payouts.list_execution_events(database)],
        'reconciliation_runs': _recon_rows(
            database, 'reconciliation_runs',
            ('fixture_name', 'matched_count', 'mismatched_count', 'unknown_count')),
        'reconciliation_items': _recon_rows(
            database, 'reconciliation_items',
            ('payout_id', 'our_status', 'settlement_status', 'result', 'reason')),
    }


def load_expected(path=None):
    return json.loads(Path(path or EXPECTED).read_text())


def run(database, now=None, reset=True):
    """Restore the starting set if asked, then apply the scripted decisions, one replay, and one reconciliation.

    review_status / status changes are only the scripted Approve, Hold and Return;
    reconciliation does not rewrite them, so the Desk words and layout stay as they are.
    """
    if reset:
        payouts.reset(database)
        # Same as POST /payouts/reset: drop the old stream log so the next
        # sync starts from the fresh seed, not the previous demo's statuses.
        payout_events.after_reset(database)
    else:
        payouts.ensure_seeded(database)
    when = now or NOW
    for payout_id, action, key, reason in DECISIONS:
        payouts.decide(database, payout_id, action, ACTOR, key, reason=reason, now=when)
    replay_report = execution_replay.replay(database)
    # Same path as POST /reconciliation: prepare the hidden targets, then compare.
    reconciliation.reconcile(database, now=when)
    return snapshot(database, replay_report)


def write_expected(database, path=None):
    Path(path or EXPECTED).write_text(
        json.dumps(run(database), indent=2, sort_keys=True) + '\n')


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ('-h', '--help'):
        print('Usage: python -m demo_scenario DATABASE', file=sys.stderr)
        return 2
    report = run(args[0])
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
