# START HERE — Structured reasons a payout is held for review (#32).
# stored_reasons() lists every stored flag, in stored order, with the Desk's
# codes and the exact per-payout sentences in flag_text, and attaches details
# to each. for_payout() / reasons() return that list only when the payout is
# still in review (FLAGGED or HELD). narrative_for_payout() joins those stored
# labels into one paragraph; it is empty whenever reasons() is empty. The Desk
# panel reads panel_items(), which is the same stored list (including on final
# payouts) plus the phone line and sanctions fact rows.
# Routing-number failures are not a stored reason: the ABA check (#112)
# rejects a bad number at creation (422) and never writes it on a payout, so it
# is left out rather than invented here. FIRST_TIME_PAYEE is not a stored flag
# and is not returned. Details are taken from stored payout fields and the
# stored #114 flag sentence; ofac.screen() is not run again at read time.
"""Return every reason a payout is held for review, as structured items."""
from datetime import datetime, timedelta

import api_contract
import payouts
import storage
from desk_common import APIError

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
    amount = payout.get('amount_cents')
    if amount is None:
        return {}
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
    amount = payout.get('amount_cents')
    if amount is None:
        return {}
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
            raise APIError(404, 'Payout not found.', code=api_contract.NOT_FOUND)
        payout = dict(zip(payouts.COLUMNS, row))
        earlier = [dict(zip(payouts.COLUMNS, other))
                   for other in db.execute(
                       f'SELECT {", ".join(payouts.COLUMNS)} FROM payouts WHERE payout_id != ?',
                       (payout_id,)).fetchall()]
    return reasons(payout, earlier)


# FEATURE 04 — Desk panel items from the same stored list
# panel_items() is stored_reasons() plus the panel's presentation: a period
# on each sentence, the BANK_CHANGED_7D phone-check line, and sanctions fact
# rows from the stored #114 details. for_payout is not used directly because
# it is empty for Sent and Returned payouts; final payouts still show these
# original rule items.
BANK_PHONE_LINE = 'Confirm the change with the payee by phone before sending.'


def panel_items(payout, earlier=()):
    """One stored rule reason per item: sentence, code, optional facts and phone line."""
    items = []
    for item in stored_reasons(payout, earlier):
        code = item['code']
        sentence = item['label']
        facts = None
        phone = None
        if code == payouts.POSSIBLE_SANCTIONS_MATCH:
            facts = _sanctions_fact_rows(item['details'])
            sentence = 'Possible sanctions match.'
        elif sentence and not sentence.endswith('.'):
            sentence += '.'
        if code == payouts.BANK_CHANGED_7D:
            phone = BANK_PHONE_LINE
        items.append({'code': code, 'sentence': sentence, 'facts': facts, 'phone': phone})
    return items


def _sanctions_fact_rows(details):
    if 'entry_name' not in details:
        return None
    score = details['score']
    exact = score == 100 or str(score) == '100'
    score_value = f'{score} (100 is an exact match)' if exact else str(score)
    return (
        ('Listed name', details['entry_name']),
        ('Score', score_value),
        ('List published', details['list_published']),
        ('Snapshot taken', details['snapshot_taken']),
    )


# FEATURE 05 — Operator narrative from stored hold reasons only
# One paragraph for GET /payouts/<id>/review-reasons. Joins the same labels
# reasons() already returns, with FLAG_TEXT_SEP, so the JSON `narrative` cannot
# diverge from `reasons`. Empty when the payout is not in review. No
# ofac.screen(), no FIRST_TIME_PAYEE, no ABA, and no FLAG_TEXT sentence for a
# code that is not on the payout. A stored code with a missing sentence still
# uses the existing stored_reasons() fallback for that code only.
def narrative_from_stored_reasons(items):
    """Join stored reason labels into one paragraph; empty when there are no items."""
    parts = []
    for item in items:
        label = (item.get('label') or '').strip()
        if label:
            parts.append(label)
    if not parts:
        return ''
    return payouts.FLAG_TEXT_SEP.join(parts)


def narrative_for_payout(payout, earlier=()):
    """One held-for-review paragraph from stored flags only; empty when not in review."""
    return narrative_from_stored_reasons(reasons(payout, earlier))

