# START HERE — In-process Desk metrics (P1 observability).
# Counters for decide / create / HTTP 503 / stream open+refuse. Values are
# process-local (one Gunicorn worker). Structured metric_recorded log lines
# are meant for an aggregator; /metrics exposes a JSON snapshot for probes.
"""Process-local counters for Desk decide, create, 503, and stream traffic."""
from __future__ import annotations

from collections import Counter
from threading import Lock

from event_log import event

# Counter names (stable for scrapers / alerts).
DECIDE_TOTAL = 'desk_decide_total'
CREATE_TOTAL = 'desk_create_total'
HTTP_503_TOTAL = 'desk_http_503_total'
STREAM_OPEN_TOTAL = 'desk_stream_open_total'
STREAM_REFUSED_TOTAL = 'desk_stream_refused_total'

_KNOWN = frozenset({
    DECIDE_TOTAL, CREATE_TOTAL, HTTP_503_TOTAL,
    STREAM_OPEN_TOTAL, STREAM_REFUSED_TOTAL,
})

_lock = Lock()
_counters: Counter = Counter()


def _key(name, labels):
    return (name, tuple(sorted((str(k), str(v)) for k, v in labels.items())))


def inc(name, **labels):
    """Increment one counter and emit a structured metric_recorded log line."""
    if name not in _KNOWN:
        raise ValueError(f'Unknown metric: {name}')
    clean = {key: value for key, value in labels.items() if value is not None}
    with _lock:
        _counters[_key(name, clean)] += 1
        value = _counters[_key(name, clean)]
    event('metric_recorded', metric=name, value=value, **clean)
    return value


def snapshot():
    """Return {name: [{labels, value}, ...]} sorted for stable JSON."""
    with _lock:
        items = list(_counters.items())
    grouped = {name: [] for name in sorted(_KNOWN)}
    for (name, label_items), value in items:
        grouped[name].append({
            'labels': dict(label_items),
            'value': value,
        })
    for name in grouped:
        grouped[name].sort(key=lambda row: sorted(row['labels'].items()))
    return grouped


def reset():
    """Clear counters (tests only)."""
    with _lock:
        _counters.clear()
