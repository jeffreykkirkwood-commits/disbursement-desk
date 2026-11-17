# START HERE — Test-mode Stripe Connect Transfers.
# The only module that imports the Stripe SDK. It does not write payout rows.
"""Create a platform-balance Transfer and parse a signed webhook into a desk command."""
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import stripe

import api_contract
import storage
from desk_common import APIError

API_VERSION = '2026-09-30.endive'
_TEST_PREFIXES = ('rk_test_', 'sk_test_')
_LIVE_PREFIXES = ('sk_live_', 'rk_live_')
_ACCT = re.compile(r'^acct_[A-Za-z0-9]+$')
_PARSE_KEY = 'rk_test_webhook_parse_only'


class WebhookSignatureError(Exception):
    """The webhook body failed signature verification or was not a JSON event."""


class StripeConfig:
    def __init__(self, secret_key, webhook_secret, destinations, pilot_id, api_base):
        self.secret_key = secret_key
        self.webhook_secret = webhook_secret
        self.destinations = destinations
        self.pilot_id = pilot_id
        self.api_base = api_base


def stripe_enabled(env=None):
    """True only when STRIPE_PAYOUTS_ENABLED is exactly '1'. Secrets are not read."""
    env = os.environ if env is None else env
    return env.get('STRIPE_PAYOUTS_ENABLED') == '1'


def load_config(env=None):
    """Return StripeConfig when the flag is '1' and the test config is safe.

    Return None when the flag is not exactly '1'. Raise STRIPE_SEND_BLOCKED
    when the flag is '1' and the key, webhook secret, map, or pilot id is unsafe.
    """
    env = os.environ if env is None else env
    if not stripe_enabled(env):
        return None
    key = _value(env, 'STRIPE_SECRET_KEY')
    if key.startswith(_LIVE_PREFIXES) or not key.startswith(_TEST_PREFIXES) or len(key) <= len('rk_test_'):
        _blocked()
    webhook = _value(env, 'STRIPE_WEBHOOK_SECRET')
    if not webhook.startswith('whsec_'):
        _blocked()
    parsed = _destination_map(_value(env, 'STRIPE_DESTINATION_MAP'))
    if parsed is None:
        _blocked()
    pilot = _value(env, 'STRIPE_DEMO_ID')
    if not storage.DEMO_TENANT.fullmatch(pilot):
        _blocked()
    return StripeConfig(
        key, webhook, parsed, pilot, _api_base(_value(env, 'STRIPE_API_BASE')))


def send_mode(env=None, demo_id=None):
    """Return 'demo' or 'stripe'. Misconfigured flag-on config raises."""
    return mode_for(load_config(env), demo_id)


def mode_for(config, demo_id):
    if config is None:
        return 'demo'
    if not isinstance(demo_id, str) or not storage.DEMO_TENANT.fullmatch(demo_id):
        _blocked()
    if demo_id != config.pilot_id:
        return 'demo'
    return 'stripe'


def destination_for(payee_account, column=None, env=None):
    """Use the payout column when it is set, otherwise the env map."""
    config = load_config(env)
    if config is None:
        _blocked()
    chosen = (column or '').strip()
    if chosen:
        if not _ACCT.fullmatch(chosen):
            _blocked()
        return chosen
    dest = config.destinations.get(payee_account)
    if not dest:
        _blocked()
    return dest


def transfer_idempotency_key(demo_id, payout_id):
    return f'desk-transfer:{demo_id}:{payout_id}'


def create_transfer(*, payout_id, amount_cents, currency, destination, demo_id,
                    idempotency_key, env=None):
    """POST /v1/transfers. Return the transfer fields. Do not write the database."""
    config = load_config(env)
    if config is None or mode_for(config, demo_id) != 'stripe':
        _blocked()
    if (currency != 'usd' or isinstance(amount_cents, bool) or not isinstance(amount_cents, int)
            or amount_cents <= 0 or not _ACCT.fullmatch(destination or '') or not idempotency_key):
        _blocked()
    client = _client(config)
    try:
        transfer = client.v1.transfers.create(
            {
                'amount': amount_cents,
                'currency': 'usd',
                'destination': destination,
                'metadata': {'payout_id': str(payout_id), 'demo_id': demo_id},
            },
            {'idempotency_key': idempotency_key},
        )
    except stripe.StripeError:
        _blocked(502)
    return _checked_transfer(
        transfer, payout_id, amount_cents, destination, demo_id, idempotency_key)


