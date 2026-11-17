# OIDC browser sign-in for Desk operators.
# DESK_OIDC_ISSUER unset: this module is idle and demo mint stays in charge.
# DESK_OIDC_ISSUER set: fail closed. No operator.json, no default admin.
"""OIDC authorization-code login, HS256 ID tokens, and file-backed operator sessions."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookies import CookieError, SimpleCookie
from dataclasses import dataclass
from pathlib import Path

import api_contract
import operators

SESSIONS_DIR = '_operator_sessions'
COOKIE_NAME = 'desk_operator'
ENV_ISSUER = 'DESK_OIDC_ISSUER'
ENV_CLIENT_ID = 'DESK_OIDC_CLIENT_ID'
ENV_CLIENT_SECRET = 'DESK_OIDC_CLIENT_SECRET'
ENV_REDIRECT = 'DESK_OIDC_REDIRECT_URI'
ENV_GROUPS_CLAIM = 'DESK_OIDC_GROUPS_CLAIM'
ENV_ADMIN_GROUP = 'DESK_OIDC_ADMIN_GROUP'
ENV_REVIEWER_GROUP = 'DESK_OIDC_REVIEWER_GROUP'
ENV_MOCK = 'DESK_OIDC_MOCK'
ENV_MOCK_SUB = 'DESK_OIDC_MOCK_SUB'
ENV_MOCK_NAME = 'DESK_OIDC_MOCK_NAME'
ENV_MOCK_EMAIL = 'DESK_OIDC_MOCK_EMAIL'
ENV_MOCK_USERNAME = 'DESK_OIDC_MOCK_PREFERRED_USERNAME'
ENV_MOCK_GROUPS = 'DESK_OIDC_MOCK_GROUPS'
DEFAULT_GROUPS_CLAIM = 'groups'
DEFAULT_ADMIN_GROUP = 'desk-admin'
DEFAULT_REVIEWER_GROUP = 'desk-reviewer'
SESSION_TTL = 8 * 60 * 60
PENDING_TTL = 10 * 60
TOKEN_TTL = 5 * 60
_HTTP_TIMEOUT = 5
_MAX_BODY = 65536
_ID = re.compile(r'^[0-9a-f]{64}$')
_MOCK_TRUE = frozenset({'1', 'true', 'yes'})


class OidcConfigError(Exception):
    """Issuer is set and Desk cannot start a safe flow."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


class OidcAuthError(Exception):
    """A sign-in attempt failed closed. No operator session is stored."""

    def __init__(self, message, status=401, code=None):
        super().__init__(message)
        self.message = message
        self.status = status
        if code is None:
            code = (api_contract.OPERATOR_FORBIDDEN if status == 403
                    else api_contract.OPERATOR_UNAUTHORIZED)
        self.code = code


@dataclass(frozen=True)
class OidcConfig:
    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    groups_claim: str
    admin_group: str
    reviewer_group: str
    mock: bool


@dataclass(frozen=True)
class PendingAuth:
    state: str
    nonce: str
    code_verifier: str
    code_challenge: str
    next_path: str
    expires_at: int


@dataclass(frozen=True)
class OperatorSession:
    session_id: str
    operator_id: str
    display_name: str
    role: str
    expires_at: int


def _now(now):
    return time.time() if now is None else now


def _blank(value):
    if value is None:
        return ''
    return str(value).strip()


def _flag(value):
    return _blank(value).lower() in _MOCK_TRUE


def _http_url(value, label):
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in ('http', 'https') or not parts.netloc:
        raise OidcConfigError(f'{label} must be an http(s) URL.')
    if parts.username or parts.password or parts.query or parts.fragment:
        raise OidcConfigError(f'{label} must be an http(s) URL.')
    return value


def _setting(env, name, default):
    value = _blank(env.get(name))
    return value or default


