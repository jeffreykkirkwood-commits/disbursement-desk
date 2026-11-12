# START HERE — Structured reasons a payout is held for review (#32).
# stored_reasons() lists every stored flag, in stored order, with the Desk's
# codes and the exact per-payout sentences in flag_text, and attaches details
# to each. for_payout() / reasons() return that list only when the payout is
# still in review (FLAGGED or HELD). This module does not change Desk HTML.
# Routing-number failures are not a stored reason: the ABA check (#112)
# rejects a bad number at creation (422) and never writes it on a payout, so it
# is left out rather than invented here. FIRST_TIME_PAYEE is not a stored flag
# and is not returned. Details are taken from stored payout fields and the
# stored #114 flag sentence; ofac.screen() is not run again at read time.
"""Return every reason a payout is held for review, as structured items."""
from datetime import datetime, timedelta

import payouts
import storage
from payments import APIError

# FEATURE 01 — In-review statuses and the stored-flag list
# The reason list is the payout's stored flag_code values, in stored order.
# Labels are the matching flag_text sentences. Details are attached to those
# stored reasons only: they never add a reason the Desk did not hold.
IN_REVIEW = frozenset({payouts.FLAGGED, payouts.HELD})


def stored_reasons(payout, earlier=()):
    """One item per stored flag, in stored order, with Desk wording and details."""
    codes = payouts.split_flag_codes(payout.get('flag_code'))
    texts = _flag_sentences(payout.get('flag_text') or '', codes)
    items = []
    for index, code in enumerate(codes):
        sentence = texts[index] if index < len(texts) else ''
        label = sentence or payouts.FLAG_TEXT.get(code, code)
        items.append({
            'code': code,
            'label': label,
            'details': _details_for(code, payout, label, earlier),
        })
    return items


def _flag_sentences(flag_text, codes):
    """Split flag_text into one sentence per stored code, in stored order.

    Sentences are joined with FLAG_TEXT_SEP ('. '). A sanctions listed name
    can contain that same separator (for example 'MSP KALA NAFT CO. TEHRAN'),
    so a plain split can yield more pieces than codes. Extra pieces are
    joined back into the sanctions sentence (or the last sentence if there
    is no sanctions flag). The stored flag_text is not rewritten.
    """
    parts = [part for part in flag_text.split(payouts.FLAG_TEXT_SEP) if part]
    if not codes or len(parts) <= len(codes):
        return parts
    extra = len(parts) - len(codes)
    try:
        index = codes.index(payouts.POSSIBLE_SANCTIONS_MATCH)
    except ValueError:
        index = len(codes) - 1
    return (
        parts[:index]
        + [payouts.FLAG_TEXT_SEP.join(parts[index:index + extra + 1])]
        + parts[index + extra + 1:]
    )


def _details_for(code, payout, sentence, earlier):
    if code == payouts.POSSIBLE_SANCTIONS_MATCH:
        return _sanctions_details_from_flag_text(sentence)
    if code == payouts.AMOUNT_OVER_3X_AVG:
        return _amount_details(payout)
    if code == payouts.BANK_CHANGED_7D:
        return _bank_details(payout)
    if code == payouts.DUPLICATE_24H:
        return _duplicate_details(payout, earlier)
    return {}


def _sanctions_details_from_flag_text(sentence):
    prefix = 'Possible sanctions match: '
    mid_score = ', score '
    mid_published = ', OFAC SDN list published '
    mid_taken = ', snapshot taken '
    if not (sentence.startswith(prefix) and mid_score in sentence
            and mid_published in sentence and mid_taken in sentence):
        return {'flag_text': sentence}
    rest = sentence[len(prefix):]
    who, rest = rest.split(mid_score, 1)
    score, rest = rest.split(mid_published, 1)
    published, taken = rest.split(mid_taken, 1)
    who = who.strip()
    listed_as = ' (listed as '
    if listed_as in who and who.endswith(')'):
        matched_name, entry = who.split(listed_as, 1)
        entry_name = entry[:-1]
    else:
        matched_name = entry_name = who
    try:
        score_value = int(score.strip())
    except ValueError:
        score_value = score.strip()
    return {
        'entry_name': entry_name,
        'matched_name': matched_name,
        'score': score_value,
        'list_published': published.strip(),
        'snapshot_taken': taken.strip(),
    }


def _amount_details(payout):
    amount = payout['amount_cents']
    details = {'amount_cents': amount}
    usual = payout.get('payee_usual_cents')
    if usual:
        details['usual_cents'] = usual
        details['multiplier'] = payouts.USUAL_MULTIPLIER
        details['ratio'] = round(amount / usual)
    return details


def _bank_details(payout):
    details = {}
    changed = payout.get('bank_details_changed_at')
    if not changed or not payout.get('created_at'):
        return details
    when = datetime.fromisoformat(payout['created_at'])
    changed_at = datetime.fromisoformat(changed)
    details['changed_at'] = changed
    details['days_ago'] = (when.date() - changed_at.date()).days
    return details


def _duplicate_details(payout, earlier):
    amount = payout['amount_cents']
    details = {'amount_cents': amount}
    if not payout.get('created_at'):
        return details
    when = datetime.fromisoformat(payout['created_at'])
    for other in earlier:
        if (other['payout_id'] == payout['payout_id']
                or other['payee_name'] != payout['payee_name']
                or other['amount_cents'] != amount
                or not other.get('created_at')):
            continue
        then = datetime.fromisoformat(other['created_at'])
        if timedelta(0) < when - then <= payouts.DUPLICATE_WINDOW:
            details['other_payout_id'] = other['payout_id']
            break
    return details


# FEATURE 02 — API list: stored flags, only while the payout is in review
# Empty when the payout is not in review (not FLAGGED or HELD). ABA routing
# is never included (see the module comment). Read-only.
def reasons(payout, earlier=()):
    if payout['status'] not in IN_REVIEW:
        return []
    return stored_reasons(payout, earlier)


# FEATURE 03 — Look up one payout in a demo database
# Raises the same 404 the other payout reads use when the id is unknown.
# Uses this visitor's database only, through storage.connect (the same
# factory as the rest of the app, so a later Turso host keeps working).
# The earlier-payout query uses fetchall() so it stays safe on the libsql
# adapter, whose cursor is not iterable. Nothing is written.
def for_payout(database, payout_id):
    with storage.connect(str(database)) as db:
        row = db.execute(
            f'SELECT {", ".join(payouts.COLUMNS)} FROM payouts WHERE payout_id = ?',
            (payout_id,)).fetchone()
        if not row:
            raise APIError(404, 'Payout not found.')
        payout = dict(zip(payouts.COLUMNS, row))
        earlier = [dict(zip(payouts.COLUMNS, other))
                   for other in db.execute(
                       f'SELECT {", ".join(payouts.COLUMNS)} FROM payouts WHERE payout_id != ?',
                       (payout_id,)).fetchall()]
    return reasons(payout, earlier)
