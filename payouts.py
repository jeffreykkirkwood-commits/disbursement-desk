# START HERE — Disbursement Desk data: the payout record, its allowed status changes,
# the fixed set of fictional payouts every new demo starts with, and the operator's
# approve / hold / return decisions. Behind the scenes only: no screen uses this yet (the
# Desk screens come later, in issue #41).
"""Payout table, transitions, synthetic seed data (#34) and operator decisions (#35)."""
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
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
# The three rules from issue #35. This file only stores which rule fired and a plain
# sentence; the rules engine that decides it is built in #35. AMOUNT_THRESHOLD is the
# value the seed below was designed around.
DUPLICATE_ACCOUNT = 'DUPLICATE_ACCOUNT'
AMOUNT_OVER_THRESHOLD = 'AMOUNT_OVER_THRESHOLD'
PAYEE_NAME_MISMATCH = 'PAYEE_NAME_MISMATCH'
FLAG_TEXT = {
    DUPLICATE_ACCOUNT: 'Another payee already uses this destination account',
    AMOUNT_OVER_THRESHOLD: 'Amount is over the $2,000.00 review threshold',
    PAYEE_NAME_MISMATCH: 'Name on the bank account does not match the payee',
}
AMOUNT_THRESHOLD = Decimal('2000.00')

# FEATURE 06 — Create the payout and audit tables
# Payouts live in the same per-visitor SQLite file as payments. Amounts are whole cents.
# Note fields stay empty until notes are built (#37, #38). payout_events is the operator
# action log (who, what, when, why) that the approve/hold/return API (#35) will append to.
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
           'rail', 'status', 'flag_code', 'flag_text', 'note_text', 'note_source', 'note_fallback',
           'created_at', 'updated_at')

# FEATURE 07 — Fix the fictional starting set
# Every name and DEMO- account below is invented. The current queue mirrors the approved
# Desk mock (issue #40/#41): 3 payouts need review, 1 is held, plus final and clean ones.
# Each exception rule has at least one payout. The history rows give each payee a few
# earlier PAID payouts so later rules can compare against "usual" amounts.
SEED_RANDOM = 34
BASE_TIME = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)
CURRENT = (
    # payout_id, payee, account, name on account, amount, rail, status, flag, day offset
    ('PO-1036', 'Quartz Copy Shop', 'DEMO-QRTZ-008', 'Quartz Copy Shop', '215.00', 'ACH', APPROVED, None, 25),
    ('PO-1037', 'Maple Tutoring LLC', 'DEMO-MAPL-007', 'M. Lane Holdings', '450.00', 'RTP', RETURNED, PAYEE_NAME_MISMATCH, 25),
    ('PO-1038', 'Harbor Hardware', 'DEMO-HRBR-006', 'Harbor Hardware', '2075.50', 'ACH', HELD, AMOUNT_OVER_THRESHOLD, 25),
    ('PO-1039', 'Nordwind Transport', 'DEMO-NWND-003', 'Nordwind Transport', '1200.00', 'ACH', PAID, None, 26),
    ('PO-1040', 'Cedar Cleaning Co.', 'DEMO-CEDR-004', 'Cedar Cleaning Co.', '640.00', 'ACH', PAID, None, 26),
    ('PO-1041', 'Pine Street Print', 'DEMO-PINE-005', 'Pine Street Print', '98.15', 'RTP', PAID, None, 26),
    ('PO-1042', 'Acme Supplies', 'DEMO-ACME-001', 'Acme Supplies', '4850.00', 'ACH', FLAGGED, AMOUNT_OVER_THRESHOLD, 27),
    ('PO-1043', 'Bluebird Bakery', 'DEMO-BLUE-002', 'Blue Bird Baking Ltd', '312.40', 'RTP', FLAGGED, PAYEE_NAME_MISMATCH, 27),
    ('PO-1044', 'Northwind Freight', 'DEMO-NWND-003', 'Northwind Freight', '1200.00', 'ACH', FLAGGED, DUPLICATE_ACCOUNT, 27),
    ('PO-1045', 'Willow Lane Florist', 'DEMO-WLLW-009', 'Willow Lane Florist', '187.25', 'RTP', PENDING, None, 27),
)
HISTORY_PAYEES = (  # payee, account, usual amount in dollars
    ('Acme Supplies', 'DEMO-ACME-001', 790), ('Bluebird Bakery', 'DEMO-BLUE-002', 300),
    ('Cedar Cleaning Co.', 'DEMO-CEDR-004', 640), ('Pine Street Print', 'DEMO-PINE-005', 95),
    ('Harbor Hardware', 'DEMO-HRBR-006', 520),
)