def load_config(env=None):
    """Return OidcConfig, or None when DESK_OIDC_ISSUER is unset."""
    env = os.environ if env is None else env
    issuer = _blank(env.get(ENV_ISSUER))
    if not issuer:
        return None
    client_id = _blank(env.get(ENV_CLIENT_ID))
    client_secret = env.get(ENV_CLIENT_SECRET) or ''
    redirect_uri = _blank(env.get(ENV_REDIRECT))
    if not client_id or not client_secret.strip() or not redirect_uri:
        raise OidcConfigError(
            'DESK_OIDC_ISSUER is set but DESK_OIDC_CLIENT_ID, '
            'DESK_OIDC_CLIENT_SECRET, and DESK_OIDC_REDIRECT_URI must be set.')
    return OidcConfig(
        issuer=_http_url(issuer, ENV_ISSUER),
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=_http_url(redirect_uri, ENV_REDIRECT),
        groups_claim=_setting(env, ENV_GROUPS_CLAIM, DEFAULT_GROUPS_CLAIM),
        admin_group=_setting(env, ENV_ADMIN_GROUP, DEFAULT_ADMIN_GROUP),
        reviewer_group=_setting(env, ENV_REVIEWER_GROUP, DEFAULT_REVIEWER_GROUP),
        mock=_flag(env.get(ENV_MOCK)),
    )


def safe_next(value):
    """A same-app path. Anything else, including an off-site URL, becomes /desk."""
    if not isinstance(value, str):
        return '/desk'
    if not value.startswith('/') or value.startswith('//') or value.startswith('/\\'):
        return '/desk'
    if '\\' in value or '://' in value or value.startswith('/auth/oidc'):
        return '/desk'
    if any(ord(char) < 32 for char in value):
        return '/desk'
    return value


def discovery_url(issuer):
    parts = urllib.parse.urlsplit(issuer)
    path = (parts.path or '').rstrip('/')
    suffix = '/.well-known/openid-configuration'
    discovered = suffix + path if path else suffix
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, discovered, '', ''))


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def _b64url_decode(segment):
    pad = '=' * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + pad)


def _same(left, right):
    if not isinstance(left, str) or not isinstance(right, str) or len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


def code_challenge(verifier):
    return _b64url(hashlib.sha256(verifier.encode('ascii')).digest())


def sign_hs256(claims, secret):
    header = _b64url(json.dumps({'alg': 'HS256', 'typ': 'JWT'}, separators=(',', ':')).encode())
    payload = _b64url(json.dumps(claims, separators=(',', ':')).encode())
    signing = f'{header}.{payload}'
    signature = hmac.new(secret.encode('utf-8'), signing.encode('ascii'), hashlib.sha256).digest()
    return f'{signing}.{_b64url(signature)}'


