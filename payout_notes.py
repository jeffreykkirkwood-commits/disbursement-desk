# START HERE — Rules-based notes stored on each payout (issue #37), with an
# optional paraphrase behind DESK_AI_NOTES (#38). rules_note() is always the
# baseline: a fixed sentence from the stored flags, no network. apply() may
# paraphrase those structured reasons when the flag and API key are set. Any
# failure keeps the rules text and sets note_fallback. Seed and backfill pass
# allow_ai=False so demos stay deterministic.
"""Rules notes for payouts, with an optional flagged AI paraphrase."""
import json
import os
import urllib.error
import urllib.request

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
SOURCE_AI = 'ai'
CLEAN_NOTE = 'No rules triggered. Within normal range for this payee.'
AI_NOTES_ENV = 'DESK_AI_NOTES'
AI_API_KEY_ENV = 'DESK_AI_API_KEY'
AI_API_URL_ENV = 'DESK_AI_API_URL'
AI_TIMEOUT_ENV = 'DESK_AI_TIMEOUT_SECONDS'
AI_ENABLED_VALUE = '1'
DEFAULT_AI_TIMEOUT_SECONDS = 5
KNOWN_FLAG_CODES = frozenset({
    AMOUNT_OVER_3X_AVG,
    BANK_CHANGED_7D,
    DUPLICATE_24H,
    NEW_PAYEE_OVER_1000,
    POSSIBLE_SANCTIONS_MATCH,
})
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
# Seed and backfill pass allow_ai=False. Runtime routing may pass allow_ai=True.
def apply(payout, allow_ai=False, provider=None, env=None):
    rules_text, source, fallback = rules_note(payout)
    if ai_enabled(env, allow_ai=allow_ai):
        rules_text, source, fallback = _paraphrase_or_rules(
            payout, rules_text, provider, env)
    payout['note_text'] = rules_text
    payout['note_source'] = source
    payout['note_fallback'] = fallback
    return payout


# FEATURE 04 — Optional AI paraphrase of structured stored reasons only
# On when DESK_AI_NOTES=1 and DESK_AI_API_KEY is non-empty. The provider sees
# code+label pairs from stored flags, never free-form prose to invent from.
# Invent-guard: every known flag code in the returned text must already be on
# the payout. Any exception, empty text, or invented code keeps the rules note.
class AiNotesError(RuntimeError):
    """AI paraphrase failed or returned an invalid payload."""


def ai_enabled(env=None, allow_ai=True):
    if not allow_ai:
        return False
    env = os.environ if env is None else env
    flag = (env.get(AI_NOTES_ENV) or '').strip()
    key = (env.get(AI_API_KEY_ENV) or '').strip()
    return flag == AI_ENABLED_VALUE and bool(key)


def _reason_items(payout):
    import review_reasons
    return [{'code': item['code'], 'label': item['label']}
            for item in review_reasons.stored_reasons(payout)]


def _invents(text, stored_codes):
    allowed = {code.upper() for code in stored_codes}
    folded = (text or '').upper()
    mentioned = {code for code in KNOWN_FLAG_CODES if code in folded}
    return not mentioned <= allowed


def _paraphrase_or_rules(payout, rules_text, provider, env):
    reasons = _reason_items(payout)
    if not reasons:
        return rules_text, SOURCE_RULES, False
    try:
        stored_codes = [item['code'] for item in reasons]
        if provider is not None:
            text = provider(reasons)
        else:
            text = paraphrase_http(reasons, env)
        if not isinstance(text, str) or not text.strip():
            raise AiNotesError('AI paraphrase was empty.')
        text = text.strip()
        if _invents(text, stored_codes):
            raise AiNotesError('AI paraphrase invented a flag code.')
    except Exception:
        return rules_text, SOURCE_RULES, True
    return text, SOURCE_AI, False


def _timeout(env):
    raw = env.get(AI_TIMEOUT_ENV) or str(DEFAULT_AI_TIMEOUT_SECONDS)
    try:
        value = float(raw)
    except (TypeError, ValueError) as error:
        raise AiNotesError(f'{AI_TIMEOUT_ENV} must be a number of seconds.') from error
    if value <= 0:
        raise AiNotesError(f'{AI_TIMEOUT_ENV} must be positive.')
    return value


def _parse_paraphrase(raw):
    try:
        payload = json.loads(raw) if raw and str(raw).strip() else None
    except ValueError as error:
        raise AiNotesError('AI paraphrase returned invalid JSON.') from error
    if isinstance(payload, str):
        text = payload.strip()
    elif isinstance(payload, dict):
        value = payload.get('text')
        if value is None:
            value = payload.get('note')
        if not isinstance(value, str):
            raise AiNotesError('AI paraphrase JSON must include text.')
        text = value.strip()
    else:
        raise AiNotesError('AI paraphrase must be a JSON string or object.')
    if not text:
        raise AiNotesError('AI paraphrase was empty.')
    return text


def paraphrase_http(reasons, env=None):
    env = os.environ if env is None else env
    url = (env.get(AI_API_URL_ENV) or '').strip()
    key = (env.get(AI_API_KEY_ENV) or '').strip()
    if not url:
        raise AiNotesError(f'{AI_API_URL_ENV} is required when AI notes are on.')
    if not key:
        raise AiNotesError(f'{AI_API_KEY_ENV} is required when AI notes are on.')
    payload = [{'code': item['code'], 'label': item['label']} for item in reasons]
    body = json.dumps({'reasons': payload}).encode('utf-8')
    request = urllib.request.Request(
        url, data=body, method='POST',
        headers={
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'Authorization': f'Bearer {key}',
        })
    try:
        with urllib.request.urlopen(request, timeout=_timeout(env)) as response:
            raw = response.read().decode('utf-8')
            status = getattr(response, 'status', None) or response.getcode()
    except urllib.error.HTTPError as error:
        raise AiNotesError(f'AI paraphrase returned HTTP {error.code}.') from error
    except urllib.error.URLError as error:
        raise AiNotesError(f'AI paraphrase unreachable: {error.reason}.') from error
    except TimeoutError as error:
        raise AiNotesError('AI paraphrase timed out.') from error
    if status is not None and not (200 <= int(status) < 300):
        raise AiNotesError(f'AI paraphrase returned HTTP {status}.')
    return _parse_paraphrase(raw)
