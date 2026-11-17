# START HERE — Disbursement Desk data: the payout record, its allowed status changes,
# the fixed set of fictional payouts every new demo starts with, the operator's
# approve / hold / return decisions, and the hook that runs automation after a seed.
# The /desk page (desk.py) reads the current queue from here.
"""Payout table, transitions, synthetic seed data (#34), operator decisions (#35), stored rules notes (#37), automation hook (#44), review/outcome columns (#54) and decision evidence plus demo settle (#55 option B)."""
import copy
import functools
import json
import random
import re
import storage
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import api_contract
import ofac
import operators
import payout_notes
import rails
import stripe_connect
from desk_common import APIError

# FEATURE 01 — Name every payout status
# PENDING = not checked yet; FLAGGED = a rule fired, waiting for an operator ("Needs
# review" on the Desk); APPROVED = operator or automation cleared it; HELD = operator
# paused it; RETURNED = operator sent it back (final); PAID = sent to the payee (final,
# "Sent" on the Desk). All money here is fictional.
PENDING, FLAGGED, APPROVED, HELD, RETURNED, PAID = (
    'PENDING', 'FLAGGED', 'APPROVED', 'HELD', 'RETURNED', 'PAID')
STATUSES = (PENDING, FLAGGED, APPROVED, HELD, RETURNED, PAID)

# FEATURE 01b — Name the review decision and payment outcome (#54, docs/DESK-STATES.md)
# These are stored separately from today's status column. status stays the source for
# the queue, the stream and the Sent word. The new-payee rule and the usual-amount
# average read execution_status SETTLED (#55). review_status uses the same codes as
# status except there is no PAID (Approved is APPROVED; Needs review is FLAGGED; not
# checked is PENDING). execution_status is the payment outcome.
NOT_STARTED, SUBMITTED, UNKNOWN, SETTLED, FAILED = (
    'NOT_STARTED', 'SUBMITTED', 'UNKNOWN', 'SETTLED', 'FAILED')
EXECUTION_STATUSES = (NOT_STARTED, SUBMITTED, UNKNOWN, SETTLED, FAILED)
REVIEW_STATUSES = (PENDING, FLAGGED, APPROVED, HELD, RETURNED)
DEMO, REPLAY, RECONCILIATION, STRIPE = 'demo', 'replay', 'reconciliation', 'stripe'
EXECUTION_SOURCES = (DEMO, REPLAY, RECONCILIATION, STRIPE)
_EXECUTION_SOURCE_CHECK = ', '.join(repr(source) for source in EXECUTION_SOURCES)
RULES_VERSION = 'desk-rules-1'
EXECUTION_TRANSITIONS = {
    NOT_STARTED: frozenset({SUBMITTED}),
    SUBMITTED: frozenset({SETTLED, FAILED, UNKNOWN}),
    UNKNOWN: frozenset({SETTLED, FAILED}),
    SETTLED: frozenset(),
    FAILED: frozenset(),
}


def review_and_outcome(status):
    """Map today's status to (review_status, execution_status) for backfill and writes."""
    if status == PAID:
        return APPROVED, SETTLED
    if status == APPROVED:
        return APPROVED, NOT_STARTED
    if status in (FLAGGED, PENDING, HELD, RETURNED):
        return status, NOT_STARTED
    raise ValueError(f'Unknown payout status: {status}')


def check_execution_transition(current, target):
    """Refuse an illegal payment-outcome move. UNKNOWN cannot go back to SUBMITTED."""
    if current not in EXECUTION_TRANSITIONS or target not in EXECUTION_TRANSITIONS:
        raise APIError(422, 'Unknown payment outcome.')
    if target not in EXECUTION_TRANSITIONS[current]:
        if current == UNKNOWN and target == SUBMITTED:
            raise APIError(409, 'An UNKNOWN payment outcome cannot be submitted or retried automatically.')
        raise APIError(409, f'A {current} payment outcome cannot move to {target}.')


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
# amount within 24 hours. A fourth rule (issue #86, option C on #79, tightened in #89)
# flags a payout over $1,000 to a payee who does not yet have a PAID payout; $1,000
# exactly is not over. A fifth rule (#111) screens the payee name against a dated
# local OFAC SDN snapshot and flags a possible sanctions match. When more than one
# rule trips, every reason is stored, not just
# the first. The seed works out each payout's flag from its own data (payee_usual_cents,
# bank_details_changed_at and earlier payouts) with explain_flag() below, and stores the
# rule code and sentence. The live automation path (issue #44, payout_rules.py) uses the
# same explain_flag() when a payout is created or the seed loads.
#
# P0 mandatory pre-send gates: every code in MANDATORY_PRE_SEND_RULES must be evaluated
# by explain_flag() before a payout can auto-settle. Any hit stops automation at FLAGGED.
# Skipping a rule (or settling despite its flag) is a release blocker — see
# docs/pre-send-gates.md and test_pre_send_gates.py.
AMOUNT_OVER_3X_AVG = 'AMOUNT_OVER_3X_AVG'
BANK_CHANGED_7D = 'BANK_CHANGED_7D'
DUPLICATE_24H = 'DUPLICATE_24H'
NEW_PAYEE_OVER_1000 = 'NEW_PAYEE_OVER_1000'
POSSIBLE_SANCTIONS_MATCH = ofac.FLAG_CODE
FLAG_TEXT = {  # generic wording per rule; explain_flag() writes the exact sentence per payout
    AMOUNT_OVER_3X_AVG: 'Amount is more than 3× this payee’s usual payout',
    BANK_CHANGED_7D: 'Payee bank details changed within the last 7 days',
    DUPLICATE_24H: 'Same payee and amount within 24 hours (possible duplicate)',
    NEW_PAYEE_OVER_1000: 'No paid payout to this payee yet; amount is over $1,000',
    POSSIBLE_SANCTIONS_MATCH: 'Possible sanctions match',
}
# Mandatory exception / sanctions codes that must fire before send (docs/pre-send-gates.md).
MANDATORY_PRE_SEND_RULES = frozenset(FLAG_TEXT)
USUAL_MULTIPLIER = 3
BANK_CHANGE_WINDOW = timedelta(days=7)
DUPLICATE_WINDOW = timedelta(hours=24)
NEW_PAYEE_LIMIT_CENTS = 100000  # $1,000; flags only when the amount is over this and the payee has no PAID payout yet
FLAG_CODE_SEP = ','
FLAG_TEXT_SEP = '. '

