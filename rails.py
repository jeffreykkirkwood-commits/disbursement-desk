# START HERE — Simulated payout-rail timing, cutoffs, per-payment limits and
# return paths. Pure functions over the rail and a submission time; nothing is
# stored and no network is called. Desk HTML is unchanged: callers that already
# serialize a payout (get_payout, list_payouts, current_queue) attach a
# rail_timing object computed here.
"""Simulated settlement windows, ET cutoffs, labelled limits and return paths per payout rail."""
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')

# FEATURE 01 — Federal Reserve holiday calendar for 2026 and 2027
# Hard-coded observed dates from the Board's K.8 schedule
# (https://www.federalreserve.gov/aboutthefed/k8.htm, last update 8 July 2026).
# Fed rule: a Saturday holiday is not moved to Friday — Reserve Banks stay
# open that Friday (the Board of Governors may close; this list follows the
# Banks, which is what ACH settlement uses). A Sunday holiday is observed
# the following Monday. Saturday and Sunday calendar dates are included for
# completeness; weekdays already skip weekends.
FED_HOLIDAYS = frozenset({
    # 2026
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # Birthday of Martin Luther King, Jr.
    date(2026, 2, 16),   # Washington's Birthday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth National Independence Day
    date(2026, 7, 4),    # Independence Day (Saturday; Banks open Friday 3 July)
    date(2026, 9, 7),    # Labor Day
    date(2026, 10, 12),  # Columbus Day
    date(2026, 11, 11),  # Veterans Day
    date(2026, 11, 26),  # Thanksgiving Day
    date(2026, 12, 25),  # Christmas Day
    # 2027
    date(2027, 1, 1),    # New Year's Day
    date(2027, 1, 18),   # Birthday of Martin Luther King, Jr.
    date(2027, 2, 15),   # Washington's Birthday
    date(2027, 5, 31),   # Memorial Day
    date(2027, 6, 19),   # Juneteenth (Saturday; Banks open Friday 18 June)
    date(2027, 7, 4),    # Independence Day (Sunday)
    date(2027, 7, 5),    # Independence Day observed (Monday)
    date(2027, 9, 6),    # Labor Day
    date(2027, 10, 11),  # Columbus Day
    date(2027, 11, 11),  # Veterans Day
    date(2027, 11, 25),  # Thanksgiving Day
    date(2027, 12, 25),  # Christmas Day (Saturday; Banks open Friday 24 December)
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


# FEATURE 03 — Rail table
# Limits and cutoff labels follow Jeffrey's #32 decision
# (https://github.com/jeffreykkirkwood-commits/payments-api-simulator/issues/32#issuecomment-6834824232).
# limit_source is 'network' for a published network cap, or 'bank_policy'
# for a bank-set figure that is not a network cap. Demo cutoff times are
# unchanged; ACH, SAME_DAY_ACH and WIRE cutoffs are labelled as bank
# cutoffs set ahead of the Fed deadlines, and the Fed times are listed
# in fed_deadlines when they were specified.
#
# SAME_DAY_ACH's $1,000,000 limit is Nacha's published Same Day ACH
# per-payment cap through 16 September 2027 (Nacha has announced a
# $10 million cap from 17 September 2027). The 14:45 ET bank cutoff is
# unchanged; it sits at the Fed's second Same Day ACH window and ahead
# of the final 16:45 ET Fed window. Fed windows: 10:30, 14:45, 16:45 ET.
#
# RTP and FedNow stay separate 24/7 rows with no cutoff (Jeffrey, #32,
# comment 6832389969). Both use the $10,000,000 network maximum.
# FedNow's default per-payment limit is $100,000; participants may raise
# it up to that network maximum. That default is a limit_note, not the
# payment_limit used for over_limit. FedNow's earlier network caps
# ($500,000, then $1,000,000) are not used here.
#
# Next-day ACH's $1,000,000 and wire's $5,000,000 limits are bank policy,
# not network caps. ACH's 17:00 ET and wire's 16:00 ET cutoffs are bank
# cutoffs set ahead of Fed deadlines. The Fedwire Funds Service customer
# cutoff is 18:45 ET. No next-day ACH Fed deadline is listed, because
# none was specified in the decision.
RAIL_SPECS = {
    'ACH': {
        'cutoff_et': time(17, 0),
        'business_days': 1,
        'immediate': False,
        'limit_cents': 100_000_000,
        'limit_source': 'bank_policy',
        'limit_note': None,
        'cutoff_source': 'bank',
        'fed_deadlines': (),
        'cutoff_note': 'Bank cutoff set ahead of Fed ACH deadlines.',
        'return_path': RETURN_ACH,
    },
    'SAME_DAY_ACH': {
        'cutoff_et': time(14, 45),
        'business_days': 0,
        'immediate': False,
        'limit_cents': 100_000_000,
        'limit_source': 'network',
        'limit_note': None,
        'cutoff_source': 'bank',
        'fed_deadlines': (time(10, 30), time(14, 45), time(16, 45)),
        'cutoff_note': (
            'Bank cutoff set ahead of the final Fed Same Day ACH window. '
            'Fed windows are 10:30 AM, 2:45 PM, and 4:45 PM ET.'
        ),
        'return_path': RETURN_ACH,
    },
    'RTP': {
        'cutoff_et': None,
        'business_days': 0,
        'immediate': True,
        'limit_cents': 1_000_000_000,
        'limit_source': 'network',
        'limit_note': None,
        'cutoff_source': None,
        'fed_deadlines': (),
        'cutoff_note': None,
        'return_path': RETURN_REQUEST_FOR_RETURN,
    },
    'FEDNOW': {
        'cutoff_et': None,
        'business_days': 0,
        'immediate': True,
        'limit_cents': 1_000_000_000,
        'limit_source': 'network',
        'limit_note': (
            'Network maximum. The FedNow default per-payment limit is '
            '$100,000; participants may raise it up to this maximum.'
        ),
        'cutoff_source': None,
        'fed_deadlines': (),
        'cutoff_note': None,
        'return_path': RETURN_REQUEST_FOR_RETURN,
    },
    'WIRE': {
        'cutoff_et': time(16, 0),
        'business_days': 0,
        'immediate': False,
        'limit_cents': 500_000_000,
        'limit_source': 'bank_policy',
        'limit_note': None,
        'cutoff_source': 'bank',
        'fed_deadlines': (time(18, 45),),
        'cutoff_note': (
            'Bank cutoff set ahead of the Fedwire Funds Service customer '
            'cutoff (6:45 PM ET).'
        ),
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
    """True when day is a weekday that is not a Federal Reserve Bank holiday."""
    return day.weekday() < 5 and day not in FED_HOLIDAYS


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
    A submission on a weekend or Federal Reserve holiday, or at a time strictly
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
    including weekends and Federal Reserve holidays. Other rails settle on the
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
    """Per-payment limit as a dollar string, the same shape as payout amount."""
    cents = _spec(rail)['limit_cents']
    return format(Decimal(cents) / 100, '.2f')


def describe(rail, submitted_at, amount):
    """The public rail_timing object: settlement, return path and payment limit.

    payment_limit is the rail's labelled cap. limit_source is 'network' or
    'bank_policy'. over_limit is True only when amount is strictly greater
    than that figure. Limits are labelled, not enforced. FedNow's limit_note
    records the $100,000 default; over_limit still uses the $10,000,000
    network maximum.
    """
    spec = _spec(rail)
    limit = payment_limit(rail)
    return {
        'expected_settlement': expected_settlement(rail, submitted_at).isoformat(),
        'return_path': return_path(rail),
        'payment_limit': limit,
        'limit_source': spec['limit_source'],
        'limit_note': spec['limit_note'],
        'over_limit': Decimal(amount) > Decimal(limit),
        'cutoff_source': spec['cutoff_source'],
        'fed_deadlines': [deadline.strftime('%H:%M') for deadline in spec['fed_deadlines']],
        'cutoff_note': spec['cutoff_note'],
    }
