# START HERE — OFAC SDN screening (#111) with an optional live vendor (P2).
# Default provider is the dated local snapshot (no network). OFAC_PROVIDER=http
# POSTs the payee name to OFAC_HTTP_URL. Create, decide, and settle all call
# screen(); Clear / Block UX is unchanged. Vendor outage fails closed by
# returning a provider-error match so settle cannot skip screening.
"""Screen a payee name against a local OFAC SDN snapshot or HTTP vendor."""
import json
import os
import re
import unicodedata
import urllib.error
import urllib.request
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path

# FEATURE 01 — Name the snapshot, the match threshold and the demo payee
# The snapshot is the public OFAC SDN file, trimmed to a few dozen entries and
# their aliases. MATCH_THRESHOLD is the lowest fuzzy score that counts as a
# possible match (1.0 is exact). Names shorter than SHORT_NAME_LIMIT only match
# when the score is exact, so a short alias like "BNC" cannot flag a similar
# three-letter word. The demo payee uses an invented DEMO- account; its name is
# written to match a real public SDN entry so the Desk can show a possible match.
SNAPSHOT_PATH = Path(__file__).with_name('fixtures') / 'ofac_sdn_snapshot.json'
FLAG_CODE = 'POSSIBLE_SANCTIONS_MATCH'
MATCH_THRESHOLD = 0.85
SHORT_NAME_LIMIT = 5
DEMO_PAYEE_NAME = 'Banco Nacional de Cuba'
DEMO_PAYEE_ACCOUNT = 'DEMO-OFAC-011'
DEMO_AMOUNT_CENTS = 50000  # $500.00; under the $1,000 new-payee rule so only this flag fires

PROVIDER_ENV = 'OFAC_PROVIDER'
PROVIDER_LOCAL = 'local'
PROVIDER_HTTP = 'http'
PROVIDERS = frozenset({PROVIDER_LOCAL, PROVIDER_HTTP})
HTTP_URL_ENV = 'OFAC_HTTP_URL'
HTTP_TIMEOUT_ENV = 'OFAC_HTTP_TIMEOUT_SECONDS'
DEFAULT_HTTP_TIMEOUT_SECONDS = 5
VENDOR_UNAVAILABLE_UID = 'vendor-unavailable'


# FEATURE 02 — Load the dated snapshot once
# Reads the committed JSON file. Nothing here calls the network.
@lru_cache(maxsize=1)
def load_snapshot():
    return json.loads(SNAPSHOT_PATH.read_text())


def list_published():
    return load_snapshot()['list_publish_date']


def snapshot_taken():
    return load_snapshot()['snapshot_date']


# FEATURE 03 — Normalize a name for comparison
# Uppercase ASCII, keep letters and digits, drop punctuation, and collapse
# spaces. "Last, First" is flipped before punctuation is stripped, then also
# compared as "First Last". Reversed word order is compared too, so
# "DROUKDEL Abdelmalek" matches "Abdelmalek DROUKDEL".
def normalize(name):
    text = unicodedata.normalize('NFKD', name or '')
    text = text.encode('ascii', 'ignore').decode('ascii').upper()
    text = re.sub(r'[^A-Z0-9]+', ' ', text)
    return ' '.join(text.split())


def _variants(name):
    raw = name or ''
    variants = set()
    text = normalize(raw)
    if text:
        variants.add(text)
        parts = text.split()
        if len(parts) >= 2:
            variants.add(' '.join(reversed(parts)))
    # Flip "Last, First" before punctuation is stripped, so the comma is still visible.
    if ',' in raw:
        last, _, first = raw.partition(',')
        flipped = f'{first.strip()} {last.strip()}'.strip()
        if flipped:
            variants.add(normalize(flipped))
    return variants


def _score(left, right):
    return max(
        (SequenceMatcher(None, a, b).ratio() for a in _variants(left) for b in _variants(right)),
        default=0.0,
    )


def _enough(score, matched_name):
    if len(normalize(matched_name)) < SHORT_NAME_LIMIT:
        return score >= 1.0
    return score >= MATCH_THRESHOLD


# FEATURE 04 — Choose the screening provider
def provider_name(env=None):
    env = os.environ if env is None else env
    raw = (env.get(PROVIDER_ENV) or PROVIDER_LOCAL).strip().lower()
    if raw not in PROVIDERS:
        raise ValueError(f'{PROVIDER_ENV} must be one of: {", ".join(sorted(PROVIDERS))}.')
    return raw


# FEATURE 04b — Screen one payee name (public entry used by create/decide/settle)
# Returns the best matching entry, the name that matched (primary or alias),
# and a 0–100 score, or None when nothing is close enough. HTTP provider
# outages fail closed with a provider-error match.
def screen(name, env=None):
    if provider_name(env) == PROVIDER_HTTP:
        try:
            return screen_http(name, env=env)
        except OfacProviderError:
            return _provider_error_match(name)
    return screen_local(name)