# FEATURE 06 — Create the payout and audit tables
# Payouts live in the same per-visitor SQLite file as payments. Amounts are whole cents.
# payee_usual_cents is the payee's usual (average earlier) payout and bank_details_changed_at is
# when the payee's bank details last changed (NULL = not recently); both are the facts
# behind a flag. Note fields are filled by payout_notes.apply() from those stored rules
# (#37). They stay 'rules' notes with fallback off; an AI source is reserved for later
# (#38). payout_events is the operator action log (who, what, when, why).
# review_status, execution_status, execution_ref and execution_updated_at are the #54
# state split (docs/DESK-STATES.md). New tables hold later replay and reconciliation
# rows; they start empty. Today's status column is not replaced.
# FEATURE 06b — Name the payout rails
# The rail column already exists. This expands the allowed values from ACH and RTP
# to ACH, same-day ACH, RTP, FedNow, and wire. FedNow has its own stored code,
# FEDNOW, separate from RTP (Jeffrey, #32, 1:30 PM ET, 12 November 2026).
# Default is ACH. The seed still uses only ACH and RTP. Settlement windows,
# cutoffs, limits and return paths are a follow-up; this change is the schema only.
ACH, SAME_DAY_ACH, RTP, FEDNOW, WIRE = 'ACH', 'SAME_DAY_ACH', 'RTP', 'FEDNOW', 'WIRE'
RAILS = (ACH, SAME_DAY_ACH, RTP, FEDNOW, WIRE)
DEFAULT_RAIL = ACH
RAIL_CHECK = "('ACH', 'SAME_DAY_ACH', 'RTP', 'FEDNOW', 'WIRE')"

SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS payouts (
        payout_id TEXT PRIMARY KEY,
        payee_name TEXT NOT NULL,
        payee_account TEXT NOT NULL CHECK(payee_account LIKE 'DEMO-%'),
        account_name TEXT NOT NULL,
        amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
        currency TEXT NOT NULL CHECK(currency = 'USD'),
        rail TEXT NOT NULL CHECK(rail IN ''' + RAIL_CHECK + '''),
        status TEXT NOT NULL CHECK(status IN ('PENDING', 'FLAGGED', 'APPROVED', 'HELD', 'RETURNED', 'PAID')),
        flag_code TEXT,
        flag_text TEXT,
        payee_usual_cents INTEGER CHECK(payee_usual_cents IS NULL OR payee_usual_cents > 0),
        bank_details_changed_at TEXT,
        note_text TEXT,
        note_source TEXT CHECK(note_source IS NULL OR note_source IN ('ai', 'rules')),
        note_fallback INTEGER CHECK(note_fallback IS NULL OR note_fallback IN (0, 1)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        review_status TEXT CHECK(review_status IS NULL OR review_status IN ('PENDING', 'FLAGGED', 'APPROVED', 'HELD', 'RETURNED')),
        execution_status TEXT NOT NULL DEFAULT 'NOT_STARTED' CHECK(execution_status IN ('NOT_STARTED', 'SUBMITTED', 'UNKNOWN', 'SETTLED', 'FAILED')),
        execution_ref TEXT,
        execution_updated_at TEXT,
        stripe_connected_account_id TEXT
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
        at TEXT NOT NULL,
        evidence TEXT
    )''',
    '''CREATE TABLE IF NOT EXISTS execution_events (
        event_id TEXT PRIMARY KEY,
        payout_id TEXT NOT NULL REFERENCES payouts(payout_id),
        execution_status TEXT NOT NULL CHECK(execution_status IN ('NOT_STARTED', 'SUBMITTED', 'UNKNOWN', 'SETTLED', 'FAILED')),
        execution_ref TEXT,
        at TEXT NOT NULL,
        source TEXT NOT NULL CHECK(source IN (''' + _EXECUTION_SOURCE_CHECK + '''))
    )''',
    '''CREATE TABLE IF NOT EXISTS reconciliation_runs (
        run_id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        fixture_name TEXT NOT NULL,
        matched_count INTEGER NOT NULL DEFAULT 0,
        mismatched_count INTEGER NOT NULL DEFAULT 0,
        unknown_count INTEGER NOT NULL DEFAULT 0
    )''',
    '''CREATE TABLE IF NOT EXISTS reconciliation_items (
        item_id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL REFERENCES reconciliation_runs(run_id),
        payout_id TEXT,
        our_status TEXT,
        settlement_status TEXT,
        result TEXT NOT NULL,
        reason TEXT
    )''',
)
COLUMNS = ('payout_id', 'payee_name', 'payee_account', 'account_name', 'amount_cents', 'currency',
           'rail', 'status', 'flag_code', 'flag_text', 'payee_usual_cents', 'bank_details_changed_at', 'note_text', 'note_source', 'note_fallback',
           'created_at', 'updated_at', 'review_status', 'execution_status', 'execution_ref',
           'execution_updated_at', 'stripe_connected_account_id')

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
    review, outcome = review_and_outcome(status)
    return dict(payout_id=payout_id, payee_name=payee, payee_account=account, account_name=payee,
                amount_cents=cents, currency='USD', rail=rail, status=status, flag_code=None,
                flag_text=None, payee_usual_cents=usual, bank_details_changed_at=changed,
                note_text=None, note_source=None, note_fallback=None, created_at=when, updated_at=when,
                review_status=review, execution_status=outcome, execution_ref=None,
                execution_updated_at=None, stripe_connected_account_id=None)


# FEATURE 07b — Work out why a payout is flagged, from its data
# Checks the approved rules in order against the payout's own facts and the earlier
# payouts given, and returns every rule that fires as (rule codes, plain sentences),
# or (None, None) if none fires. Used to build the seed so every stored reason is
# backed by data. A payee counts as new until earlier contains at least one SETTLED
# payout for the same payee name (issue #89, #55 option B). Several rules can fire at once, so a
# new-payee flag no longer hides a bank-details change.
def split_flag_codes(flag_code):
    if not flag_code:
        return []
    return [part for part in flag_code.split(FLAG_CODE_SEP) if part]


def explain_flag(payout, earlier):
    when = datetime.fromisoformat(payout['created_at'])
    flags = []
    usual = payout.get('payee_usual_cents')
    if usual and payout['amount_cents'] > USUAL_MULTIPLIER * usual:
        ratio = round(payout['amount_cents'] / usual)
        flags.append((AMOUNT_OVER_3X_AVG, f'Amount is {ratio}× this payee’s usual payout (${usual / 100:,.0f} avg)'))
    prior_paid = [other for other in earlier
                  if other['payout_id'] != payout['payout_id']
                  and other['payee_name'] == payout['payee_name']
                  and other.get('execution_status') == SETTLED]
    if not prior_paid and payout['amount_cents'] > NEW_PAYEE_LIMIT_CENTS:
        flags.append((NEW_PAYEE_OVER_1000, FLAG_TEXT[NEW_PAYEE_OVER_1000]))
    changed = payout.get('bank_details_changed_at')
    if changed and timedelta(0) <= when - datetime.fromisoformat(changed) <= BANK_CHANGE_WINDOW:
        days = (when.date() - datetime.fromisoformat(changed).date()).days
        ago = 'today' if days == 0 else f'{days} day{"s" if days > 1 else ""} ago'
        flags.append((BANK_CHANGED_7D, f'Payee bank details changed {ago}'))
    for other in earlier:
        then = datetime.fromisoformat(other['created_at'])
        if (other['payout_id'] != payout['payout_id'] and other['payee_name'] == payout['payee_name']
                and other['amount_cents'] == payout['amount_cents']
                and timedelta(0) < when - then <= DUPLICATE_WINDOW):
            day = 'yesterday' if (when.date() - then.date()).days == 1 else 'earlier today'
            flags.append((DUPLICATE_24H, f'Same payee and amount as {day} (possible duplicate)'))
            break
    match = ofac.screen(payout.get('payee_name'))
    if match:
        flags.append((POSSIBLE_SANCTIONS_MATCH, ofac.flag_sentence(match)))
    if not flags:
        return None, None
    return (FLAG_CODE_SEP.join(code for code, _ in flags),
            FLAG_TEXT_SEP.join(text for _, text in flags))


# FEATURE 08 — Build the seed rows
# Pure function: the same rows on every call and every boot (fixed random seed, fixed
# timestamps). Nothing here reads the clock or the network. History amounts come in
# +/- pairs around the usual amount (within 10%), so their average is exactly the usual.
# Each current payout's usual amount is the average of that payee's earlier payouts, and
# its flag comes from explain_flag(). Each row then gets a stored rules note.
# The starting set, including explain_flag() / ofac.screen(), is built once per process;
# each Reset or new demo gets a copy, so callers cannot change the cached rows (#9).
def seed_rows():
    return copy.deepcopy(_build_seed_rows())


@functools.lru_cache(maxsize=1)
def _build_seed_rows():
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
                 ('bank_details_changed_at', 'TEXT'),
                 ('review_status',
                  "TEXT CHECK(review_status IS NULL OR review_status IN "
                  "('PENDING', 'FLAGGED', 'APPROVED', 'HELD', 'RETURNED'))"),
                 ('execution_status',
                  "TEXT NOT NULL DEFAULT 'NOT_STARTED' CHECK(execution_status IN "
                  "('NOT_STARTED', 'SUBMITTED', 'UNKNOWN', 'SETTLED', 'FAILED'))"),
                 ('execution_ref', 'TEXT'),
                 ('execution_updated_at', 'TEXT'),
                 ('stripe_connected_account_id', 'TEXT'))


def _upgrade(db):
    changed = False
    existing = {row[1] for row in db.execute('PRAGMA table_info(payouts)')}
    missing = [(name, kind) for name, kind in ADDED_COLUMNS if name not in existing]
    for name, kind in missing:
        db.execute(f'ALTER TABLE payouts ADD COLUMN {name} {kind}')
        changed = True
    event_cols = {row[1] for row in db.execute('PRAGMA table_info(payout_events)')}
    if event_cols and 'evidence' not in event_cols:
        db.execute('ALTER TABLE payout_events ADD COLUMN evidence TEXT')
        changed = True
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='execution_events'").fetchone()
    sql = (row[0] or '') if row else ''
    if row and ("'demo'" not in sql or "'stripe'" not in sql):
        db.execute('ALTER TABLE execution_events RENAME TO execution_events_old')
        db.execute(SCHEMA[2])
        db.execute('INSERT INTO execution_events SELECT * FROM execution_events_old')
        db.execute('DROP TABLE execution_events_old')
        changed = True
    return changed


def _rail_check_stale(db):
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='payouts'").fetchone()
    return bool(row and row[0] and 'FEDNOW' not in row[0])


def _rebuild_payouts_rail_check(database):
    """Rebuild payouts when the rail CHECK does not yet allow FEDNOW."""
    with storage.connect(str(database), timeout=10) as db:
        db.execute('PRAGMA foreign_keys = OFF')
        db.execute('BEGIN IMMEDIATE')
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='payouts'").fetchone()
        if not exists or not _rail_check_stale(db):
            return False
        db.execute(SCHEMA[0].replace('IF NOT EXISTS payouts', 'payouts_new', 1))
        cols = [row[1] for row in db.execute('PRAGMA table_info(payouts)')]
        db.execute(
            f'INSERT INTO payouts_new ({", ".join(cols)}) SELECT {", ".join(cols)} FROM payouts')
        db.execute('DROP TABLE payouts')
        db.execute('ALTER TABLE payouts_new RENAME TO payouts')
        return True


def ensure_seeded(database):
    rebuilt = _rebuild_payouts_rail_check(database)
    with storage.connect(str(database), timeout=10) as db:
        db.execute('PRAGMA foreign_keys = ON')
        db.execute('BEGIN IMMEDIATE')
        for statement in SCHEMA:
            db.execute(statement)
        upgraded = _upgrade(db) or rebuilt
        if upgraded and not db.execute('SELECT COUNT(*) FROM payout_events').fetchone()[0]:
            _clear_payout_data(db)
        if db.execute('SELECT COUNT(*) FROM payouts').fetchone()[0]:
            _backfill_notes(db)
            _backfill_state(db)
            return 0
        return _insert_seed(db)


# FEATURE 10 — Put this one demo back to the starting set
# Clears this database's payouts, action log, execution events and reconciliation
# rows, and reseeds them. It only touches the file it is given (one visitor's demo),
# never anyone else's. The Desk footer calls this through POST /payouts/reset
# (#32 slice 4).
def reset(database):
    _rebuild_payouts_rail_check(database)
    with storage.connect(str(database), timeout=10) as db:
        db.execute('PRAGMA foreign_keys = ON')
        db.execute('BEGIN IMMEDIATE')
        for statement in SCHEMA:
            db.execute(statement)
        _upgrade(db)
        _clear_payout_data(db)
        return _insert_seed(db)


# FEATURE 10b — Fill empty note columns on an existing demo
# Writes a rules note only where note_text is still NULL. Status, flags and the
# action log are left alone, so an operator's work is kept. Running it again
# after notes exist does nothing.
def _clear_payout_data(db):
    db.execute('DELETE FROM reconciliation_items')
    db.execute('DELETE FROM reconciliation_runs')
    db.execute('DELETE FROM execution_events')
    db.execute('DELETE FROM payout_events')
    db.execute('DELETE FROM payouts')


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


# FEATURE 10c — Fill review and outcome from today's status
# Writes review_status and execution_status only where review_status is still NULL,
# using the #54 mapping in docs/DESK-STATES.md. Does not change status, so the queue,
# the stream and the Sent word stay as they are. Running it again after a backfill
# changes nothing.
def _backfill_state(db):
    rows = db.execute(
        'SELECT payout_id, status FROM payouts WHERE review_status IS NULL').fetchall()
    for payout_id, status in rows:
        review, outcome = review_and_outcome(status)
        db.execute(
            'UPDATE payouts SET review_status = ?, execution_status = ? WHERE payout_id = ?',
            (review, outcome, payout_id))
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
    # Computed from the rail and created_at; not stored. Desk HTML ignores
    # unknown keys, so this does not change the page.
    payout['rail_timing'] = rails.describe(
        payout['rail'], payout['created_at'], payout['amount'])
    return payout


def list_payouts(database):
    with storage.connect(str(database)) as db:
        rows = db.execute(f'SELECT {", ".join(COLUMNS)} FROM payouts ORDER BY payout_id').fetchall()
    return [_public(row) for row in rows]


def get_payout(database, payout_id):
    with storage.connect(str(database)) as db:
        row = db.execute(f'SELECT {", ".join(COLUMNS)} FROM payouts WHERE payout_id = ?',
                         (payout_id,)).fetchone()
    if not row:
        raise APIError(404, 'Payout not found.', code=api_contract.NOT_FOUND)
    return _public(row)


# FEATURE 12 — Read the Desk's current queue
# Every payout except the 30 history rows, which exist only for usual-amount math
# (a history view was declined, #40). Newly created payouts, including ones flagged
# after the seed, are included so they appear on the Desk (#80). Ordered the way the
# approved mock lists them: Needs review, then Held, then Returned, then Sent, each
# by the id's number so PO-998 comes before PO-1046 (and PO-1046 before PO-10000).
# Ids with no trailing number keep a stable fallback: they sort after numbered ids
# in the same status group, by the id text. Synthetic #56 replay and #57
# reconciliation targets stay off the Desk and off the live stream
# (hidden_queue_ids); they are JSON/API only.
CURRENT_IDS = tuple(row[0] for row in CURRENT)
HISTORY_IDS = frozenset(row['payout_id'] for row in seed_rows() if row['payout_id'] not in CURRENT_IDS)
QUEUE_ORDER = {FLAGGED: 0, PENDING: 0, HELD: 1, RETURNED: 2, APPROVED: 3, PAID: 3}


def hidden_queue_ids():
    """History rows plus synthetic replay and reconciliation targets, which never appear on the Desk."""
    from execution_replay import REPLAY_PAYOUT_IDS
    from reconciliation import RECON_PAYOUT_IDS
    return HISTORY_IDS | REPLAY_PAYOUT_IDS | RECON_PAYOUT_IDS


_ID_NUMBER = re.compile(r'(\d+)$')


def payout_id_sort_key(payout_id):
    """Sort key for a payout id: the trailing number, then the id itself."""
    match = _ID_NUMBER.search(payout_id)
    if match:
        return (0, int(match.group(1)), payout_id)
    return (1, 0, payout_id)


def current_queue(database):
    hidden = tuple(hidden_queue_ids())
    with closing(storage.connect(str(database))) as db:
        rows = db.execute(f'SELECT {", ".join(COLUMNS)} FROM payouts WHERE payout_id NOT IN '
                          f'({", ".join("?" * len(hidden))})', hidden).fetchall()
    queue = [_public(row) for row in rows]
    return sorted(queue, key=lambda p: (QUEUE_ORDER[p['status']], payout_id_sort_key(p['payout_id'])))


# FEATURE 13 — Name the three Desk decisions
# Each operator decision and where it leaves the payout. Approve goes straight on to PAID
# ("Sent" on the Desk) through APPROVED inside one save, so no Desk action ever leaves a
# payout showing PENDING or APPROVED (approved design, issue #35 comment). Only payouts
# waiting for an operator (FLAGGED = "Needs review", or HELD) can be decided; HELD can
# still be approved or returned later.
APPROVE, HOLD, RETURN, CLEAR, BLOCK = 'approve', 'hold', 'return', 'clear', 'block'
CREATE, SCREEN = 'create', 'screen'
DECISIONS = {
    APPROVE: (APPROVED, PAID), HOLD: (HELD,), RETURN: (RETURNED,),
    CLEAR: (APPROVED, PAID), BLOCK: (RETURNED,),
}
DECIDABLE = frozenset({FLAGGED, HELD})
EVENT_COLUMNS = ('event_id', 'payout_id', 'action', 'from_status', 'to_status', 'actor', 'reason',
                 'idempotency_key', 'at', 'evidence')
EXECUTION_EVENT_COLUMNS = ('event_id', 'payout_id', 'execution_status', 'execution_ref', 'at', 'source')


def clean_idempotency_key(key):
    """Require a non-empty Idempotency-Key (1–200 characters), same rules as decide."""
    if not isinstance(key, str) or not key.strip() or len(key) > 200:
        raise APIError(422, 'Provide an Idempotency-Key header (1–200 characters).',
                       code=api_contract.IDEMPOTENCY_KEY_REQUIRED)
    return key.strip()


# FEATURE 14 — Check the decision details
# Reviewer name is required (it goes in the action log). A reason is optional: Jeffrey
# approved confirm-only Hold and Return, so a blank or missing reason is saved as empty.
# The idempotency key works like the payment routes' Idempotency-Key header.
def _clean(action, actor, reason, key):
    if action not in DECISIONS:
        raise APIError(422, 'Decision must be approve, hold, return, clear or block.',
                       code=api_contract.VALIDATION_ERROR)
    key = clean_idempotency_key(key)
    if (not isinstance(actor, str) or not actor.strip()
            or len(actor.strip()) > operators.MAX_ACTOR_LENGTH):
        raise APIError(
            422, f'Operator actor must be 1–{operators.MAX_ACTOR_LENGTH} characters.',
            code=api_contract.VALIDATION_ERROR)
    if reason is not None and not isinstance(reason, str):
        raise APIError(422, 'Reason must be text if given.', code=api_contract.VALIDATION_ERROR)
    reason = (reason or '').strip() or None
    if reason is not None and len(reason) > 500:
        raise APIError(422, 'Reason must be 500 characters or fewer.',
                       code=api_contract.VALIDATION_ERROR)
    return actor.strip(), reason, key


def record_transition(db, payout_id, action, from_status, to_status, actor, idempotency_key,
                      reason=None, at=None, evidence=None):
    """Append one payout_events audit row for a Desk status transition (who/what/when)."""
    payload = None if evidence is None else json.dumps(evidence, sort_keys=True)
    cursor = db.execute(
        'INSERT INTO payout_events (payout_id, action, from_status, to_status, actor, reason, '
        'idempotency_key, at, evidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (payout_id, action, from_status, to_status, actor, reason, idempotency_key, at, payload))
    return dict(event_id=cursor.lastrowid, payout_id=payout_id, action=action,
                from_status=from_status, to_status=to_status, actor=actor, reason=reason,
                idempotency_key=idempotency_key, at=at, evidence=evidence)


# FEATURE 15 — Record one decision, at most once per key
# One locked save: look up the key, check the move, update the payout and append one row
# to the action log (reviewer, from/to status, reason, key, time, and an evidence snapshot).
# Repeating the same decision with the same key changes nothing and returns the first
# result (200); the same key with different details is refused (409). Illegal moves are
# refused with a clear 409 and change nothing. Works only on the one viewer database it
# is given. Approve (#55 option B) records review_status APPROVED and the evidence, then
# the settle step in the same save writes status PAID, an execution event,
# execution_ref and execution_updated_at. The queue, stream and Sent word still
# see PAID. Approve itself does not write PAID.
def decide(database, payout_id, action, actor, idempotency_key, reason=None, now=None):
    actor, reason, idempotency_key = _clean(action, actor, reason, idempotency_key)
    with storage.connect(str(database), timeout=10) as db:
        db.execute('PRAGMA foreign_keys = ON')
        db.execute('BEGIN IMMEDIATE')
        for statement in SCHEMA:
            db.execute(statement)
        _upgrade(db)
        existing = db.execute(f'SELECT {", ".join(EVENT_COLUMNS)} FROM payout_events WHERE idempotency_key = ?',
                              (idempotency_key,)).fetchone()
        if existing:
            previous = _as_event(existing)
            if (previous['payout_id'], previous['action'], previous['actor'], previous['reason']) != (
                    payout_id, action, actor, reason):
                raise APIError(409, 'This Idempotency-Key was already used for a different decision.',
                               code=api_contract.IDEMPOTENCY_KEY_REUSED)
            return 200, previous
        row = db.execute(f'SELECT {", ".join(COLUMNS)} FROM payouts WHERE payout_id = ?',
                         (payout_id,)).fetchone()
        if not row:
            raise APIError(404, 'Payout not found.', code=api_contract.NOT_FOUND)
        payout = dict(zip(COLUMNS, row))
        current = payout['status']
        if current not in DECIDABLE:
            raise APIError(409, f'A {current} payout cannot be decided; only FLAGGED or HELD payouts can '
                                f'be approved, held, returned, cleared or blocked.',
                           code=api_contract.CONFLICT)
        at = (now or datetime.now(timezone.utc)).isoformat()
        flagged_sanctions = POSSIBLE_SANCTIONS_MATCH in split_flag_codes(payout['flag_code'])
        if action == APPROVE and flagged_sanctions:
            raise APIError(409, 'A payout with a possible sanctions match cannot be approved; use Clear or Block.',
                           code=api_contract.CONFLICT)
        if action in (CLEAR, BLOCK) and not flagged_sanctions:
            raise APIError(409, 'Clear and Block apply only to a payout with a possible sanctions match.',
                           code=api_contract.CONFLICT)
        # Screen again before sending. A reviewer who is clearing an already-flagged
        # possible match is allowed to send; any other path that still matches is
        # held for review instead of settling. Approve on an already-flagged
        # possible match is refused above, so only Clear can send it.
        if action in (APPROVE, CLEAR) and not flagged_sanctions:
            match = ofac.screen(payout['payee_name'])
            if match:
                _apply_sanctions_hold(db, payout, match, at)
                return 201, record_transition(
                    db, payout_id, SCREEN, current, FLAGGED, actor, idempotency_key,
                    reason=ofac.flag_sentence(match), at=at)
        step = current
        for target in DECISIONS[action]:
            check_transition(step, target)
            step = target
        held = None
        if action in (APPROVE, CLEAR):
            db.execute(
                'UPDATE payouts SET review_status = ?, updated_at = ? WHERE payout_id = ?',
                (APPROVED, at, payout_id))
            held = _settle(db, payout_id, at, allow_sanctions_clear=(action == CLEAR),
                           actor=actor, idempotency_key=f'settle-screen:{idempotency_key}')
            if held and held.get('action') == SCREEN:
                return 201, held
        else:
            review, outcome = review_and_outcome(step)
            db.execute(
                'UPDATE payouts SET status = ?, review_status = ?, execution_status = ?, updated_at = ? '
                'WHERE payout_id = ?',
                (step, review, outcome, at, payout_id))
        evidence = _decision_evidence(payout, actor, reason, at)
        if held and held.get('kind') == 'stripe_submit':
            evidence['stripe_transfer_id'] = held['transfer_id']
            evidence['stripe_destination'] = held['destination']
            evidence['stripe_idempotency_key'] = held['idempotency_key']
            evidence['stripe_livemode'] = False
        return 201, record_transition(
            db, payout_id, action, current, step, actor, idempotency_key,
            reason=reason, at=at, evidence=evidence)


# FEATURE 16 — Read the action log
# Oldest first, for one payout or the whole demo. Read-only.
def _decision_evidence(payout, actor, reason, at):
    return {
        'amount_cents': payout['amount_cents'],
        'flag_code': payout['flag_code'],
        'flag_text': payout['flag_text'],
        'payee_usual_cents': payout['payee_usual_cents'],
        'bank_details_changed_at': payout['bank_details_changed_at'],
        'rules_version': RULES_VERSION,
        'actor': actor,
        'reason': reason,
        'at': at,
    }


def _as_event(row):
    event = dict(zip(EVENT_COLUMNS, row))
    raw = event.get('evidence')
    if isinstance(raw, str):
        event['evidence'] = json.loads(raw)
    return event


def _apply_sanctions_hold(db, payout, match, at):
    """Store a possible-match flag and leave the payout at FLAGGED for review."""
    sentence = ofac.flag_sentence(match)
    codes = split_flag_codes(payout.get('flag_code'))
    texts = [part for part in (payout.get('flag_text') or '').split(FLAG_TEXT_SEP) if part]
    if POSSIBLE_SANCTIONS_MATCH not in codes:
        codes.append(POSSIBLE_SANCTIONS_MATCH)
        texts.append(sentence)
    payout['flag_code'] = FLAG_CODE_SEP.join(codes)
    payout['flag_text'] = FLAG_TEXT_SEP.join(texts)
    payout_notes.apply(payout)
    db.execute(
        'UPDATE payouts SET status = ?, review_status = ?, execution_status = ?, '
        'flag_code = ?, flag_text = ?, note_text = ?, note_source = ?, note_fallback = ?, '
        'updated_at = ? WHERE payout_id = ?',
        (FLAGGED, FLAGGED, NOT_STARTED, payout['flag_code'], payout['flag_text'],
         payout['note_text'], payout['note_source'], int(payout['note_fallback']),
         at, payout['payout_id']))


def _settle_demo(db, payout_id, at, allow_sanctions_clear=False, actor='automation',
                 idempotency_key=None):
    row = db.execute(
        f'SELECT {", ".join(COLUMNS)} FROM payouts WHERE payout_id = ?', (payout_id,)).fetchone()
    if not row:
        raise APIError(404, 'Payout not found.', code=api_contract.NOT_FOUND)
    payout = dict(zip(COLUMNS, row))
    ref = payout['execution_ref'] or f'demo:{payout_id}'
    event_id = f'demo-settle:{payout_id}'
    already = payout['execution_status'] == SETTLED and payout['status'] == PAID
    if already:
        pass
    elif not allow_sanctions_clear:
        match = ofac.screen(payout['payee_name'])
        if match:
            from_status = payout['status']
            _apply_sanctions_hold(db, payout, match, at)
            key = idempotency_key or f'settle-screen:{payout_id}:{at}'
            return record_transition(
                db, payout_id, SCREEN, from_status, FLAGGED, actor, key,
                reason=ofac.flag_sentence(match), at=at)
    if not already:
        if payout['review_status'] != APPROVED:
            raise APIError(409, 'Only an Approved payout can be settled.')
        # Refuse over-limit amounts and closed same-day rail windows before money moves.
        rails.enforce_send(payout['rail'], payout['amount_cents'], submitted_at=at)
        current = payout['execution_status']
        if current == UNKNOWN:
            check_execution_transition(current, SUBMITTED)
        # Honour the allowed-move table without skipping submit, then store SETTLED.
        if current == NOT_STARTED:
            check_execution_transition(NOT_STARTED, SUBMITTED)
            current = SUBMITTED
        check_execution_transition(current, SETTLED)
        db.execute(
            'UPDATE payouts SET status = ?, review_status = ?, execution_status = ?, '
            'execution_ref = ?, execution_updated_at = ?, updated_at = ? WHERE payout_id = ?',
            (PAID, APPROVED, SETTLED, ref, at, at, payout_id))
    elif not payout['execution_ref'] or not payout['execution_updated_at']:
        db.execute(
            'UPDATE payouts SET execution_ref = ?, execution_updated_at = ? WHERE payout_id = ?',
            (ref, payout['execution_updated_at'] or at, payout_id))
    if db.execute('SELECT 1 FROM execution_events WHERE event_id = ?', (event_id,)).fetchone():
        return None
    db.execute(
        'INSERT INTO execution_events (event_id, payout_id, execution_status, execution_ref, at, source) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (event_id, payout_id, SETTLED, ref, at, DEMO))
    return None


def _demo_id(db):
    path = None
    for item in db.execute('PRAGMA database_list'):
        if item[1] == 'main':
            path = item[2]
            break
    return storage.tenant_from_path(path)


def _settle(db, payout_id, at, allow_sanctions_clear=False, actor='automation',
            idempotency_key=None):
    if not stripe_connect.stripe_enabled():
        return _settle_demo(
            db, payout_id, at, allow_sanctions_clear=allow_sanctions_clear,
            actor=actor, idempotency_key=idempotency_key)
    row = db.execute(
        f'SELECT {", ".join(COLUMNS)} FROM payouts WHERE payout_id = ?', (payout_id,)).fetchone()
    if not row:
        raise APIError(404, 'Payout not found.', code=api_contract.NOT_FOUND)
    payout = dict(zip(COLUMNS, row))
    ref = payout.get('execution_ref') or ''
    if payout['execution_status'] in (SUBMITTED, SETTLED, FAILED) and ref.startswith('tr_'):
        return None
    if not allow_sanctions_clear:
        match = ofac.screen(payout['payee_name'])
        if match:
            from_status = payout['status']
            _apply_sanctions_hold(db, payout, match, at)
            key = idempotency_key or f'settle-screen:{payout_id}:{at}'
            return record_transition(
                db, payout_id, SCREEN, from_status, FLAGGED, actor, key,
                reason=ofac.flag_sentence(match), at=at)
    if payout['review_status'] != APPROVED:
        raise APIError(409, 'Only an Approved payout can be settled.')
    rails.enforce_send(payout['rail'], payout['amount_cents'], submitted_at=at)
    demo_id = _demo_id(db)
    if stripe_connect.send_mode(demo_id=demo_id) != 'stripe':
        return _settle_demo(
            db, payout_id, at, allow_sanctions_clear=allow_sanctions_clear,
            actor=actor, idempotency_key=idempotency_key)
    return _submit_stripe_transfer(db, payout, payout_id, at, demo_id)


def _submit_stripe_transfer(db, payout, payout_id, at, demo_id):
    if payout['execution_status'] == UNKNOWN:
        check_execution_transition(UNKNOWN, SUBMITTED)
    if payout['execution_status'] == SUBMITTED:
        raise APIError(409, 'A SUBMITTED payment outcome cannot move to SUBMITTED.',
                       code=api_contract.CONFLICT)
    if payout['execution_status'] in (SETTLED, FAILED):
        return None
    if payout['execution_status'] != NOT_STARTED:
        check_execution_transition(payout['execution_status'], SUBMITTED)
    destination = stripe_connect.destination_for(
        payout['payee_account'], column=payout.get('stripe_connected_account_id'))
    idem = stripe_connect.transfer_idempotency_key(demo_id, payout_id)
    record = stripe_connect.create_transfer(
        payout_id=payout_id, amount_cents=payout['amount_cents'], currency='usd',
        destination=destination, demo_id=demo_id, idempotency_key=idem)
    check_execution_transition(NOT_STARTED, SUBMITTED)
    stored_dest = payout.get('stripe_connected_account_id') or destination
    db.execute(
        'UPDATE payouts SET status = ?, review_status = ?, execution_status = ?, '
        'execution_ref = ?, execution_updated_at = ?, stripe_connected_account_id = ?, '
        'updated_at = ? WHERE payout_id = ?',
        (PAID, APPROVED, SUBMITTED, record['transfer_id'], at, stored_dest, at, payout_id))
    event_id = f'stripe-submit:{payout_id}'
    if not db.execute('SELECT 1 FROM execution_events WHERE event_id = ?', (event_id,)).fetchone():
        db.execute(
            'INSERT INTO execution_events (event_id, payout_id, execution_status, execution_ref, at, source) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (event_id, payout_id, SUBMITTED, record['transfer_id'], at, STRIPE))
    return {
        'kind': 'stripe_submit',
        'transfer_id': record['transfer_id'],
        'destination': destination,
        'idempotency_key': idem,
        'livemode': False,
    }


def apply_stripe_event(database, command, now=None):
    if not isinstance(command, dict) or command.get('outcome') not in (SETTLED, FAILED):
        return 'ignored'
    if command.get('livemode'):
        raise APIError(422, 'Stripe send is blocked.', code=api_contract.STRIPE_SEND_BLOCKED)
    payout_id = command.get('payout_id')
    event_id = command.get('stripe_event_id')
    transfer_id = command.get('transfer_id') or ''
    if not payout_id or not event_id or not str(transfer_id).startswith('tr_'):
        return 'ignored'
    outcome = command['outcome']
    at = (now or datetime.now(timezone.utc)).isoformat()
    with storage.connect(str(database), timeout=10) as db:
        db.execute('PRAGMA foreign_keys = ON')
        db.execute('BEGIN IMMEDIATE')
        for statement in SCHEMA:
            db.execute(statement)
        _upgrade(db)
        row = db.execute(
            f'SELECT {", ".join(COLUMNS)} FROM payouts WHERE payout_id = ?',
            (payout_id,)).fetchone()
        if not row:
            return 'ignored'
        payout = dict(zip(COLUMNS, row))
        if payout['status'] == RETURNED:
            return 'ignored'
        if command.get('amount_cents') != payout['amount_cents'] or command.get('currency') != 'usd':
            raise APIError(422, 'Stripe send is blocked.', code=api_contract.STRIPE_SEND_BLOCKED)
        ref = payout.get('execution_ref') or ''
        if ref and ref != transfer_id:
            raise APIError(422, 'Stripe send is blocked.', code=api_contract.STRIPE_SEND_BLOCKED)
        column = payout.get('stripe_connected_account_id')
        if column and column != (command.get('destination') or ''):
            raise APIError(422, 'Stripe send is blocked.', code=api_contract.STRIPE_SEND_BLOCKED)
        if payout['execution_status'] == NOT_STARTED:
            return 'pending'
        if db.execute(
                'SELECT 1 FROM execution_events WHERE event_id = ?', (event_id,)).fetchone():
            return 'ignored'
        if payout['execution_status'] == outcome:
            return 'ignored'
        try:
            check_execution_transition(payout['execution_status'], outcome)
        except APIError:
            return 'ignored'
        stored_ref = ref or transfer_id
        db.execute(
            'UPDATE payouts SET execution_status = ?, execution_ref = ?, execution_updated_at = ? '
            'WHERE payout_id = ?',
            (outcome, stored_ref, at, payout_id))
        db.execute(
            'INSERT INTO execution_events (event_id, payout_id, execution_status, execution_ref, at, source) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (event_id, payout_id, outcome, stored_ref, at, STRIPE))
        return 'applied'


def list_events(database, payout_id=None):
    query = f'SELECT {", ".join(EVENT_COLUMNS)} FROM payout_events'
    args = ()
    if payout_id is not None:
        query, args = query + ' WHERE payout_id = ?', (payout_id,)
    with storage.connect(str(database)) as db:
        return [_as_event(row) for row in db.execute(query + ' ORDER BY event_id', args)]


def list_execution_events(database, payout_id=None):
    query = f'SELECT {", ".join(EXECUTION_EVENT_COLUMNS)} FROM execution_events'
    args = ()
    if payout_id is not None:
        query, args = query + ' WHERE payout_id = ?', (payout_id,)
    with storage.connect(str(database)) as db:
        return [dict(zip(EXECUTION_EVENT_COLUMNS, row))
                for row in db.execute(query + ' ORDER BY at, event_id', args)]
