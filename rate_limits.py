# START HERE — In-process abuse limits for mutating Desk POSTs (Wave C).
# Sliding window per (bucket, client IP, demo session). Memory only — same
# process-local posture as metrics; not shared across Gunicorn workers.
"""In-process rate limits keyed by route bucket, IP, and demo session."""
from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from threading import Lock

ENV_MAX = 'DESK_RATE_LIMIT_MAX'
ENV_WINDOW = 'DESK_RATE_LIMIT_WINDOW_SECONDS'
DEFAULT_MAX = 60
DEFAULT_WINDOW_SECONDS = 60

# Route buckets used by hosted_app (one counter family each).
BUCKET_DECIDE = 'decide'
BUCKET_CREATE = 'create'
BUCKET_RESET = 'reset'
BUCKET_SIMULATE = 'simulate'
BUCKET_DEMO_SCENARIO = 'demo-scenario'
BUCKET_RECON = 'recon'

_lock = Lock()
_hits: dict[str, deque[float]] = defaultdict(deque)
_max = DEFAULT_MAX
_window = float(DEFAULT_WINDOW_SECONDS)


def _parse_positive_int(raw, default):
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        return default
    return value if value > 0 else default


def configure(env=None):
    """Reload max/window from env. Tests call this after patching environ."""
    global _max, _window
    source = os.environ if env is None else env
    with _lock:
        _max = _parse_positive_int(source.get(ENV_MAX), DEFAULT_MAX)
        _window = float(_parse_positive_int(
            source.get(ENV_WINDOW), DEFAULT_WINDOW_SECONDS))


def reset():
    """Clear hit history (tests only)."""
    with _lock:
        _hits.clear()


def allow(bucket, ip, demo_id, now=None):
    """Record one hit. Return True when under the limit, False when refused."""
    key = f'{bucket}|{ip or "-"}|{demo_id or "-"}'
    stamp = time.monotonic() if now is None else float(now)
    with _lock:
        window = _window
        limit = _max
        hits = _hits[key]
        cutoff = stamp - window
        while hits and hits[0] < cutoff:
            hits.popleft()
        if len(hits) >= limit:
            return False
        hits.append(stamp)
        return True


configure()
