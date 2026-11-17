# START HERE — Automation that routes each new payout through the exception rules
# (issue #44). It runs in-process when a payout is created or the seed loads, so
# there is no new hosted service. Clean payouts move to PAID and are logged.
# Flagged payouts stop at FLAGGED for a person. This path never approves, holds
# or returns a flagged payout. Running it twice changes nothing. Nothing on any
# screen changes.
"""In-process payout automation. Reuses the #37 rules and notes and the #51 event log."""
import storage
from datetime import datetime, timezone

import aba
import api_contract
import payout_notes
import payouts
import stripe_connect
from desk_common import APIError

# FEATURE 01 — Name the automation actor and the log action
# The action is "route", never approve, hold or return, so a flagged payout cannot
# be decided by this path. The actor is "automation", never an operator name.
# Create uses action "create" so a double-submit can replay via Idempotency-Key.
ACTOR = 'automation'
ACTION = 'route'
CREATE_ACTION = payouts.CREATE


def _earlier_than(db, payout):
    rows = db.execute(
        f'SELECT {", ".join(payouts.COLUMNS)} FROM payouts WHERE created_at < ?',
        (payout['created_at'],)).fetchall()
    return [dict(zip(payouts.COLUMNS, row)) for row in rows]


def _load(db, payout_id):
    row = db.execute(
        f'SELECT {", ".join(payouts.COLUMNS)} FROM payouts WHERE payout_id = ?',
        (payout_id,)).fetchone()
    if not row:
        raise APIError(404, 'Payout not found.')
    return dict(zip(payouts.COLUMNS, row))


# FEATURE 02 — Route one PENDING payout through the rules
# Recomputes the flag with explain_flag() and stores the #37 rules note. A clean
# payout moves PENDING -> APPROVED -> PAID in one save (the same two steps the
# #51 approve path uses) and appends one action-log row. A flagged payout moves
# only to FLAGGED. Anything already past PENDING is left alone, so a second run
# changes nothing and this path cannot approve, hold or return a flagged payout.
def route_one(db, payout_id, now=None):
    payout = _load(db, payout_id)
    current = payout['status']
    if current != payouts.PENDING:
        return dict(changed=False, payout_id=payout_id, status=current, event=None)
    payout['flag_code'], payout['flag_text'] = payouts.explain_flag(
        payout, _earlier_than(db, payout))
    payout_notes.apply(payout)
    targets = (payouts.FLAGGED,) if payout['flag_code'] else (payouts.APPROVED, payouts.PAID)
    step = current
    for target in targets:
        payouts.check_transition(step, target)
        step = target
    at = (now or datetime.now(timezone.utc)).isoformat()
    review, outcome = payouts.review_and_outcome(step)
    if stripe_connect.stripe_enabled() and step == payouts.PAID:
        db.execute(
            'UPDATE payouts SET status = ?, review_status = ?, execution_status = ?, '
            'flag_code = ?, flag_text = ?, note_text = ?, '
            'note_source = ?, note_fallback = ?, updated_at = ? WHERE payout_id = ?',
            (payouts.APPROVED, payouts.APPROVED, payouts.NOT_STARTED,
             payout['flag_code'], payout['flag_text'], payout['note_text'],
             payout['note_source'], int(payout['note_fallback']), at, payout_id))
    else:
        db.execute(
            'UPDATE payouts SET status = ?, review_status = ?, execution_status = ?, '
            'flag_code = ?, flag_text = ?, note_text = ?, '
            'note_source = ?, note_fallback = ?, updated_at = ? WHERE payout_id = ?',
            (step, review, outcome, payout['flag_code'], payout['flag_text'], payout['note_text'],
             payout['note_source'], int(payout['note_fallback']), at, payout_id))
    held = None
    if step == payouts.PAID:
        held = payouts._settle(db, payout_id, at, actor=ACTOR,
                               idempotency_key=f'settle-screen:route:{payout_id}')
        if held:
            if held.get('action') == payouts.SCREEN:
                return dict(changed=True, payout_id=payout_id, status=payouts.FLAGGED, event=held)
    evidence = None
    if held and held.get('kind') == 'stripe_submit':
        evidence = {
            'stripe_transfer_id': held['transfer_id'],
            'stripe_destination': held['destination'],
            'stripe_idempotency_key': held['idempotency_key'],
            'stripe_livemode': False,
        }
    key = f'route:{payout_id}'
    event = payouts.record_transition(
        db, payout_id, ACTION, current, step, ACTOR, key,
        reason=payout['flag_text'], at=at, evidence=evidence)
    return dict(changed=True, payout_id=payout_id, status=step, event=event)


