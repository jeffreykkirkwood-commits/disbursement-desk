# START HERE — OFAC SDN screening against a dated local snapshot (#111).
# No network, no API key, and no paid dependencies. A payee is screened at
# creation and again before a payout is sent. A possible name or alias match
# holds the payout on the Desk for a person to clear or block. The snapshot is
# a trimmed public subset, not the complete list; the date and source are stored
# with the file and shown on the flag.
"""Screen a payee name against a dated local OFAC SDN snapshot."""
import json
import re
import unicodedata
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


# FEATURE 04 — Screen one payee name
# Returns the best matching entry, the name that matched (primary or alias),
# and a 0–100 score, or None when nothing is close enough. Read-only.
def screen(name):
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
