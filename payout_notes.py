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
SOURCE_RULES = 'rules'
CLEAN_NOTE = 'No rules triggered. Within normal range for this payee.'
BANK_CHANGED_NOTE = (
    'Rule BANK_CHANGED_7D: bank details changed within 7 days. '
    'Confirm the change with the payee by phone before sending.')
REVIEW_BEFORE_SENDING = 'Review before sending.'


# FEATURE 02 — Build one rules note from a payout's stored flag
# Same payout facts always produce the same (text, source, fallback) triple.
# Source is always "rules" and fallback is always False: these are the primary
# stored rules notes, not an AI-unavailable stand-in. Unknown or missing flags
# get the clean line. When more than one rule is stored (comma-separated codes,
# issue #89), the note lists every matching template, still as a rules note.
# No clock and no I/O.
def _note_for(code, flag):
    if code == AMOUNT_OVER_3X_AVG:
        return f'Rule {AMOUNT_OVER_3X_AVG}: {flag}. {REVIEW_BEFORE_SENDING}'
    if code == BANK_CHANGED_7D:
        return BANK_CHANGED_NOTE
    if code == DUPLICATE_24H:
        return f'Rule {DUPLICATE_24H}: {flag}. {REVIEW_BEFORE_SENDING}'
    if code == NEW_PAYEE_OVER_1000:
        return f'Rule {NEW_PAYEE_OVER_1000}: {flag}. {REVIEW_BEFORE_SENDING}'
    return None


def rules_note(payout):
    codes = [part for part in (payout.get('flag_code') or '').split(',') if part]
    flag = payout.get('flag_text') or ''
    if not codes:
        text = CLEAN_NOTE
    elif len(codes) == 1:
        text = _note_for(codes[0], flag) or CLEAN_NOTE
    else:
        parts = flag.split('. ') if flag else [''] * len(codes)
        notes = []
        for i, code in enumerate(codes):
            piece = parts[i] if i < len(parts) else ''
            note = _note_for(code, piece)
            if note:
                notes.append(note)
        text = ' '.join(notes) if notes else CLEAN_NOTE
    return text, SOURCE_RULES, False


# FEATURE 03 — Write the note fields onto a payout dict
# Used when building the seed and when filling empty note columns on an older demo.
def apply(payout):
    payout['note_text'], payout['note_source'], payout['note_fallback'] = rules_note(payout)
    return payout
