# START HERE — Disbursement Desk data: the payout record, its allowed status changes,
# the fixed set of fictional payouts every new demo starts with, the operator's
# approve / hold / return decisions, and the hook that runs automation after a seed.
# The /desk page (desk.py) reads the current queue from here.
"""Payout table, transitions, synthetic seed data (#34), operator decisions (#35), stored rules notes (#37) and automation hook (#44)."""
import random
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import payout_notes
from payments import APIError

# FEATURE 01 — Name every payout status
# PENDING = not checked yet; FLAGGED = a rule fired, waiting for an operator ("Needs
# review" on the Desk); APPROVED = operator or automation cleared it; HELD = operator
# paused it; RETURNED = operator sent it back (final); PAID = sent to the payee (final,
# "Sent" on the Desk). All money here is fictional.
PENDING, FLAGGED, APPROVED, HELD, RETURNED, PAID = (
    'PENDING', 'FLAGGED', 'APPROVED', 'HELD', 'RETURNED', 'PAID')
STATUSES = (PENDING, FLAGGED, APPROVED, HELD, RETURNED, PAID)

# FEATURE 02 — List the allowed status changes
# Every move a payout may make. Anything not listed is blocked. Held payouts can still be
# approved later (approved design, issue #41). RETURNED and PAID are final.
TRANSITIONS = {
    PENDING: frozenset({FLAGGED, APPROVED}),
    FLAGGED: frozenset({APPROVED, HELD, RETURNED}),
    HELD: frozenset({APPROVED, RETURNED}),
    APPROVED: frozenset({PAID}),
    RETURNED: frozenset(),
    PAID: frozenset(),
}
FINAL_STATUSES = frozenset(s for s, targets in TRANSITIONS.items() if not targets)


# FEATURE 03 — Answer "may this payout move here?"
# True only for a listed move. Unknown statuses are never allowed.
def can_transition(current, target):
    return target in TRANSITIONS.get(current, frozenset())


# FEATURE 04 — Refuse an illegal move with a clear message
# Raise the same 409 error type the payment routes use, so a later API (issue #35) can
# return it unchanged.
def check_transition(current, target):
    if current not in TRANSITIONS or target not in TRANSITIONS:
        raise APIError(422, 'Unknown payout status.')
    if not can_transition(current, target):
        raise APIError(409, f'A {current} payout cannot move to {target}.')


# FEATURE 05 — Name the exception rules
# The three rules approved for the Desk (issue #35 decision log): an amount more than 3x
# the payee's usual payout, bank details changed within 7 days, and the same payee and
# amount within 24 hours. The seed works out each payout's flag from its own data
# (payee_usual_cents, bank_details_changed_at and earlier payouts) with explain_flag()
# below, and stores the rule code and sentence. The live automation path (issue #44,
# payout_rules.py) uses the same explain_flag() when a payout is created or the seed loads.
AMOUNT_OVER_3X_AVG = 'AMOUNT_OVER_3X_AVG'
BANK_CHANGED_7D = 'BANK_CHANGED_7D'
DUPLICATE_24H = 'DUPLICATE_24H'
FLAG_TEXT = {  # generic wording per rule; explain_flag() writes the exact sentence per payout
    AMOUNT_OVER_3X_AVG: 'Amount is more than 3× this payee’s usual payout',
    BANK_CHANGED_7D: 'Payee bank details changed within the last 7 days',
    DUPLICATE_24H: 'Same payee and amount within 24 hours (possible duplicate)',
}
USUAL_MULTIPLIER = 3
BANK_CHANGE_WINDOW = timedelta(days=7)
DUPLICATE_WINDOW = timedelta(hours=24)