def screen_local(name):
    if not isinstance(name, str) or not normalize(name):
        return None
    best = None
    for entry in load_snapshot()['entries']:
        candidates = [entry['name'], *entry.get('aliases', [])]
        for candidate in candidates:
            score = _score(name, candidate)
            if not _enough(score, candidate):
                continue
            if best is None or score > best['score']:
                best = {
                    'uid': entry['uid'],
                    'entry_name': entry['name'],
                    'entry_type': entry['type'],
                    'matched_name': candidate,
                    'score': score,
                    'list_published': list_published(),
                    'snapshot_taken': snapshot_taken(),
                }
            if score == 1.0:
                return _public(best)
    return None if best is None else _public(best)


class OfacProviderError(RuntimeError):
    """HTTP vendor unreachable or returned an invalid payload."""


def _http_timeout(env):
    raw = env.get(HTTP_TIMEOUT_ENV) or str(DEFAULT_HTTP_TIMEOUT_SECONDS)
    try:
        value = float(raw)
    except (TypeError, ValueError) as error:
        raise OfacProviderError(f'{HTTP_TIMEOUT_ENV} must be a number of seconds.') from error
    if value <= 0:
        raise OfacProviderError(f'{HTTP_TIMEOUT_ENV} must be positive.')
    return value


def _validate_http_match(payload):
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise OfacProviderError('OFAC HTTP vendor must return null or a JSON object.')
    required = ('uid', 'entry_name', 'entry_type', 'matched_name', 'score',
                'list_published', 'snapshot_taken')
    missing = [key for key in required if key not in payload]
    if missing:
        raise OfacProviderError(f'OFAC HTTP match missing fields: {", ".join(missing)}.')
    score = payload['score']
    if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 100:
        raise OfacProviderError('OFAC HTTP match score must be an integer 0–100.')
    for key in ('uid', 'entry_name', 'entry_type', 'matched_name',
                'list_published', 'snapshot_taken'):
        if not isinstance(payload[key], str) or not payload[key].strip():
            raise OfacProviderError(f'OFAC HTTP match {key} must be non-empty text.')
    return {key: payload[key] for key in required}


def screen_http(name, env=None):
    env = os.environ if env is None else env
    if not isinstance(name, str) or not normalize(name):
        return None
    url = (env.get(HTTP_URL_ENV) or '').strip()
    if not url:
        raise OfacProviderError(f'{HTTP_URL_ENV} is required when {PROVIDER_ENV}=http.')
    body = json.dumps({'name': name}).encode('utf-8')
    request = urllib.request.Request(
        url, data=body, method='POST',
        headers={'Content-Type': 'application/json', 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=_http_timeout(env)) as response:
            raw = response.read().decode('utf-8')
            status = getattr(response, 'status', None) or response.getcode()
    except urllib.error.HTTPError as error:
        raise OfacProviderError(f'OFAC HTTP vendor returned HTTP {error.code}.') from error
    except urllib.error.URLError as error:
        raise OfacProviderError(f'OFAC HTTP vendor unreachable: {error.reason}.') from error
    except TimeoutError as error:
        raise OfacProviderError('OFAC HTTP vendor timed out.') from error
    if status is not None and not (200 <= int(status) < 300):
        raise OfacProviderError(f'OFAC HTTP vendor returned HTTP {status}.')
    try:
        payload = json.loads(raw) if raw.strip() else None
    except ValueError as error:
        raise OfacProviderError('OFAC HTTP vendor returned invalid JSON.') from error
    return _validate_http_match(payload)


def _provider_error_match(name):
    display = name if isinstance(name, str) and name.strip() else '(unknown payee)'
    return {
        'uid': VENDOR_UNAVAILABLE_UID,
        'entry_name': 'OFAC vendor unavailable',
        'entry_type': 'provider_error',
        'matched_name': display,
        'score': 100,
        'list_published': 'unknown',
        'snapshot_taken': 'vendor-error',
    }


def _public(match):
    out = dict(match)
    out['score'] = int(round(match['score'] * 100))
    return out


# FEATURE 05 — Build the Desk flag sentence
# One sentence, no internal ". " so it can sit next to another rule's sentence.
# Names the matched list entry (and the listed primary name when an alias hit),
# the score, the list publish date, and the date the snapshot was taken.
def flag_sentence(match):
    score = match['score']
    published = match['list_published']
    taken = match['snapshot_taken']
    if match['matched_name'] != match['entry_name']:
        who = f"{match['matched_name']} (listed as {match['entry_name']})"
    else:
        who = match['entry_name']
    return (f'Possible sanctions match: {who}, score {score}, '
            f'OFAC SDN list published {published}, snapshot taken {taken}')