# FEATURE 03 — Route every PENDING payout on an open connection
# Oldest first, so a later payout in the same batch can see an earlier one that
# just moved (needed for the 24-hour duplicate rule). Called after the seed
# insert: the starting set has no PENDING rows, so this is a no-op there.
def route_pending(db, now=None):
    ids = [row[0] for row in db.execute(
        'SELECT payout_id FROM payouts WHERE status = ? ORDER BY created_at, payout_id',
        (payouts.PENDING,))]
    return [route_one(db, payout_id, now=now) for payout_id in ids]


# FEATURE 04 — Route payouts in one visitor database
# Opens one locked transaction, then routes either one payout or every PENDING
# payout. Works only on the file it is given.
def route(database, payout_id=None, now=None):
    with storage.connect(str(database), timeout=10) as db:
        db.execute('BEGIN IMMEDIATE')
        for statement in payouts.SCHEMA:
            db.execute(statement)
        if payout_id is None:
            return route_pending(db, now=now)
        return route_one(db, payout_id, now=now)


def _create_fingerprint(payout_id, payee_name, payee_account, amount_cents, rail,
                        bank_details_changed_at):
    return {
        'payout_id': payout_id,
        'payee_name': payee_name,
        'payee_account': payee_account,
        'amount_cents': amount_cents,
        'rail': rail,
        'bank_details_changed_at': bank_details_changed_at,
    }


