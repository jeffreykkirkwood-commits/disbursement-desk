# Deploy posture helpers for Render-hosted Desk (ephemeral FS, secrets inventory).
"""Warn when demo storage will not survive redeploy; list secret env keys."""
from __future__ import annotations

import os
from pathlib import Path

from event_log import event

# Absolute paths that are always ephemeral on Linux hosts (and on Render free).
_EPHEMERAL_PREFIXES = ('/tmp', '/var/tmp')

# Env keys that must never be committed; Dashboard / secret store only.
# Keep in sync with docs/deploy-posture.md and render.yaml sync:false entries.
SECRET_ENV_KEYS = (
    'DESK_OPERATOR_TOKENS',
    'DESK_OIDC_CLIENT_SECRET',
    'STRIPE_SECRET_KEY',
    'STRIPE_WEBHOOK_SECRET',
    'RENDER_EVENTS_SECRET',
    'DESK_METRICS_SECRET',
    'OFAC_HTTP_URL',
    'OFAC_HTTP_TOKEN',
    'DESK_AI_API_KEY',
    'TURSO_DATABASE_URL',
    'TURSO_AUTH_TOKEN',
    'SENTRY_DSN',
)


def demo_directory_is_ephemeral(directory: Path | str) -> bool:
    """True when *directory* lives under a known ephemeral prefix."""
    resolved = Path(directory).expanduser().resolve()
    text = str(resolved)
    return any(text == prefix or text.startswith(prefix + os.sep)
               for prefix in _EPHEMERAL_PREFIXES)


def warn_ephemeral_demo_directory(directory: Path | str) -> bool:
    """Emit a structured warning when running on Render with ephemeral demo storage.

    Returns True when a warning was emitted. Local/dev runs (no RENDER=) stay quiet
    so unit tests and laptop demos are not noisy. Durable visitor state should use
    Turso tenancy (docs/tenancy.md) or a paid persistent disk (docs/deploy-posture.md).
    """
    if not os.environ.get('RENDER'):
        return False
    if not demo_directory_is_ephemeral(directory):
        return False
    event('deploy_posture_ephemeral_demo_directory',
          demo_directory=str(Path(directory)),
          hint='Use TURSO_DATABASE_URL per visitor or attach a paid Render disk; '
               'see docs/deploy-posture.md')
    return True


def secret_env_keys() -> tuple[str, ...]:
    """Secret environment variable names operators must set outside git."""
    return SECRET_ENV_KEYS
