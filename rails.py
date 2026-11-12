# START HERE — Simulated payout-rail timing, cutoffs, per-payment limits and
# return paths. Pure functions over the rail and a submission time; nothing is
# stored and no network is called. Desk HTML is unchanged: callers that already
# serialize a payout (get_payout, list_payouts, current_queue) attach a
# rail_timing object computed here.
"""Simulated settlement windows, ET cutoffs, demo limits and return paths per payout rail."""
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')

# FEATURE 01 — A fixed synthetic holiday calendar
# These dates are a demo list, not the Federal Reserve or ACH holiday calendar.
# Tests use them to prove weekend-and-holiday rollover without tracking the
# real observed-holiday schedule year to year.
SYNTHETIC_HOLIDAYS = frozenset({
    date(2026, 3, 4),
    date(2026, 6, 24),
    date(2026, 11, 11),
})


# FEATURE 02 — Return paths per rail family
# ACH (including Same Day ACH) can come back as a return with a Nacha-style
# reason code. This simulator only models R01 (insufficient funds), R02
# (account closed) and R03 (no account / unable to locate). Those code
# meanings are the published Nacha labels; the simulator does not generate
# a live return file.
# RTP and FedNow transfers are final once sent; the only post-send path is a
# request-for-return, which the receiving side may decline.
# Wire transfers are recalled by a request, not an automatic return.
RETURN_ACH = {'kind': 'return', 'codes': ('R01', 'R02', 'R03')}
RETURN_REQUEST_FOR_RETURN = {'kind': 'request_for_return', 'codes': ()}
RETURN_RECALL = {'kind': 'recall', 'codes': ()}


# FEATURE 03 — Simulated rail table
# Every cutoff, settlement window and per-payment limit below is a labelled
# DEMO value for this simulator. They are not live network rules.
#
# SAME_DAY_ACH's demo limit of $1,000,000 matches Nacha's published Same Day
# ACH per-payment cap through 16 September 2027 (Nacha has announced a
# $10 million cap from 17 September 2027). The 14:45 ET cutoff is a single
# demo window inspired by a common Same Day ACH window, not a claim that
# it is the network's only or official cutoff.
#
# RTP and FedNow are both treated as 24/7 with no cutoff in this simulator,
# but they are separate rows with different demo limits so each can show its
# own timing and caps (Jeffrey, #32, comment 6832389969). Official network
# caps are not recorded here.
#
# ACH's 17:00 ET cutoff, wire's 16:00 ET cutoff, ACH and RTP's $1,000,000
# demo limits, FedNow's $500,000 demo limit and wire's $5,000,000 demo
# limit are simulator figures only.
RAIL_SPECS = {
    'ACH': {
        'cutoff_et': time(17, 0),
        'business_days': 1,
        'immediate': False,
        'limit_cents': 100_000_000,
        'return_path': RETURN_ACH,
    },
    'SAME_DAY_ACH': {
        'cutoff_et': time(14, 45),
        'business_days': 0,
        'immediate': False,
        'limit_cents': 100_000_000,
        'return_path': RETURN_ACH,
    },
    'RTP': {
        'cutoff_et': None,
        'business_days': 0,
        'immediate': True,
        'limit_cents': 100_000_000,
        'return_path': RETURN_REQUEST_FOR_RETURN,
    },
    'FEDNOW': {
        'cutoff_et': None,
        'business_days': 0,
        'immediate': True,
        'limit_cents': 50_000_000,
        'return_path': RETURN_REQUEST_FOR_RETURN,
    },
    'WIRE': {
        'cutoff_et': time(16, 0),
        'business_days': 0,
        'immediate': False,
        'limit_cents': 500_000_000,
        'return_path': RETURN_RECALL,
    },
}

# Settlement-day marker on a business-day rail: the demo cutoff time on the
# settlement date, or 17:00 ET when the rail has no cutoff. This is a label
# for that business day, not a real clearing timestamp.
_SETTLEMENT_MARKER = time(17, 0)


def _as_et(submitted_at):
    """Parse a datetime or ISO string and return it in America/New_York.

    Naive values are treated as already being in ET (the demo's cutoff zone).
    """
    if isinstance(submitted_at, str):
        submitted_at = datetime.fromisoformat(submitted_at)
    if not isinstance(submitted_at, datetime):
        raise TypeError('submitted_at must be a datetime or ISO-8601 string.')
    if submitted_at.tzinfo is None:
        return submitted_at.replace(tzinfo=ET)
    return submitted_at.astimezone(ET)


def _spec(rail):
    try:
        return RAIL_SPECS[rail]
    except KeyError:
        raise ValueError(f'Unknown rail: {rail!r}.') from None


def is_business_day(day):
    """True when day is a weekday that is not on the synthetic holiday list."""
    return day.weekday() < 5 and day not in SYNTHETIC_HOLIDAYS


def next_business_day(day):
    """The first business day strictly after day."""
    day += timedelta(days=1)
    while not is_business_day(day):
        day += timedelta(days=1)
    return day


def add_business_days(day, count):
    """Advance day by count business days (0 leaves a business day unchanged)."""
    for _ in range(count):
        day = next_business_day(day)
    return day


def effective_submission_date(rail, submitted_at):
    """The business day the submission counts as, after applying the rail's cutoff.

    Immediate rails (RTP, FedNow) have no cutoff and are not used here.
    A submission on a weekend or synthetic holiday, or at a time strictly
    after the demo cutoff on a business day, rolls to the next business day.
    A submission exactly at the cutoff is on time.
    """
    spec = _spec(rail)
    submitted = _as_et(submitted_at)
    day = submitted.date()
    cutoff = spec['cutoff_et']
    late = (not is_business_day(day)
            or (cutoff is not None and submitted.time() > cutoff))
    if late:
        day = next_business_day(day)
    return day


def expected_settlement(rail, submitted_at):
    """Timezone-aware settlement datetime in America/New_York.

    Immediate rails (RTP, FedNow) settle at the submission instant, any day
    including weekends and synthetic holidays. Other rails settle on the
    effective submission date plus the rail's business-day window, marked at
    the rail's demo cutoff (or 17:00 ET) on that date.
    """
    spec = _spec(rail)
    submitted = _as_et(submitted_at)
    if spec['immediate']:
        return submitted
    day = add_business_days(effective_submission_date(rail, submitted),
                            spec['business_days'])
    marker = spec['cutoff_et'] or _SETTLEMENT_MARKER
    return datetime.combine(day, marker, tzinfo=ET)


def return_path(rail):
    """The simulated post-send path for this rail, with a copy of any codes."""
    path = _spec(rail)['return_path']
    return {'kind': path['kind'], 'codes': list(path['codes'])}


def payment_limit(rail):
    """Demo per-payment limit as a dollar string, the same shape as payout amount."""
    cents = _spec(rail)['limit_cents']
    return format(Decimal(cents) / 100, '.2f')


def describe(rail, submitted_at):
    """The public rail_timing object: settlement, return path and demo limit."""
    return {
        'expected_settlement': expected_settlement(rail, submitted_at).isoformat(),
        'return_path': return_path(rail),
        'payment_limit': payment_limit(rail),
    }
