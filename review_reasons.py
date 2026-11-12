# START HERE — Structured reasons a payout is held for review (#32).
# One read-only function returns every reason, as a list of {code, label, details}
# items. The Desk panel reads panel_items() (stored flag order and sentences, with
# sanctions facts from the same OFAC match for_payout uses). Routing-number
# failures are not a stored reason: the ABA check (#112)
# rejects a bad number at creation (422) and never writes it on a payout, so it
# is left out rather than invented here.
"""Return every reason a payout is held for review, as structured items."""
import sqlite3
from datetime import datetime, timedelta
from statistics import median

import ofac
import payouts
from payments import APIError

# FEATURE 01 — Name the reason codes, short labels and the amount rule
# FIRST_TIME_PAYEE is broader than the NEW_PAYEE_OVER_1000 hold rule: it applies
# whenever there is no earlier settled or cleared payout to that payee, including
# when another rule (for example sanctions) is what held a smaller payout.
# Amount vs history uses the same multiple the Desk already uses to hold a
# payout: more than payouts.USUAL_MULTIPLIER times the payee's usual payout
# (the stored average of earlier payouts). When usual is not stored, the usual
# is the median of earlier settled or cleared amounts for that payee.
IN_REVIEW = frozenset({payouts.FLAGGED, payouts.HELD})
FIRST_TIME_PAYEE = 'FIRST_TIME_PAYEE'
LABELS = {
    payouts.POSSIBLE_SANCTIONS_MATCH: 'Possible sanctions match',
    FIRST_TIME_PAYEE: 'First-time payee',
    payouts.AMOUNT_OVER_3X_AVG: payouts.FLAG_TEXT[payouts.AMOUNT_OVER_3X_AVG],
    payouts.BANK_CHANGED_7D: 'Payee bank details changed recently',
    payouts.DUPLICATE_24H: 'Possible duplicate payout',
}


def _item(code, details):
    return {'code': code, 'label': LABELS[code], 'details': details}


def _settled_or_cleared(payout):
    return (payout.get('execution_status') == payouts.SETTLED
            or payout.get('review_status') == payouts.APPROVED)


def _earlier_for_payee(payout, earlier):
    return [other for other in earlier
            if other['payout_id'] != payout['payout_id']
            and other['payee_name'] == payout['payee_name']
            and other['created_at'] < payout['created_at']]


# FEATURE 02 — Build the list of reasons from one payout and earlier rows
# Empty when the payout is not in review (not FLAGGED or HELD). Order is
# sanctions, first-time payee, amount vs history, recent bank-details change,
# then possible duplicate. ABA routing is never included (see the module
# comment). Read-only.
def reasons(payout, earlier):
    if payout['status'] not in IN_REVIEW:
        return []
    prior = _earlier_for_payee(payout, earlier)
    settled = [other for other in prior if _settled_or_cleared(other)]
    items = []

    match = ofac.screen(payout.get('payee_name'))
    flagged = payouts.split_flag_codes(payout.get('flag_code'))
    if match or payouts.POSSIBLE_SANCTIONS_MATCH in flagged:
        details = {
            'uid': match['uid'],
            'entry_name': match['entry_name'],
            'entry_type': match['entry_type'],
            'matched_name': match['matched_name'],
            'score': match['score'],
            'list_published': match['list_published'],
            'snapshot_taken': match['snapshot_taken'],
        } if match else {'flag_text': payout.get('flag_text')}
        items.append(_item(payouts.POSSIBLE_SANCTIONS_MATCH, details))

    if not settled:
        items.append(_item(FIRST_TIME_PAYEE, {'earlier_settled_or_cleared': 0}))

    usual = payout.get('payee_usual_cents')
    if usual is None and settled:
        usual = int(median(other['amount_cents'] for other in settled))
    amount = payout['amount_cents']
    if usual and amount > payouts.USUAL_MULTIPLIER * usual:
        items.append(_item(payouts.AMOUNT_OVER_3X_AVG, {
            'amount_cents': amount,
            'usual_cents': usual,
            'multiplier': payouts.USUAL_MULTIPLIER,
            'ratio': round(amount / usual),
        }))

    changed = payout.get('bank_details_changed_at')
    if changed:
        when = datetime.fromisoformat(payout['created_at'])
        changed_at = datetime.fromisoformat(changed)
        if timedelta(0) <= when - changed_at <= payouts.BANK_CHANGE_WINDOW:
            items.append(_item(payouts.BANK_CHANGED_7D, {
                'changed_at': changed,
                'days_ago': (when.date() - changed_at.date()).days,
            }))

    when = datetime.fromisoformat(payout['created_at'])
    for other in prior:
        then = datetime.fromisoformat(other['created_at'])
        if (other['amount_cents'] == amount
                and timedelta(0) < when - then <= payouts.DUPLICATE_WINDOW):
            items.append(_item(payouts.DUPLICATE_24H, {
                'other_payout_id': other['payout_id'],
                'amount_cents': amount,
            }))
            break
    return items