# FEATURE 08 — Build the seed rows
# Pure function: the same rows on every call and every boot (fixed random seed, fixed
# timestamps). Nothing here reads the clock or the network.
def seed_rows():
    rng = random.Random(SEED_RANDOM)
    rows, number = [], 1001
    for index, (payee, account, usual) in enumerate(HISTORY_PAYEES):
        for visit in range(3):
            cents = usual * 100 + rng.randint(-usual * 10, usual * 10)  # within ±10% of usual
            when = (BASE_TIME + timedelta(days=visit * 7 + index)).isoformat()
            rows.append(dict(payout_id=f'PO-{number}', payee_name=payee, payee_account=account,
                             account_name=payee, amount_cents=cents, currency='USD',
                             rail=rng.choice(('ACH', 'RTP')), status=PAID, flag_code=None,
                             flag_text=None, note_text=None, note_source=None, note_fallback=None,
                             created_at=when, updated_at=when))
            number += 1
    for payout_id, payee, account, name, amount, rail, status, flag, day in CURRENT:
        when = (BASE_TIME + timedelta(days=day)).isoformat()
        rows.append(dict(payout_id=payout_id, payee_name=payee, payee_account=account,
                         account_name=name, amount_cents=int(Decimal(amount) * 100), currency='USD',
                         rail=rail, status=status, flag_code=flag, flag_text=FLAG_TEXT.get(flag),
                         note_text=None, note_source=None, note_fallback=None,
                         created_at=when, updated_at=when))
    return rows


# FEATURE 09 — Create tables and seed a new demo once
# Called when a visitor demo database is opened. Seeds only an empty payout table, so a
# restart keeps that visitor's changes. Returns how many rows were added.
def ensure_seeded(database):
    with sqlite3.connect(str(database), timeout=10) as db:
        db.execute('BEGIN IMMEDIATE')
        for statement in SCHEMA:
            db.execute(statement)
        if db.execute('SELECT COUNT(*) FROM payouts').fetchone()[0]:
            return 0
        return _insert_seed(db)


# FEATURE 10 — Put this one demo back to the starting set
# Clears this database's payouts and action log and reseeds them. It only touches the
# file it is given (one visitor's demo), never anyone else's. No route calls it yet.
def reset(database):
    with sqlite3.connect(str(database), timeout=10) as db:
        db.execute('BEGIN IMMEDIATE')
        for statement in SCHEMA:
            db.execute(statement)
        db.execute('DELETE FROM payout_events')
        db.execute('DELETE FROM payouts')
        return _insert_seed(db)


def _insert_seed(db):
    rows = seed_rows()
    db.executemany(f'INSERT INTO payouts ({", ".join(COLUMNS)}) VALUES ({", ".join("?" * len(COLUMNS))})',
                   [tuple(row[c] for c in COLUMNS) for row in rows])
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


# FEATURE 12 — Name the three Desk decisions
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


# FEATURE 13 — Check the decision details
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


# FEATURE 14 — Record one decision, at most once per key
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


# FEATURE 15 — Read the action log
# Oldest first, for one payout or the whole demo. Read-only.
def list_events(database, payout_id=None):
    query = f'SELECT {", ".join(EVENT_COLUMNS)} FROM payout_events'
    args = ()
    if payout_id is not None:
        query, args = query + ' WHERE payout_id = ?', (payout_id,)
    with sqlite3.connect(str(database)) as db:
        return [dict(zip(EVENT_COLUMNS, row)) for row in db.execute(query + ' ORDER BY event_id', args)]