def verify_id_token(token, config, *, nonce, now):
    """Check an HS256 ID token. Any other algorithm is rejected."""
    if not isinstance(token, str) or token.count('.') != 2:
        raise OidcAuthError('ID token is not a JWT.')
    header_b64, payload_b64, signature_b64 = token.split('.')
    try:
        header = json.loads(_b64url_decode(header_b64))
        claims = json.loads(_b64url_decode(payload_b64))
        signature = _b64url_decode(signature_b64)
    except (ValueError, json.JSONDecodeError, UnicodeError):
        raise OidcAuthError('ID token is not a JWT.')
    if not isinstance(header, dict) or header.get('alg') != 'HS256' or header.get('crit'):
        raise OidcAuthError('ID token algorithm is not allowed.')
    if not isinstance(claims, dict):
        raise OidcAuthError('ID token payload is not an object.')
    expected = hmac.new(
        config.client_secret.encode('utf-8'),
        f'{header_b64}.{payload_b64}'.encode('ascii'),
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(expected, signature):
        raise OidcAuthError('ID token signature does not match.')
    if claims.get('iss') != config.issuer:
        raise OidcAuthError('ID token issuer does not match.')
    audience = claims.get('aud')
    if isinstance(audience, str):
        audiences = [audience]
    elif isinstance(audience, list) and audience and all(isinstance(item, str) for item in audience):
        audiences = audience
    else:
        raise OidcAuthError('ID token audience does not match.')
    if config.client_id not in audiences:
        raise OidcAuthError('ID token audience does not match.')
    if len(audiences) > 1 and claims.get('azp') != config.client_id:
        raise OidcAuthError('ID token audience does not match.')
    exp = claims.get('exp')
    if isinstance(exp, bool) or not isinstance(exp, (int, float)) or now >= exp:
        raise OidcAuthError('ID token is expired.')
    nbf = claims.get('nbf')
    if nbf is not None and (isinstance(nbf, bool) or not isinstance(nbf, (int, float)) or now < nbf):
        raise OidcAuthError('ID token is not valid yet.')
    if claims.get('nonce') != nonce:
        raise OidcAuthError('ID token nonce does not match.')
    return claims


def subject_ok(sub):
    return (isinstance(sub, str) and bool(sub) and sub == sub.strip()
            and len(sub) <= operators.MAX_SUBJECT_LENGTH
            and '\n' not in sub and '\r' not in sub and '<' not in sub and '>' not in sub)


def clean_subject(sub):
    if not isinstance(sub, str):
        raise OidcAuthError('OIDC subject is not allowed.')
    value = sub.strip()
    if not subject_ok(value):
        raise OidcAuthError('OIDC subject is not allowed.')
    return value


def _stored_name_ok(name):
    return (isinstance(name, str) and bool(name) and name == name.strip()
            and len(name) <= operators.MAX_NAME_LENGTH
            and '\n' not in name and '\r' not in name)


def _display_piece(value):
    if not isinstance(value, str):
        return None
    text = ' '.join(value.replace('\r', ' ').replace('\n', ' ').split())
    if not text:
        return None
    return text[:operators.MAX_NAME_LENGTH]


def display_name_from_claims(claims, sub):
    for key in ('name', 'email', 'preferred_username'):
        piece = _display_piece(claims.get(key))
        if piece:
            return piece
    return sub[:operators.MAX_NAME_LENGTH]


def groups_from_claims(claims, claim_name):
    raw = claims.get(claim_name)
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [item.strip() for item in raw if isinstance(item, str) and item.strip()]


def role_for_groups(groups, config):
    names = set(groups)
    if config.admin_group in names:
        return operators.ROLE_ADMIN
    if config.reviewer_group in names:
        return operators.ROLE_REVIEWER
    return None


def operator_from_claims(claims, config):
    if not isinstance(claims, dict):
        raise OidcAuthError('ID token payload is not an object.')
    sub = clean_subject(claims.get('sub'))
    role = role_for_groups(groups_from_claims(claims, config.groups_claim), config)
    if role is None:
        raise OidcAuthError('This account is not in a Desk group.', status=403)
    return operators.Operator(
        operator_id=sub,
        display_name=display_name_from_claims(claims, sub),
        role=role,
    )


def cookie_value(cookie_header):
    jar = SimpleCookie()
    try:
        jar.load(cookie_header or '')
    except CookieError:
        return None
    if COOKIE_NAME not in jar:
        return None
    value = jar[COOKIE_NAME].value
    if not _ID.fullmatch(value or ''):
        return None
    return value


def cookie_header(session, *, secure, now):
    max_age = max(0, int(session.expires_at - _now(now)))
    header = (f'{COOKIE_NAME}={session.session_id}; Path=/; HttpOnly; '
              f'SameSite=Strict; Max-Age={max_age}')
    if secure:
        header += '; Secure'
    return header


class SessionStore:
    """Operator sessions and one-time login state under directory/_operator_sessions."""

    def __init__(self, directory):
        self.root = Path(directory) / SESSIONS_DIR

    def _file(self, kind, ident):
        if not _ID.fullmatch(ident or ''):
            return None
        return self.root / kind / f'{ident}.json'

    def _write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + f'.{secrets.token_hex(4)}.tmp')
        temporary.write_text(json.dumps(data, sort_keys=True) + '\n', encoding='utf-8')
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)

    def _read(self, path):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _drop_expired(self, path, data, now):
        expires_at = data.get('expires_at')
        if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)) or now >= expires_at:
            path.unlink(missing_ok=True)
            return None
        return data

    def peek(self, kind, ident, now):
        path = self._file(kind, ident)
        if path is None or not path.is_file():
            return None
        data = self._read(path)
        if data is None:
            return None
        return self._drop_expired(path, data, now)

    def take(self, kind, ident, now):
        path = self._file(kind, ident)
        if path is None or not path.is_file():
            return None
        claimed = path.with_name(path.name + '.claimed')
        try:
            os.replace(path, claimed)
        except OSError:
            return None
        data = self._read(claimed)
        claimed.unlink(missing_ok=True)
        if data is None:
            return None
        expires_at = data.get('expires_at')
        if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)) or now >= expires_at:
            return None
        return data

    def sweep(self, now):
        if not self.root.exists():
            return
        cutoff = now - SESSION_TTL
        for path in self.root.rglob('*'):
            if not path.is_file():
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue

    def save_pending(self, pending):
        self._write(self._file('pending', pending.state), {
            'state': pending.state,
            'nonce': pending.nonce,
            'code_verifier': pending.code_verifier,
            'code_challenge': pending.code_challenge,
            'next_path': pending.next_path,
            'expires_at': pending.expires_at,
        })

    def save_session(self, operator, now):
        session = OperatorSession(
            session_id=secrets.token_hex(32),
            operator_id=operator.operator_id,
            display_name=operator.display_name,
            role=operator.role,
            expires_at=int(_now(now)) + SESSION_TTL,
        )
        self._write(self._file('sessions', session.session_id), {
            'session_id': session.session_id,
            'operator_id': session.operator_id,
            'display_name': session.display_name,
            'role': session.role,
            'expires_at': session.expires_at,
        })
        return session

    def save_code(self, record):
        self._write(self._file('codes', record['code']), record)

    def operator_from_cookie(self, cookie_header, now):
        now = _now(now)
        session_id = cookie_value(cookie_header)
        if session_id is None:
            return None
        data = self.peek('sessions', session_id, now)
        if data is None or data.get('session_id') != session_id:
            return None
        role = data.get('role')
        if role not in operators.ROLES:
            return None
        operator_id = data.get('operator_id')
        display_name = data.get('display_name')
        if not subject_ok(operator_id) or not _stored_name_ok(display_name):
            return None
        return operators.Operator(operator_id=operator_id, display_name=display_name, role=role)


