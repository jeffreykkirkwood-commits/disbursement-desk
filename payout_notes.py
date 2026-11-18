# START HERE — Rules-based notes stored on each payout (issue #37).
# A fixed sentence is worked out from the rule that fired (or a short clean-payout
# line) and saved on the payout row. Nothing here calls a network, a model, or a
# paid API. The Desk reads these stored notes and shows them as a rules note.
"""Deterministic rules-based payout notes. No network and no AI."""

# FEATURE 01 — Name the clean-payout sentence and the rule templates
# Wording matches the approved #40 mock's rules notes. Amount, duplicate and new-payee
# notes include the stored flag sentence (so Harbor's 4× amount is not Acme's 6×) and
# the same "Review before sending." close the mock uses for those fallbacks.
# Bank-change notes use the Bluebird Bakery sentence word for word. Harbor starts
# Held because of its amount flag, so it gets the amount template, not an
# operator-hold sentence. Clean payouts, including the three current-queue rows
# the mock did not list (PO-1036, PO-1039, PO-1045), use Cedar's line.
AMOUNT_OVER_3X_AVG = 'AMOUNT_OVER_3X_AVG'
BANK_CHANGED_7D = 'BANK_CHANGED_7D'
DUPLICATE_24H = 'DUPLICATE_24H'
NEW_PAYEE_OVER_1000 = 'NEW_PAYEE_OVER_1000'
POSSIBLE_SANCTIONS_MATCH = 'POSSIBLE_SANCTIONS_MATCH'
SOURCE_RULES = 'rules'
CLEAN_NOTE = 'No rules triggered. Within normal range for this payee.'
_BANK_FACT = 'bank details changed within 7 days'
_PHONE_CONFIRM = 'Confirm the change with the payee by phone before sending.'
BANK_CHANGED_NOTE = f'Rule {BANK_CHANGED_7D}: {_BANK_FACT}. {_PHONE_CONFIRM}'
REVIEW_BEFORE_SENDING = 'Review before sending.'
_REVIEW_CODES = (
    AMOUNT_OVER_3X_AVG,
    DUPLICATE_24H,
    NEW_PAYEE_OVER_1000,
    POSSIBLE_SANCTIONS_MATCH,
)
_FLAG_TEXT_SEP = '. '


# FEATURE 02 — Build one rules note from a payout's stored flag
def _single_note(code, flag):
    if code == BANK_CHANGED_7D:
        return BANK_CHANGED_NOTE
    if code in _REVIEW_CODES:
        return f'Rule {code}: {flag}. {REVIEW_BEFORE_SENDING}'
    return None


def _flag_pieces(flag_text, codes):
    parts = [part for part in (flag_text or '').split(_FLAG_TEXT_SEP) if part]
    if len(parts) <= len(codes):
        return parts
    extra = len(parts) - len(codes)
    try:
        index = codes.index(POSSIBLE_SANCTIONS_MATCH)
    except ValueError:
        index = len(codes) - 1
    return (
        parts[:index]
        + [_FLAG_TEXT_SEP.join(parts[index:index + extra + 1])]
        + parts[index + extra + 1:]
    )


def _story(codes, flag):
    pieces = _flag_pieces(flag, codes)
    known = []
    for index, code in enumerate(codes):
        if code == BANK_CHANGED_7D or code in _REVIEW_CODES:
            piece = pieces[index] if index < len(pieces) else ''
            known.append((code, piece))
    if not known:
        return CLEAN_NOTE
    if len(known) == 1:
        code, piece = known[0]
        return _single_note(code, piece)
    clauses = []
    specific = []
    review = False
    for code, piece in known:
        if code == BANK_CHANGED_7D:
            clauses.append(f'Rule {code}: {piece or _BANK_FACT}')
            if _PHONE_CONFIRM not in specific:
                specific.append(_PHONE_CONFIRM)
        else:
            clauses.append(f'Rule {code}: {piece}')
            review = True
    tail = specific + ([REVIEW_BEFORE_SENDING] if review else [])
    return '. '.join(clauses) + '. ' + ' '.join(tail)


def rules_note(payout):
    codes = [part for part in (payout.get('flag_code') or '').split(',') if part]
    flag = payout.get('flag_text') or ''
    if not codes:
        text = CLEAN_NOTE
    elif len(codes) == 1:
        text = _single_note(codes[0], flag) or CLEAN_NOTE
    else:
        text = _story(codes, flag)
    return text, SOURCE_RULES, False


# FEATURE 03 — Write the note fields onto a payout dict
# Used when building the seed and when filling empty note columns on an older demo.
def apply(payout):
    payout['note_text'], payout['note_source'], payout['note_fallback'] = rules_note(payout)
    return payout
