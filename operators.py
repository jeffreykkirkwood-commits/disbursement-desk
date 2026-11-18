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
# Blueprint / older docs used OPERATOR_BEARER_TOKENS; accept it as an alias.
ENV_TOKENS_LEGACY = 'OPERATOR_BEARER_TOKENS'
ENV_DEMO_TOOLS = 'DESK_DEMO_TOOLS'
ENV_OIDC_ISSUER = 'DESK_OIDC_ISSUER'
ENV_RENDER = 'RENDER'
# Local/dev sandboxes default to admin so Reset demo stays usable. On Render
# without OIDC or bearer tokens, mint reviewer unless DESK_OPERATOR_ROLE=admin.
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

    def can_reconcile_stripe(self):
        """Stripe recon dry-run and apply stay admin-only, same gate as Reset."""
        return self.role == ROLE_ADMIN

    def can_use_demo_tools(self, env=None):
        """Demo mutators: admin, or any role when DESK_DEMO_TOOLS=1."""
        if self.role == ROLE_ADMIN:
            return True
        env = os.environ if env is None else env
        return (env.get(ENV_DEMO_TOOLS) or '').strip() == '1'

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


def _token_map_raw(env):
    """Prefer DESK_OPERATOR_TOKENS; fall back to OPERATOR_BEARER_TOKENS."""
    for key in (ENV_TOKENS, ENV_TOKENS_LEGACY):
        raw = env.get(key)
        if raw and str(raw).strip():
            return raw, key
    return None, None


def _oidc_or_tokens_configured(env):
    if (env.get(ENV_OIDC_ISSUER) or '').strip():
        return True
    raw, _ = _token_map_raw(env)
    return raw is not None


def mint_role(env=None):
    """Role for a newly minted demo operator.

    Explicit DESK_OPERATOR_ROLE always wins. Local/dev keeps admin. On Render
    with neither OIDC nor bearer tokens configured, mint reviewer unless the
    operator sets DESK_OPERATOR_ROLE=admin.
    """
    env = os.environ if env is None else env
    explicit = env.get(ENV_ROLE)
    if explicit is not None and str(explicit).strip():
        return _clean_role(explicit)
    if env.get(ENV_RENDER) and not _oidc_or_tokens_configured(env):
        return ROLE_REVIEWER
    return DEFAULT_ROLE


def default_operator(demo_id, env=None):
    """Mint a principal for a new demo. Role/name come from env at create time only."""
    env = os.environ if env is None else env
    suffix = (demo_id or secrets.token_hex(4))[:12]
    return Operator(
        operator_id=f'op-{suffix}',
        display_name=_clean_name(env.get(ENV_NAME)),
        role=mint_role(env),
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
    """Optional production-shaped token map: {token: {id, name, role}}.

    Reads DESK_OPERATOR_TOKENS when set; otherwise OPERATOR_BEARER_TOKENS.
    """
    env = os.environ if env is None else env
    raw, source = _token_map_raw(env)
    if raw is None:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f'{source} must be a JSON object of token → operator.')
    out = {}
    for token, record in data.items():
        if not isinstance(token, str) or not token.strip():
            raise ValueError(f'{source} keys must be non-empty token strings.')
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
