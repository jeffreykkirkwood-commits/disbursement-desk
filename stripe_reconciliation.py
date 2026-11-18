"""Reconcile Desk payouts with Stripe. Stripe is the source of truth for SETTLED and FAILED.

Dry-run is the default and does not write. Apply reuses payouts.apply_stripe_event.
The hand-written CSV job in reconciliation.py is a separate source and is not used here.

Item results:
    matched            Desk execution_status already equals the Stripe outcome.
    would_update       The move is legal. Dry-run reports it and does not write.
    updated            Apply wrote the legal move.
    mismatch           Stripe disagrees, but the move is illegal or the amount,
                       currency, reference, destination, or metadata conflicts.
    missing_on_stripe  Stripe retrieve returned no object.
    skipped            The row is outside the statuses this job moves, or Stripe
                       is not terminal yet. A submitted row whose Stripe object
                       is still in flight is matched, not skipped.
    error              Retrieve or configuration failed for that row, or a live-mode
                       object was refused.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from desk_common import APIError
import payouts
import storage
import stripe_connect


MATCHED = 'matched'
WOULD_UPDATE = 'would_update'
UPDATED = 'updated'
MISMATCH = 'mismatch'
MISSING_ON_STRIPE = 'missing_on_stripe'
SKIPPED = 'skipped'
ERROR = 'error'
ITEM_RESULTS = (
    MATCHED, WOULD_UPDATE, UPDATED, MISMATCH, MISSING_ON_STRIPE, SKIPPED, ERROR)
# Rows this job will compare. Other execution statuses on a Stripe reference are skipped.
IN_SCOPE_STATUSES = frozenset({
    payouts.SUBMITTED, payouts.UNKNOWN, payouts.SETTLED, payouts.FAILED})
_TERMINAL = frozenset({payouts.SETTLED, payouts.FAILED})
# Operator surface: persist the last run's mismatch view next to the demo database.
LAST_SUMMARY_FILE = 'stripe_recon_last.json'
MISMATCH_RESULTS = frozenset({MISMATCH, MISSING_ON_STRIPE, ERROR})
MAX_MISMATCH_ITEMS = 50


def reconcile_stripe(database, *, dry_run=True, now=None, retrieve=None, env=None):
    """Compare in-scope Desk rows with Stripe and return a JSON-ready report.

    In-scope rows have an execution_ref starting with tr_, pi_, or po_ and an
    execution_status of SUBMITTED, UNKNOWN, SETTLED, or FAILED. retrieve(ref)
    returns the Stripe object dict, or None when Stripe has no such object.
    The default retrieve calls stripe_connect.retrieve_object.
    """
    if retrieve is None:
        def retrieve(ref, _env=env):
            return stripe_connect.retrieve_object(ref, env=_env)
    tenant = storage.tenant_from_path(database)
    items = []
    for payout in _scoped_payouts(database):
        item, command = _judge(payout, retrieve, tenant)
        if item['result'] == WOULD_UPDATE and not dry_run:
            item = _apply(database, item, command, now)
        items.append(item)
    run = {name: sum(item['result'] == name for item in items) for name in ITEM_RESULTS}
    return {'dry_run': bool(dry_run), 'run': run, 'items': items}


def summary_path(database):
    return Path(database).resolve().parent / LAST_SUMMARY_FILE


def mismatch_summary(report, *, operator_id='', ran_at=None):
    """Build the operator-facing last-run summary from a reconcile_stripe report."""
    when = ran_at or datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    mismatches = [
        item for item in report.get('items') or []
        if item.get('result') in MISMATCH_RESULTS
    ][:MAX_MISMATCH_ITEMS]
    return {
        'ran_at': when,
        'dry_run': bool(report.get('dry_run')),
        'operator_id': operator_id or '',
        'run': dict(report.get('run') or {}),
        'mismatches': mismatches,
    }


def load_last_summary(database):
    path = summary_path(database)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or 'run' not in data:
        return None
    return data


def save_last_summary(database, summary):
    path = summary_path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    return summary


def run_and_remember(database, *, dry_run=True, operator_id='', now=None,
                     retrieve=None, env=None, ran_at=None):
    """Run reconcile_stripe and persist the mismatch summary for the Desk surface."""
    report = reconcile_stripe(
        database, dry_run=dry_run, now=now, retrieve=retrieve, env=env)
    summary = mismatch_summary(report, operator_id=operator_id, ran_at=ran_at)
    save_last_summary(database, summary)
    return {'report': report, 'summary': summary}


def _scoped_payouts(database):
    payouts.ensure_seeded(database)
    with storage.connect(str(database)) as db:
        rows = db.execute(
            f'SELECT {", ".join(payouts.COLUMNS)} FROM payouts ORDER BY payout_id').fetchall()
    chosen = []
    for row in rows:
        payout = dict(zip(payouts.COLUMNS, row))
        if stripe_connect.is_stripe_ref(payout.get('execution_ref')):
            chosen.append(payout)
    return chosen


def _item(payout, result, reason, stripe_outcome=None):
    return {
        'payout_id': payout['payout_id'],
        'execution_ref': payout.get('execution_ref') or '',
        'execution_status': payout['execution_status'],
        'stripe_outcome': stripe_outcome,
        'result': result,
        'reason': reason,
    }


def _judge(payout, retrieve, tenant):
    if payout['execution_status'] not in IN_SCOPE_STATUSES:
        return _item(
            payout, SKIPPED,
            'This reference is outside the Stripe reconciliation statuses.'), None
    try:
        obj = retrieve(payout.get('execution_ref') or '')
    except APIError:
        return _item(payout, ERROR, 'Stripe retrieve is not configured.'), None
    except Exception:
        return _item(payout, ERROR, 'Stripe retrieve failed.'), None
    if obj is None:
        return _item(payout, MISSING_ON_STRIPE, 'Stripe has no object for this id.'), None
    try:
        facts = stripe_connect.object_facts(obj)
    except ValueError:
        return _item(payout, ERROR, 'Stripe returned an object this desk cannot read.'), None
    result, reason, command = _classify(payout, facts, tenant)
    return _item(payout, result, reason, facts.get('outcome')), command


def _classify(payout, facts, tenant):
    if facts.get('livemode'):
        return ERROR, 'A live-mode Stripe object is blocked.', None
    outcome = facts.get('outcome')
    if outcome is None:
        if payout['execution_status'] == payouts.SUBMITTED:
            return MATCHED, 'Stripe has not finished and the Desk is still submitted.', None
        return SKIPPED, 'Stripe has not finished this object.', None
    if outcome not in _TERMINAL:
        return ERROR, 'Stripe outcome is not settled or failed.', None
    conflict = _conflict(payout, facts, tenant)
    if conflict:
        return MISMATCH, conflict, None
    if payout['execution_status'] == outcome:
        return MATCHED, 'Desk outcome already matches Stripe.', None
    if payout['status'] == payouts.RETURNED:
        return MISMATCH, 'A returned payout cannot take a Stripe outcome.', None
    try:
        payouts.check_execution_transition(payout['execution_status'], outcome)
    except APIError as exc:
        return MISMATCH, exc.message, None
    return WOULD_UPDATE, f"Desk {payout['execution_status']} would move to {outcome}.", _command(
        payout, facts, outcome, tenant)


def _conflict(payout, facts, tenant):
    if facts.get('id') != (payout.get('execution_ref') or ''):
        return 'The Stripe id does not match the Desk reference.'
    meta_payout = facts.get('payout_id') or ''
    if meta_payout and meta_payout != payout['payout_id']:
        return 'Stripe metadata names a different payout.'
    meta_demo = facts.get('demo_id') or ''
    if meta_demo and tenant and meta_demo != tenant:
        return 'Stripe metadata names a different demo.'
    if facts.get('amount_cents') != payout['amount_cents']:
        return 'The Stripe amount does not match the Desk amount.'
    if facts.get('currency') != 'usd':
        return 'The Stripe currency is not usd.'
    column = payout.get('stripe_connected_account_id')
    if column and column != (facts.get('destination') or ''):
        return 'The Stripe destination does not match the stored connected account.'
    return None


def _command(payout, facts, outcome, tenant):
    object_id = facts['id']
    return {
        'stripe_event_id': f'recon:stripe:{object_id}:{outcome}',
        'payout_id': facts.get('payout_id') or payout['payout_id'],
        'demo_id': facts.get('demo_id') or tenant or '',
        'transfer_id': object_id,
        'amount_cents': facts.get('amount_cents'),
        'currency': facts.get('currency'),
        'destination': facts.get('destination') or '',
        'outcome': outcome,
        'livemode': bool(facts.get('livemode')),
    }


def _apply(database, item, command, now):
    try:
        written = payouts.apply_stripe_event(database, command, now=now)
    except APIError:
        return dict(item, result=ERROR, reason='Stripe outcome was not written.')
    if written != 'applied':
        return dict(item, result=ERROR, reason='Stripe outcome was not written.')
    return dict(item, result=UPDATED, reason=f"Desk outcome updated to {command['outcome']}.")


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    apply = '--apply' in args
    args = [arg for arg in args if arg != '--apply']
    if len(args) != 1 or args[0] in ('-h', '--help'):
        print('Usage: python -m stripe_reconciliation DATABASE [--apply]', file=sys.stderr)
        return 2
    report = reconcile_stripe(args[0], dry_run=not apply)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