# FEATURE 03 — Look up one payout in a demo database
# Raises the same 404 the other payout reads use when the id is unknown.
# Uses this visitor's database only. Nothing is written.
def for_payout(database, payout_id):
    with sqlite3.connect(str(database)) as db:
        row = db.execute(
            f'SELECT {", ".join(payouts.COLUMNS)} FROM payouts WHERE payout_id = ?',
            (payout_id,)).fetchone()
        if not row:
            raise APIError(404, 'Payout not found.')
        payout = dict(zip(payouts.COLUMNS, row))
        earlier = [dict(zip(payouts.COLUMNS, other))
                   for other in db.execute(
                       f'SELECT {", ".join(payouts.COLUMNS)} FROM payouts WHERE payout_id != ?',
                       (payout_id,))]
    return reasons(payout, earlier)


# FEATURE 04 — Desk panel items from stored flags
# The Desk lists one item per stored flag code, in stored order, using that
# rule's flag sentence (with a period). for_payout is not used directly: it
# is empty for Sent and Returned payouts, and its FIRST_TIME_PAYEE code is
# not a stored flag. Sanctions facts still come from the same OFAC match.
# BANK_CHANGED_7D also carries the required phone-check line. Final payouts
# keep these original rule items.
BANK_PHONE_LINE = 'Confirm the change with the payee by phone before sending.'


def panel_items(payout):
    """One stored rule reason per item: sentence, code, optional facts and phone line."""
    codes = payouts.split_flag_codes(payout.get('flag_code'))
    texts = [part for part in (payout.get('flag_text') or '').split(payouts.FLAG_TEXT_SEP) if part]
    items = []
    for index, code in enumerate(codes):
        sentence = texts[index] if index < len(texts) else ''
        facts = None
        phone = None
        if code == payouts.POSSIBLE_SANCTIONS_MATCH:
            facts = _sanctions_fact_rows(payout, sentence)
            sentence = 'Possible sanctions match.'
        elif sentence and not sentence.endswith('.'):
            sentence += '.'
        if code == payouts.BANK_CHANGED_7D:
            phone = BANK_PHONE_LINE
        items.append({'code': code, 'sentence': sentence, 'facts': facts, 'phone': phone})
    return items


def _sanctions_fact_rows(payout, sentence=''):
    match = ofac.screen(payout.get('payee_name'))
    if match:
        score = match['score']
        score_value = (f'{score} (100 is an exact match)' if score == 100 else str(score))
        return (
            ('Listed name', match['entry_name']),
            ('Score', score_value),
            ('List published', match['list_published']),
            ('Snapshot taken', match['snapshot_taken']),
        )
    prefix = 'Possible sanctions match: '
    mid_score = ', score '
    mid_published = ', OFAC SDN list published '
    mid_taken = ', snapshot taken '
    if not (sentence.startswith(prefix) and mid_score in sentence
            and mid_published in sentence and mid_taken in sentence):
        return None
    rest = sentence[len(prefix):]
    name, rest = rest.split(mid_score, 1)
    score, rest = rest.split(mid_published, 1)
    published, taken = rest.split(mid_taken, 1)
    score = score.strip()
    score_value = f'{score} ({score} is an exact match)' if score == '100' else score
    return (
        ('Listed name', name.strip()),
        ('Score', score_value),
        ('List published', published.strip()),
        ('Snapshot taken', taken.strip()),
    )
