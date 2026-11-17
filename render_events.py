"""Shared-secret gate and parser for POST /internal/render-events."""
from __future__ import annotations

import hmac
import json
import os
import re

ENV_SECRET = 'RENDER_EVENTS_SECRET'
HEADER = 'X-Render-Events-Secret'

EVENT_TYPES = frozenset({
    'build_ended',
    'build_started',
    'deploy_ended',
    'deploy_started',
    'image_pull_failed',
    'server_available',
    'server_failed',
    'server_restarted',
})
STATUSES = frozenset({'succeeded', 'failed', 'canceled'})

_MAX_BODY = 8192
_IDENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}')


def configured_secret(env=None):
    """Return the trimmed shared secret, or '' when it is unset."""
    source = os.environ if env is None else env
    raw = source.get(ENV_SECRET)
    if not isinstance(raw, str):
        return ''
    return raw.strip()


def header_matches(presented, secret):
    """True only when both sides are non-empty and equal."""
    if not secret or not isinstance(presented, str):
        return False
    got = presented.strip()
    if not got or len(got) != len(secret):
        return False
    return hmac.compare_digest(got, secret)


def notice_from_payload(payload):
    """Parse a thin deploy notification into log fields.

    Raises ValueError when the body is not a JSON object within the size cap.
    Unknown event types become event_type 'other'. status is included only
    for succeeded, failed, or canceled.
    """
    if not isinstance(payload, (bytes, bytearray)) or len(payload) > _MAX_BODY:
        raise ValueError('payload')
    try:
        body = json.loads(payload.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError('payload') from None
    if not isinstance(body, dict):
        raise ValueError('payload')
    raw_type = body.get('type')
    notice = {
        'event_type': raw_type if isinstance(raw_type, str) and raw_type in EVENT_TYPES else 'other',
    }
    data = body.get('data')
    if not isinstance(data, dict):
        data = {}
    status = data.get('status')
    if isinstance(status, str) and status in STATUSES:
        notice['status'] = status
    service_id = _token(data.get('serviceId'), _IDENT)
    if service_id:
        notice['service_id'] = service_id
    service_name = _token(data.get('serviceName'), _NAME)
    if service_name:
        notice['service_name'] = service_name
    event_id = _token(data.get('id'), _IDENT)
    if event_id:
        notice['render_event_id'] = event_id
    return notice


def _token(value, pattern):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        return ''
    return value
