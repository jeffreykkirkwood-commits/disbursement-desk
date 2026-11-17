# START HERE — Shared stream-slot store for multi-worker Gunicorn (P1).
# Cap concurrent /payouts/stream holds so /health stays reachable across
# workers on one host. Default file backend uses flock + lease tokens under
# DEMO_DIRECTORY; memory backend keeps the in-process counter for unit tests.
"""Cross-process stream slot leases (file) or in-process counter (memory)."""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from uuid import uuid4

try:
    import fcntl
except ImportError:  # pragma: no cover - Desk deploys on Linux
    fcntl = None

BACKEND_ENV = 'STREAM_SLOT_BACKEND'
BACKEND_MEMORY = 'memory'
BACKEND_FILE = 'file'
BACKENDS = frozenset({BACKEND_MEMORY, BACKEND_FILE})
# Leases expire after HOLD_SECONDS plus grace so a crashed worker cannot
# permanently exhaust the global cap.
DEFAULT_LEASE_GRACE_SECONDS = 30


class StreamSlotStore:
    """Acquire returns a token (truthy) or None; release frees that token."""

    def acquire(self):
        raise NotImplementedError

    def release(self, token=None):
        raise NotImplementedError

    def reset(self):
        raise NotImplementedError

    def active_count(self):
        raise NotImplementedError


class MemoryStreamSlots(StreamSlotStore):
    """Process-local counter — same semantics as the original #83 gate."""

    def __init__(self, max_streams, lock):
        self.max_streams = max_streams
        self._lock = lock
        self._count = 0

    def acquire(self):
        with self._lock:
            if self._count >= self.max_streams:
                return None
            self._count += 1
            return f'memory:{self._count}:{uuid4().hex}'

    def release(self, token=None):
        with self._lock:
            if self._count > 0:
                self._count -= 1

    def reset(self):
        with self._lock:
            self._count = 0

    def active_count(self):
        with self._lock:
            return self._count


class FileStreamSlots(StreamSlotStore):
    """flock-backed leases shared by every worker that mounts the same directory."""

    def __init__(self, directory, max_streams, lease_seconds):
        if fcntl is None:
            raise RuntimeError('File stream slots require fcntl (Linux).')
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'stream_slots.json'
        self.lock_path = self.directory / 'stream_slots.lock'
        self.max_streams = max_streams
        self.lease_seconds = lease_seconds
        self._thread = Lock()

    @contextmanager
    def _locked(self):
        # Thread lock first so one process does not nest flock from two threads.
        with self._thread:
            self.lock_path.touch(exist_ok=True)
            with open(self.lock_path, 'a+', encoding='utf-8') as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _load(self):
        if not self.path.is_file():
            return {'slots': []}
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {'slots': []}
        slots = data.get('slots') if isinstance(data, dict) else None
        if not isinstance(slots, list):
            return {'slots': []}
        clean = []
        for row in slots:
            if (isinstance(row, dict) and isinstance(row.get('id'), str)
                    and isinstance(row.get('at'), (int, float))):
                clean.append({'id': row['id'], 'at': float(row['at'])})
        return {'slots': clean}

    def _save(self, state):
        tmp = self.path.with_name(self.path.name + '.tmp')
        tmp.write_text(json.dumps(state, separators=(',', ':')), encoding='utf-8')
        tmp.replace(self.path)

    def _expire(self, state, now):
        cutoff = now - self.lease_seconds
        state['slots'] = [row for row in state['slots'] if row['at'] >= cutoff]

    def acquire(self):
        token = uuid4().hex
        now = time.time()
        with self._locked():
            state = self._load()
            self._expire(state, now)
            if len(state['slots']) >= self.max_streams:
                return None
            state['slots'].append({'id': token, 'at': now})
            self._save(state)
            return token

    def release(self, token=None):
        if not token:
            return
        with self._locked():
            state = self._load()
            state['slots'] = [row for row in state['slots'] if row['id'] != token]
            self._save(state)

    def reset(self):
        with self._locked():
            self._save({'slots': []})

    def active_count(self):
        now = time.time()
        with self._locked():
            state = self._load()
            self._expire(state, now)
            self._save(state)
            return len(state['slots'])


def backend_name(env=None):
    env = os.environ if env is None else env
    raw = (env.get(BACKEND_ENV) or BACKEND_FILE).strip().lower()
    if raw not in BACKENDS:
        raise ValueError(f'{BACKEND_ENV} must be one of: {", ".join(sorted(BACKENDS))}.')
    return raw


def build_store(directory, max_streams, hold_seconds, env=None, memory_lock=None):
    """Choose memory or file store. File path is directory/_stream_slots/."""
    name = backend_name(env)
    if name == BACKEND_MEMORY:
        if memory_lock is None:
            raise ValueError('memory stream slots need a threading.Lock')
        return MemoryStreamSlots(max_streams, memory_lock)
    lease = hold_seconds + int(os.environ.get(
        'STREAM_SLOT_LEASE_GRACE_SECONDS', str(DEFAULT_LEASE_GRACE_SECONDS)))
    return FileStreamSlots(Path(directory) / '_stream_slots', max_streams, lease)