# FEATURE 05 — Create one new synthetic payout and route it
# Inserts the row as PENDING (not checked yet), fills the usual amount from that
# payee's earlier payouts when they exist, then runs route_one(). Callers pass
# invented DEMO- accounts and amounts only. An optional ABA routing number is
# checked (#112) when one is entered; a number that fails the check digit is
# refused and nothing is saved. An Idempotency-Key (required on HTTP; optional
# here with default create:{payout_id}) makes a double-submit replay the first
# create instead of inserting a second disbursement. A create audit row is
# written before routing so every birth is in the action log.
def create(database, payout_id, payee_name, payee_account, amount_cents, rail,
           bank_details_changed_at=None, now=None, routing_number=None,
           idempotency_key=None):
    if not isinstance(payout_id, str) or not payout_id.strip() or len(payout_id) > 40:
        raise APIError(422, 'Provide a payout id (1–40 characters).',
                       code=api_contract.VALIDATION_ERROR)
    if not isinstance(payee_name, str) or not payee_name.strip() or len(payee_name.strip()) > 80:
        raise APIError(422, 'Provide a payee name (1–80 characters).',
                       code=api_contract.VALIDATION_ERROR)
    if not isinstance(payee_account, str) or not payee_account.startswith('DEMO-'):
        raise APIError(422, 'Payee account must be a DEMO- id.',
                       code=api_contract.VALIDATION_ERROR)
    if not isinstance(amount_cents, int) or isinstance(amount_cents, bool) or amount_cents <= 0:
        raise APIError(422, 'Amount must be a positive number of cents.',
                       code=api_contract.VALIDATION_ERROR)
    if rail is None or (isinstance(rail, str) and not rail.strip()):
        rail = payouts.DEFAULT_RAIL
    elif rail not in payouts.RAILS:
        raise APIError(422, 'Rail must be ACH, SAME_DAY_ACH, RTP, FEDNOW, or WIRE.',
                       code=api_contract.VALIDATION_ERROR)
    if bank_details_changed_at is not None:
        if not isinstance(bank_details_changed_at, str):
            raise APIError(422, 'Bank-details change time must be text if given.',
                           code=api_contract.VALIDATION_ERROR)
        try:
            parsed = datetime.fromisoformat(bank_details_changed_at)
        except ValueError:
            parsed = None
        if parsed is None or parsed.tzinfo is None:
            raise APIError(
                422, 'Bank-details change time must be an ISO 8601 datetime with a timezone offset.',
                code=api_contract.VALIDATION_ERROR)
    aba.validate(routing_number)
    import rails
    rails.enforce_send(rail, amount_cents, submitted_at=now)
    payout_id, payee_name = payout_id.strip(), payee_name.strip()
    payee_account = payee_account.strip()
    key = payouts.clean_idempotency_key(
        idempotency_key if idempotency_key is not None else f'create:{payout_id}')
    fingerprint = _create_fingerprint(
        payout_id, payee_name, payee_account, amount_cents, rail, bank_details_changed_at)
    at = (now or datetime.now(timezone.utc)).isoformat()
    with storage.connect(str(database), timeout=10) as db:
        db.execute('BEGIN IMMEDIATE')
        for statement in payouts.SCHEMA:
            db.execute(statement)
        db.execute('PRAGMA foreign_keys = ON')
        payouts._upgrade(db)
        existing = db.execute(
            f'SELECT {", ".join(payouts.EVENT_COLUMNS)} FROM payout_events WHERE idempotency_key = ?',
            (key,)).fetchone()
        if existing:
            previous = payouts._as_event(existing)
            prior = (previous.get('evidence') or {}) if isinstance(previous.get('evidence'), dict) else {}
            if previous['action'] != CREATE_ACTION or prior != fingerprint:
                raise APIError(409, 'This Idempotency-Key was already used for a different create.',
                               code=api_contract.IDEMPOTENCY_KEY_REUSED)
            payout = _load(db, previous['payout_id'])
            return dict(changed=False, replayed=True, payout_id=previous['payout_id'],
                        status=payout['status'], event=None, create_event=previous)
        if db.execute('SELECT 1 FROM payouts WHERE payout_id = ?', (payout_id,)).fetchone():
            raise APIError(409, 'A payout with this id already exists.',
                           code=api_contract.CONFLICT)
        # Usual amount is the average of earlier SETTLED payouts, matching the seed,
        # so a prior flagged or held outlier does not hide a new amount exception.
        past = [row[0] for row in db.execute(
            'SELECT amount_cents FROM payouts WHERE payee_name = ? AND execution_status = ?',
            (payee_name, payouts.SETTLED))]
        usual = sum(past) // len(past) if past else None
        review, outcome = payouts.review_and_outcome(payouts.PENDING)
        row = dict(payout_id=payout_id, payee_name=payee_name, payee_account=payee_account,
                   account_name=payee_name, amount_cents=amount_cents, currency='USD', rail=rail,
                   status=payouts.PENDING, flag_code=None, flag_text=None,
                   payee_usual_cents=usual, bank_details_changed_at=bank_details_changed_at,
                   note_text=None, note_source=None, note_fallback=None,
                   created_at=at, updated_at=at,
                   review_status=review, execution_status=outcome, execution_ref=None,
                   execution_updated_at=None, stripe_connected_account_id=None)
        db.execute(
            f'INSERT INTO payouts ({", ".join(payouts.COLUMNS)}) VALUES '
            f'({", ".join("?" * len(payouts.COLUMNS))})',
            tuple(row[c] for c in payouts.COLUMNS))
        create_event = payouts.record_transition(
            db, payout_id, CREATE_ACTION, payouts.PENDING, payouts.PENDING, ACTOR, key,
            reason=None, at=at, evidence=fingerprint)
        result = route_one(db, payout_id, now=now)
        result['create_event'] = create_event
        result['replayed'] = False
        return result