def _read_http(opener, request):
    try:
        with opener(request, timeout=_HTTP_TIMEOUT) as response:
            body = response.read(_MAX_BODY + 1)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise OidcAuthError('The identity provider did not answer.')
    if len(body) > _MAX_BODY:
        raise OidcAuthError('The identity provider response is too large.')
    try:
        data = json.loads(body.decode('utf-8'))
    except (UnicodeError, json.JSONDecodeError):
        raise OidcAuthError('The identity provider response is not JSON.')
    if not isinstance(data, dict):
        raise OidcAuthError('The identity provider response is not JSON.')
    return data


def discover(issuer, opener):
    url = discovery_url(issuer)
    data = _read_http(opener, urllib.request.Request(url, headers={'Accept': 'application/json'}))
    if data.get('issuer') != issuer:
        raise OidcAuthError('OIDC discovery issuer does not match.')
    auth_endpoint = data.get('authorization_endpoint')
    token_endpoint = data.get('token_endpoint')
    if not isinstance(auth_endpoint, str) or not isinstance(token_endpoint, str):
        raise OidcAuthError('OIDC discovery is missing endpoints.')
    return data


def authorization_url(config, pending, metadata):
    base = '/auth/oidc/mock/authorize' if config.mock else metadata['authorization_endpoint']
    query = urllib.parse.urlencode({
        'response_type': 'code',
        'client_id': config.client_id,
        'redirect_uri': config.redirect_uri,
        'scope': 'openid',
        'state': pending.state,
        'nonce': pending.nonce,
        'code_challenge': pending.code_challenge,
        'code_challenge_method': 'S256',
    })
    joiner = '&' if '?' in base else '?'
    return f'{base}{joiner}{query}'


def start_login(config, store, next_path, *, now=None, opener=None):
    now = _now(now)
    store.sweep(now)
    metadata = None if config.mock else discover(config.issuer, opener or urllib.request.urlopen)
    verifier = secrets.token_urlsafe(32)
    pending = PendingAuth(
        state=secrets.token_hex(32),
        nonce=secrets.token_hex(32),
        code_verifier=verifier,
        code_challenge=code_challenge(verifier),
        next_path=safe_next(next_path),
        expires_at=int(now) + PENDING_TTL,
    )
    store.save_pending(pending)
    return pending, authorization_url(config, pending, metadata)


def _mock_text(args, env, query_name, env_name):
    if query_name in args and args.get(query_name) is not None:
        return _blank(args.get(query_name))
    return _blank(env.get(env_name))


def mock_identity(args, env):
    groups_raw = args.get('mock_groups') if 'mock_groups' in args else None
    if groups_raw is None:
        groups_raw = env.get(ENV_MOCK_GROUPS)
    if groups_raw is None:
        groups = []
    else:
        groups = [part.strip() for part in str(groups_raw).split(',') if part.strip()]
    return {
        'sub': _mock_text(args, env, 'mock_sub', ENV_MOCK_SUB),
        'name': _mock_text(args, env, 'mock_name', ENV_MOCK_NAME),
        'email': _mock_text(args, env, 'mock_email', ENV_MOCK_EMAIL),
        'preferred_username': _mock_text(args, env, 'mock_preferred_username', ENV_MOCK_USERNAME),
        'groups': groups,
    }