# FEATURE 06 — Create the payout and audit tables
# Payouts live in the same per-visitor SQLite file as payments. Amounts are whole cents.
# payee_usual_cents is the payee's usual (average earlier) payout and bank_details_changed_at is
# when the payee's bank details last changed (NULL = not recently); both are the facts
# behind a flag. Note fields are filled by payout_notes.apply() from those stored rules
# (#37). They stay 'rules' notes with fallback off; an AI source is reserved for later
# (#38). payout_events is the operator action log (who, what, when, why).
SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS payouts (
        payout_id TEXT PRIMARY KEY,
        payee_name TEXT NOT NULL,
        payee_account TEXT NOT NULL CHECK(payee_account LIKE 'DEMO-%'),
        account_name TEXT NOT NULL,
        amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
        currency TEXT NOT NULL CHECK(currency = 'USD'),
        rail TEXT NOT NULL CHECK(rail IN ('ACH', 'RTP')),
        status TEXT NOT NULL CHECK(status IN ('PENDING', 'FLAGGED', 'APPROVED', 'HELD', 'RETURNED', 'PAID')),
        flag_code TEXT,
        flag_text TEXT,
        payee_usual_cents INTEGER CHECK(payee_usual_cents IS NULL OR payee_usual_cents > 0),
        bank_details_changed_at TEXT,
        note_text TEXT,
        note_source TEXT CHECK(note_source IS NULL OR note_source IN ('ai', 'rules')),
        note_fallback INTEGER CHECK(note_fallback IS NULL OR note_fallback IN (0, 1)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )''',
    '''CREATE TABLE IF NOT EXISTS payout_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        payout_id TEXT NOT NULL REFERENCES payouts(payout_id),
        action TEXT NOT NULL,
        from_status TEXT NOT NULL,
        to_status TEXT NOT NULL,
        actor TEXT NOT NULL,
        reason TEXT,
        idempotency_key TEXT UNIQUE,
        at TEXT NOT NULL
    )''',
)
COLUMNS = ('payout_id', 'payee_name', 'payee_account', 'account_name', 'amount_cents', 'currency',
           'rail', 'status', 'flag_code', 'flag_text', 'payee_usual_cents', 'bank_details_changed_at', 'note_text', 'note_source', 'note_fallback',
           'created_at', 'updated_at')

# FEATURE 07 — Fix the fictional starting set
# Every name and DEMO- account below is invented. The current queue mirrors the approved
# Desk mock (issue #40/#41): 3 payouts need review (one per rule), 1 is held, 1 returned,
# the rest sent. No seeded payout is PENDING or APPROVED: viewers only ever see Needs
# review (FLAGGED), Held, Returned and Sent (PAID). Flags are not listed here; they are
# worked out from the data. The history rows give each payee five earlier PAID payouts
# whose average is exactly the payee's usual amount.
SEED_RANDOM = 34
BASE_TIME = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)
CURRENT = (
    # payout_id, payee, account, amount, rail, status, hours after BASE_TIME,
    # bank details last changed (hours after BASE_TIME, or None for not recently)
    ('PO-1036', 'Quartz Copy Shop', 'DEMO-QRTZ-008', '215.00', 'ACH', PAID, 25 * 24, None),
    ('PO-1037', 'Maple Tutoring LLC', 'DEMO-MAPL-007', '450.00', 'RTP', RETURNED, 25 * 24, 24 * 24),
    ('PO-1038', 'Harbor Hardware', 'DEMO-HRBR-006', '2075.50', 'ACH', HELD, 25 * 24 + 2, None),
    ('PO-1039', 'Northwind Freight', 'DEMO-NWND-003', '1200.00', 'ACH', PAID, 26 * 24 + 4, None),
    ('PO-1040', 'Cedar Cleaning Co.', 'DEMO-CEDR-004', '640.00', 'ACH', PAID, 26 * 24, None),
    ('PO-1041', 'Pine Street Print', 'DEMO-PINE-005', '98.15', 'RTP', PAID, 26 * 24 + 1, None),
    ('PO-1042', 'Acme Supplies', 'DEMO-ACME-001', '4850.00', 'ACH', FLAGGED, 27 * 24, None),
    ('PO-1043', 'Bluebird Bakery', 'DEMO-BLUE-002', '312.40', 'RTP', FLAGGED, 27 * 24 + 1, 25 * 24 + 1),
    ('PO-1044', 'Northwind Freight', 'DEMO-NWND-003', '1200.00', 'ACH', FLAGGED, 27 * 24 + 2, None),
    ('PO-1045', 'Willow Lane Florist', 'DEMO-WLLW-009', '187.25', 'RTP', PAID, 27 * 24 - 3, None),
)
HISTORY_PAYEES = (  # payee, account, usual amount in dollars
    ('Acme Supplies', 'DEMO-ACME-001', 790), ('Bluebird Bakery', 'DEMO-BLUE-002', 300),
    ('Northwind Freight', 'DEMO-NWND-003', 1200), ('Cedar Cleaning Co.', 'DEMO-CEDR-004', 640),
    ('Pine Street Print', 'DEMO-PINE-005', 95), ('Harbor Hardware', 'DEMO-HRBR-006', 520),
)


def _row(payout_id, payee, account, cents, rail, status, when, usual=None, changed=None):
    return dict(payout_id=payout_id, payee_name=payee, payee_account=account, account_name=payee,
                amount_cents=cents, currency='USD', rail=rail, status=status, flag_code=None,
                flag_text=None, payee_usual_cents=usual, bank_details_changed_at=changed,
                note_text=None, note_source=None, note_fallback=None, created_at=when, updated_at=when)


# FEATURE 07b — Work out why a payout is flagged, from its data
# Checks the three approved rules in order against the payout's own facts and the earlier
# payouts given, and returns (rule code, plain sentence), or (None, None) if none fires.
# Used to build the seed so every stored reason is backed by data.
def explain_flag(payout, earlier):
    when = datetime.fromisoformat(payout['created_at'])
    usual = payout.get('payee_usual_cents')
    if usual and payout['amount_cents'] > USUAL_MULTIPLIER * usual:
        ratio = round(payout['amount_cents'] / usual)
        return AMOUNT_OVER_3X_AVG, f'Amount is {ratio}× this payee’s usual payout (${usual / 100:,.0f} avg)'
    changed = payout.get('bank_details_changed_at')
    if changed and timedelta(0) <= when - datetime.fromisoformat(changed) <= BANK_CHANGE_WINDOW:
        days = (when.date() - datetime.fromisoformat(changed).date()).days
        ago = 'today' if days == 0 else f'{days} day{"s" if days > 1 else ""} ago'
        return BANK_CHANGED_7D, f'Payee bank details changed {ago}'
    for other in earlier:
        then = datetime.fromisoformat(other['created_at'])
        if (other['payout_id'] != payout['payout_id'] and other['payee_name'] == payout['payee_name']
                and other['amount_cents'] == payout['amount_cents']
                and timedelta(0) < when - then <= DUPLICATE_WINDOW):
            day = 'yesterday' if (when.date() - then.date()).days == 1 else 'earlier today'
            return DUPLICATE_24H, f'Same payee and amount as {day} (possible duplicate)'
    return None, None


# FEATURE 08 — Build the seed rows
# Pure function: the same rows on every call and every boot (fixed random seed, fixed
# timestamps). Nothing here reads the clock or the network. History amounts come in
# +/- pairs around the usual amount (within 10%), so their average is exactly the usual.
# Each current payout's usual amount is the average of that payee's earlier payouts, and
# its flag comes from explain_flag(). Each row then gets a stored rules note.
def seed_rows():
    rng = random.Random(SEED_RANDOM)
    rows, number = [], 1001
    for index, (payee, account, usual) in enumerate(HISTORY_PAYEES):
        a, b = rng.randint(1, usual * 10), rng.randint(1, usual * 10)
        for visit, offset in enumerate((a, -a, b, -b, 0)):
            when = (BASE_TIME + timedelta(days=visit * 4 + index // 2, hours=index)).isoformat()
            rows.append(_row(f'PO-{number}', payee, account, usual * 100 + offset,
                             rng.choice(('ACH', 'RTP')), PAID, when))
            number += 1
    history = list(rows)
    for payout_id, payee, account, amount, rail, status, hours, changed in CURRENT:
        when = (BASE_TIME + timedelta(hours=hours)).isoformat()
        past = [r['amount_cents'] for r in history if r['payee_name'] == payee]
        usual = sum(past) // len(past) if past else None
        changed = None if changed is None else (BASE_TIME + timedelta(hours=changed)).isoformat()
        rows.append(_row(payout_id, payee, account, int(Decimal(amount) * 100), rail, status, when,
                         usual, changed))
    for row in rows[len(history):]:
        row['flag_code'], row['flag_text'] = explain_flag(row, [r for r in rows if r['created_at'] < row['created_at']])
    for row in rows:
        payout_notes.apply(row)
    return rows


# FEATURE 09 — Create tables, upgrade older demos, and seed a new demo once
# Called when a visitor demo database is opened. A payouts table made by an earlier
# version gets any missing columns (ALTER TABLE ADD COLUMN, only when missing, so running
# it again does nothing). If that upgrade happened and no operator has acted yet (empty
# action log), its old starting set is replaced with the current one, so old flag reasons
# don't linger. Otherwise only an empty payout table is seeded, so a restart keeps that
# visitor's changes. Returns how many rows were added.
ADDED_COLUMNS = (('payee_usual_cents', 'INTEGER CHECK(payee_usual_cents IS NULL OR payee_usual_cents > 0)'),
                 ('bank_details_changed_at', 'TEXT'))


def _upgrade(db):
    existing = {row[1] for row in db.execute('PRAGMA table_info(payouts)')}
    missing = [(name, kind) for name, kind in ADDED_COLUMNS if name not in existing]
    for name, kind in missing:
        db.execute(f'ALTER TABLE payouts ADD COLUMN {name} {kind}')
    return bool(missing)


def ensure_seeded(database):
    with sqlite3.connect(str(database), timeout=10) as db:
        db.execute('BEGIN IMMEDIATE')
        for statement in SCHEMA:
            db.execute(statement)
        upgraded = _upgrade(db)
        if upgraded and not db.execute('SELECT COUNT(*) FROM payout_events').fetchone()[0]:
            db.execute('DELETE FROM payouts')
        if db.execute('SELECT COUNT(*) FROM payouts').fetchone()[0]:
            _backfill_notes(db)
            return 0
        return _insert_seed(db)


# FEATURE 10 — Put this one demo back to the starting set
# Clears this database's payouts and action log and reseeds them. It only touches the
# file it is given (one visitor's demo), never anyone else's. The Desk footer calls
# this through POST /payouts/reset (#32 slice 4).
def reset(database):
    with sqlite3.connect(str(database), timeout=10) as db:
        db.execute('BEGIN IMMEDIATE')
        for statement in SCHEMA:
            db.execute(statement)
        _upgrade(db)
        db.execute('DELETE FROM payout_events')
        db.execute('DELETE FROM payouts')
        return _insert_seed(db)


# FEATURE 10b — Fill empty note columns on an existing demo
# Writes a rules note only where note_text is still NULL. Status, flags and the
# action log are left alone, so an operator's work is kept. Running it again
# after notes exist does nothing.
def _backfill_notes(db):
    rows = db.execute(
        f'SELECT {", ".join(COLUMNS)} FROM payouts WHERE note_text IS NULL').fetchall()
    for raw in rows:
        payout = payout_notes.apply(dict(zip(COLUMNS, raw)))
        db.execute(
            'UPDATE payouts SET note_text = ?, note_source = ?, note_fallback = ? '
            'WHERE payout_id = ?',
            (payout['note_text'], payout['note_source'],
             int(payout['note_fallback']), payout['payout_id']))
    return len(rows)


def _insert_seed(db):
    rows = seed_rows()
    db.executemany(f'INSERT INTO payouts ({", ".join(COLUMNS)}) VALUES ({", ".join("?" * len(COLUMNS))})',
                   [tuple(row[c] for c in COLUMNS) for row in rows])
    # The starting set has no PENDING rows, so automation here changes nothing.
    import payout_rules
    payout_rules.route_pending(db)
    return len(rows)


# FEATURE 11 — Read payouts back
# Dollar strings for amounts (exact, like the payments API) and True/False for the
# fallback flag. Read-only.
def _public(row):
    payout = dict(zip(COLUMNS, row))
    payout['amount'] = format(Decimal(payout.pop('amount_cents')) / 100, '.2f')
    if payout['note_fallback'] is not None:
        payout['note_fallback'] = bool(payout['note_fallback'])
    return payout


def list_payouts(database):
    with sqlite3.connect(str(database)) as db:
        rows = db.execute(f'SELECT {", ".join(COLUMNS)} FROM payouts ORDER BY payout_id').fetchall()
    return [_public(row) for row in rows]


def get_payout(database, payout_id):
    with sqlite3.connect(str(database)) as db:
        row = db.execute(f'SELECT {", ".join(COLUMNS)} FROM payouts WHERE payout_id = ?',
                         (payout_id,)).fetchone()
    if not row:
        raise APIError(404, 'Payout not found.')
    return _public(row)


# FEATURE 12 — Read the Desk's current queue
# Every payout except the 30 history rows, which exist only for usual-amount math
# (a history view was declined, #40). Newly created payouts, including ones flagged
# after the seed, are included so they appear on the Desk (#80). Ordered the way the
# approved mock lists them: Needs review, then Held, then Returned, then Sent, each by id.
CURRENT_IDS = tuple(row[0] for row in CURRENT)
HISTORY_IDS = frozenset(row['payout_id'] for row in seed_rows() if row['payout_id'] not in CURRENT_IDS)
QUEUE_ORDER = {FLAGGED: 0, PENDING: 0, HELD: 1, RETURNED: 2, APPROVED: 3, PAID: 3}


def current_queue(database):
    history = tuple(HISTORY_IDS)
    with closing(sqlite3.connect(str(database))) as db:
        rows = db.execute(f'SELECT {", ".join(COLUMNS)} FROM payouts WHERE payout_id NOT IN '
                          f'({", ".join("?" * len(history))})', history).fetchall()
    queue = [_public(row) for row in rows]
    return sorted(queue, key=lambda p: (QUEUE_ORDER[p['status']], p['payout_id']))


# FEATURE 13 — Name the three Desk decisions
# Each operator decision and where it leaves the payout. Approve goes straight on to PAID
# ("Sent" on the Desk) through APPROVED inside one save, so no Desk action ever leaves a
# payout showing PENDING or APPROVED (approved design, issue #35 comment). Only payouts
# waiting for an operator (FLAGGED = "Needs review", or HELD) can be decided; HELD can
# still be approved or returned later.
APPROVE, HOLD, RETURN = 'approve', 'hold', 'return'
DECISIONS = {APPROVE: (APPROVED, PAID), HOLD: (HELD,), RETURN: (RETURNED,)}
DECIDABLE = frozenset({FLAGGED, HELD})
EVENT_COLUMNS = ('event_id', 'payout_id', 'action', 'from_status', 'to_status', 'actor', 'reason',
                 'idempotency_key', 'at')


# FEATURE 14 — Check the decision details
# Reviewer name is required (it goes in the action log). A reason is optional: Jeffrey
# approved confirm-only Hold and Return, so a blank or missing reason is saved as empty.
# The idempotency key works like the payment routes' Idempotency-Key header.
def _clean(action, actor, reason, key):
    if action not in DECISIONS:
        raise APIError(422, 'Decision must be approve, hold or return.')
    if not isinstance(key, str) or not key.strip() or len(key) > 200:
        raise APIError(422, 'Provide an Idempotency-Key header (1–200 characters).')
    if not isinstance(actor, str) or not actor.strip() or len(actor.strip()) > 80:
        raise APIError(422, 'Provide a reviewer name (1–80 characters).')
    if reason is not None and not isinstance(reason, str):
        raise APIError(422, 'Reason must be text if given.')
    reason = (reason or '').strip() or None
    if reason is not None and len(reason) > 500:
        raise APIError(422, 'Reason must be 500 characters or fewer.')
    return actor.strip(), reason


# FEATURE 15 — Record one decision, at most once per key
# One locked save: look up the key, check the move, update the payout and append one row
# to the action log (reviewer, from/to status, reason, key, time). Repeating the same
# decision with the same key changes nothing and returns the first result (200); the same
# key with different details is refused (409). Illegal moves are refused with a clear
# 409 and change nothing. Works only on the one viewer database it is given.
def decide(database, payout_id, action, actor, idempotency_key, reason=None, now=None):
    actor, reason = _clean(action, actor, reason, idempotency_key)
    with sqlite3.connect(str(database), timeout=10) as db:
        db.execute('BEGIN IMMEDIATE')
        for statement in SCHEMA:
            db.execute(statement)
        existing = db.execute(f'SELECT {", ".join(EVENT_COLUMNS)} FROM payout_events WHERE idempotency_key = ?',
                              (idempotency_key,)).fetchone()
        if existing:
            previous = dict(zip(EVENT_COLUMNS, existing))
            if (previous['payout_id'], previous['action'], previous['actor'], previous['reason']) != (
                    payout_id, action, actor, reason):
                raise APIError(409, 'This Idempotency-Key was already used for a different decision.')
            return 200, previous
        row = db.execute('SELECT status FROM payouts WHERE payout_id = ?', (payout_id,)).fetchone()
        if not row:
            raise APIError(404, 'Payout not found.')
        current = row[0]
        if current not in DECIDABLE:
            raise APIError(409, f'A {current} payout cannot be decided; only FLAGGED or HELD payouts can '
                                f'be approved, held or returned.')
        step = current
        for target in DECISIONS[action]:
            check_transition(step, target)
            step = target
        at = (now or datetime.now(timezone.utc)).isoformat()
        db.execute('UPDATE payouts SET status = ?, updated_at = ? WHERE payout_id = ?', (step, at, payout_id))
        cursor = db.execute('INSERT INTO payout_events (payout_id, action, from_status, to_status, actor, reason, '
                            'idempotency_key, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                            (payout_id, action, current, step, actor, reason, idempotency_key, at))
        return 201, dict(event_id=cursor.lastrowid, payout_id=payout_id, action=action, from_status=current,
                         to_status=step, actor=actor, reason=reason, idempotency_key=idempotency_key, at=at)


# FEATURE 16 — Read the action log
# Oldest first, for one payout or the whole demo. Read-only.
def list_events(database, payout_id=None):
    query = f'SELECT {", ".join(EVENT_COLUMNS)} FROM payout_events'
    args = ()
    if payout_id is not None:
        query, args = query + ' WHERE payout_id = ?', (payout_id,)
    with sqlite3.connect(str(database)) as db:
        return [dict(zip(EVENT_COLUMNS, row)) for row in db.execute(query + ' ORDER BY event_id', args)]