def parse_webhook(payload, signature_header, secret):
    """Verify the raw body, then return a desk command, or None to ignore the event."""
    if not isinstance(payload, (bytes, bytearray)):
        raise WebhookSignatureError('Webhook payload must be the raw body.')
    client = stripe.StripeClient(
        _PARSE_KEY, stripe_version=API_VERSION, max_network_retries=0)
    try:
        event = client.construct_event(payload, signature_header, secret)
    except (stripe.SignatureVerificationError, ValueError):
        raise WebhookSignatureError('Invalid Stripe signature.') from None
    return _command_from_event(event)


def demo_database(directory, demo_id):
    """Return the visitor payments file for a 64-hex demo id, or None."""
    if not isinstance(demo_id, str) or not storage.DEMO_TENANT.fullmatch(demo_id):
        return None
    root = Path(directory).resolve()
    path = (root / demo_id / 'payments.sqlite3').resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    if storage.tenant_from_path(path) != demo_id or not path.is_file():
        return None
    return path


def _blocked(status=409):
    raise APIError(status, 'Stripe send is blocked.', code=api_contract.STRIPE_SEND_BLOCKED)


def _value(env, name):
    raw = env.get(name)
    return raw.strip() if isinstance(raw, str) else ''


def _destination_map(raw):
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    cleaned = {}
    for key, value in parsed.items():
        if not isinstance(key, str) or not isinstance(value, str) or not _ACCT.fullmatch(value):
            return None
        cleaned[key] = value
    return cleaned


def _api_base(value):
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in ('http', 'https') or parsed.hostname not in ('127.0.0.1', 'localhost'):
        return None
    if parsed.username or parsed.password or parsed.path not in ('', '/'):
        return None
    return f'{parsed.scheme}://{parsed.netloc}'


def _client(config):
    kwargs = {
        'api_key': config.secret_key,
        'stripe_version': API_VERSION,
        'max_network_retries': 0,
    }
    if config.api_base:
        kwargs['base_addresses'] = {'api': config.api_base}
    return stripe.StripeClient(**kwargs)


def _checked_transfer(transfer, payout_id, amount_cents, destination, demo_id, idempotency_key):
    transfer_id = str(_field(transfer, 'id') or '')
    if (_field(transfer, 'livemode') or _field(transfer, 'reversed')
            or not transfer_id.startswith('tr_')):
        _blocked()
    if _field(transfer, 'amount') != amount_cents:
        _blocked()
    if str(_field(transfer, 'currency') or '').lower() != 'usd':
        _blocked()
    if _destination_id(_field(transfer, 'destination')) != destination:
        _blocked()
    if _metadata_value(transfer, 'payout_id') != str(payout_id):
        _blocked()
    if _metadata_value(transfer, 'demo_id') != demo_id:
        _blocked()
    return {
        'transfer_id': transfer_id,
        'amount_cents': amount_cents,
        'currency': 'usd',
        'destination': destination,
        'livemode': False,
        'idempotency_key': idempotency_key,
    }


def _command_from_event(event):
    obj = _field(_field(event, 'data'), 'object')
    event_type = str(_field(event, 'type') or '')
    reversed_flag = bool(_field(obj, 'reversed'))
    is_transfer = (
        _field(obj, 'object') == 'transfer'
        or str(_field(obj, 'id') or '').startswith('tr_'))
    if event_type == 'transfer.reversed' or (is_transfer and reversed_flag):
        outcome = 'FAILED'
    elif event_type == 'transfer.created' and not reversed_flag:
        outcome = 'SETTLED'
    else:
        return None
    if obj is None:
        return None
    try:
        amount_cents = int(_field(obj, 'amount'))
    except (TypeError, ValueError):
        amount_cents = None
    return {
        'stripe_event_id': str(_field(event, 'id') or ''),
        'payout_id': _metadata_value(obj, 'payout_id'),
        'demo_id': _metadata_value(obj, 'demo_id'),
        'transfer_id': str(_field(obj, 'id') or ''),
        'amount_cents': amount_cents,
        'currency': str(_field(obj, 'currency') or '').lower(),
        'destination': _destination_id(_field(obj, 'destination')),
        'outcome': outcome,
        'livemode': bool(_field(event, 'livemode')) or bool(_field(obj, 'livemode')),
    }


def _field(obj, name, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    try:
        return obj[name]
    except Exception:
        return getattr(obj, name, default)


def _metadata_value(obj, key):
    value = _field(_field(obj, 'metadata'), key, '')
    return '' if value is None else str(value)


def _destination_id(value):
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    ident = _field(value, 'id', '')
    return '' if ident is None else str(ident)
