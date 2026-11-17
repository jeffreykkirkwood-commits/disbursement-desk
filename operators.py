# START HERE — Authenticated Desk operators (P1).
# Cookie possession still selects the visitor demo database. The operator principal
# for that demo is minted server-side into operator.json and is the only actor
# written into payout_events. Clients must not send a free-text reviewer name.
"""Session-bound Desk operators with reviewer vs admin roles."""
from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

ROLE_REVIEWER = 'reviewer'
ROLE_ADMIN = 'admin'
ROLES = frozenset({ROLE_REVIEWER, ROLE_ADMIN})
OPERATOR_FILE = 'operator.json'
ENV_ROLE = 'DESK_OPERATOR_ROLE'
ENV_NAME = 'DESK_OPERATOR_NAME'
ENV_TOKENS = 'DESK_OPERATOR_TOKENS'
# Demo sandboxes default to admin so Reset demo stays usable for the visitor who
# owns that demo. Production token maps assign reviewer or admin explicitly.
DEFAULT_ROLE = ROLE_ADMIN
DEFAULT_NAME = 'Desk operator'
ID_PATTERN = re.compile(r'^[a-z0-9][a-z0-9._-]{1,63}$')
# Demo operator ids stay on ID_PATTERN. An OIDC sub may be 256 characters and
# is not slugged. The audit actor is "{display_name} <{operator_id}>".
MAX_NAME_LENGTH = 80
MAX_SUBJECT_LENGTH = 256
MAX_ACTOR_LENGTH = MAX_NAME_LENGTH + MAX_SUBJECT_LENGTH + 3


@dataclass(frozen=True)
class Operator:
    """One signed-in Desk principal. actor is what decide() stores in the audit log."""

    operator_id: str
    display_name: str
    role: str

    @property
    def actor(self):
        return f'{self.display_name} <{self.operator_id}>'

    def can_decide(self):
        return self.role in ROLES

    def can_reset(self):
        return self.role == ROLE_ADMIN

    def public(self):
        return {
            'operator_id': self.operator_id,
            'display_name': self.display_name,
            'role': self.role,
            'actor': self.actor,
        }


def _clean_role(role):
    value = (role or DEFAULT_ROLE).strip().lower()
    if value not in ROLES:
        raise ValueError(f'Operator role must be one of: {", ".join(sorted(ROLES))}.')
    return value


def _clean_name(name):
    value = (name or DEFAULT_NAME).strip()
    if not value or len(value) > MAX_NAME_LENGTH:
        raise ValueError('Operator display name must be 1–80 characters.')
    return value


def _clean_id(operator_id):
    value = (operator_id or '').strip().lower()
    if not ID_PATTERN.fullmatch(value):
        raise ValueError('Operator id must be 2–64 chars: letters, digits, . _ -')
    return value


def from_mapping(data):
    if not isinstance(data, dict):
        raise ValueError('Operator record must be an object.')
    return Operator(
        operator_id=_clean_id(data.get('operator_id') or data.get('id')),
        display_name=_clean_name(data.get('display_name') or data.get('name')),
        role=_clean_role(data.get('role')),
    )


def default_operator(demo_id, env=None):
    """Mint a principal for a new demo. Role/name come from env at create time only."""
    env = os.environ if env is None else env
    suffix = (demo_id or secrets.token_hex(4))[:12]
    return Operator(
        operator_id=f'op-{suffix}',
        display_name=_clean_name(env.get(ENV_NAME)),
        role=_clean_role(env.get(ENV_ROLE)),
    )


def load(folder):
    path = Path(folder) / OPERATOR_FILE
    if not path.is_file():
        return None
    try:
        return from_mapping(json.loads(path.read_text(encoding='utf-8')))
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def save(folder, operator):
    path = Path(folder) / OPERATOR_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(operator.public(), sort_keys=True, indent=2) + '\n',
                    encoding='utf-8')
    return operator


def ensure_for_demo(folder, demo_id, env=None):
    """Load the demo's operator, or mint and persist one on first use."""
    existing = load(folder)
    if existing is not None:
        return existing
    return save(folder, default_operator(demo_id, env=env))


def parse_bearer(authorization):
    if not isinstance(authorization, str):
        return None
    parts = authorization.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != 'bearer' or not parts[1].strip():
        return None
    return parts[1].strip()


def tokens_from_env(env=None):
    """Optional production-shaped token map: {token: {id, name, role}}."""
    env = os.environ if env is None else env
    raw = env.get(ENV_TOKENS)
    if not raw or not str(raw).strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f'{ENV_TOKENS} must be a JSON object of token → operator.')
    out = {}
    for token, record in data.items():
        if not isinstance(token, str) or not token.strip():
            raise ValueError(f'{ENV_TOKENS} keys must be non-empty token strings.')
        out[token.strip()] = from_mapping(record)
    return out


def resolve_bearer(authorization, env=None):
    """Return the Operator for a Bearer token, or None when no Authorization is sent."""
    token = parse_bearer(authorization)
    if token is None:
        return None
    mapping = tokens_from_env(env=env)
    operator = mapping.get(token)
    if operator is None:
        raise PermissionError('Unknown or revoked operator token.')
    return operator