def mock_authorize(config, store, args, *, now=None, env=None):
    """Issue one code for the in-process issuer and return the callback URL."""
    now = _now(now)
    env = os.environ if env is None else env
    if not config.mock:
        raise OidcAuthError('Mock issuer is disabled.', status=404)
    if args.get('client_id') != config.client_id or args.get('redirect_uri') != config.redirect_uri:
        raise OidcAuthError('OIDC client does not match.')
    if args.get('code_challenge_method') != 'S256':
        raise OidcAuthError('PKCE method must be S256.')
    pending_data = store.peek('pending', args.get('state') or '', now)
    if pending_data is None:
        raise OidcAuthError('Sign-in expired or unknown.')
    if not _same(args.get('nonce') or '', pending_data.get('nonce') or ''):
        raise OidcAuthError('OIDC nonce does not match.')
    if not _same(args.get('code_challenge') or '', pending_data.get('code_challenge') or ''):
        raise OidcAuthError('PKCE challenge does not match.')
    identity = mock_identity(args, env)
    code = secrets.token_hex(32)
    store.save_code({
        'code': code,
        'state': pending_data['state'],
        'nonce': pending_data['nonce'],
        'code_challenge': pending_data['code_challenge'],
        'expires_at': int(now) + PENDING_TTL,
        'sub': identity['sub'],
        'name': identity['name'],
        'email': identity['email'],
        'preferred_username': identity['preferred_username'],
        'groups': identity['groups'],
    })
    query = urllib.parse.urlencode({'code': code, 'state': pending_data['state']})
    joiner = '&' if '?' in config.redirect_uri else '?'
    return f'{config.redirect_uri}{joiner}{query}'


def _claims_from_mock(config, store, code, pending, now):
    record = store.take('codes', code, now)
    if record is None or not _same(record.get('state') or '', pending.state):
        raise OidcAuthError('Authorization code is unknown.')
    if not _same(record.get('code_challenge') or '', pending.code_challenge):
        raise OidcAuthError('PKCE challenge does not match.')
    claims = {
        'iss': config.issuer,
        'aud': config.client_id,
        'exp': int(now) + TOKEN_TTL,
        'iat': int(now),
        'nonce': record.get('nonce'),
        'sub': record.get('sub') or '',
    }
    for key in ('name', 'email', 'preferred_username'):
        if record.get(key):
            claims[key] = record[key]
    claims[config.groups_claim] = list(record.get('groups') or [])
    token = sign_hs256(claims, config.client_secret)
    return verify_id_token(token, config, nonce=pending.nonce, now=now)


def _claims_from_http(config, code, pending, now, opener):
    metadata = discover(config.issuer, opener)
    form = urllib.parse.urlencode({
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': config.redirect_uri,
        'client_id': config.client_id,
        'client_secret': config.client_secret,
        'code_verifier': pending.code_verifier,
    }).encode('ascii')
    request = urllib.request.Request(
        metadata['token_endpoint'],
        data=form,
        headers={
            'Accept': 'application/json',
            'Content-Type': 'application/x-www-form-urlencoded',
        },
    )
    data = _read_http(opener, request)
    token = data.get('id_token')
    if not isinstance(token, str) or not token:
        raise OidcAuthError('Token response did not include an ID token.')
    return verify_id_token(token, config, nonce=pending.nonce, now=now)


def finish_login(config, store, code, state, *, now=None, opener=None):
    """Exchange one code. Raise OidcAuthError and store nothing on failure."""
    now = _now(now)
    if not code or not state:
        raise OidcAuthError('Missing code or state.')
    pending_data = store.take('pending', state, now)
    if pending_data is None:
        raise OidcAuthError('Sign-in expired or already used.')
    try:
        pending = PendingAuth(
            state=pending_data['state'],
            nonce=pending_data['nonce'],
            code_verifier=pending_data['code_verifier'],
            code_challenge=pending_data['code_challenge'],
            next_path=safe_next(pending_data.get('next_path')),
            expires_at=int(pending_data['expires_at']),
        )
    except (KeyError, TypeError, ValueError):
        raise OidcAuthError('Sign-in expired or already used.')
    if config.mock:
        claims = _claims_from_mock(config, store, code, pending, now)
    else:
        claims = _claims_from_http(config, code, pending, now, opener or urllib.request.urlopen)
    operator = operator_from_claims(claims, config)
    session = store.save_session(operator, now)
    return operator, session, pending.next_path
