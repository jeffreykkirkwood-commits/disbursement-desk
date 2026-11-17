# START HERE — One connection factory for demo storage (#30, P1 tenancy).
# Callers still pass the per-visitor SQLite path they use today. Local SQLite
# keeps each visitor isolated by file. A shared TURSO_DATABASE_URL across
# visitors is refused unless tenancy is configured (per-tenant URL template
# or an explicit single-tenant shared allow).
"""Open a local SQLite file, or a Turso database with proven tenant isolation."""
import os
import re
import sqlite3
import threading
from pathlib import Path

# FEATURE 01 — Name the two Turso settings
# Jeffrey creates the database and token. Platform Engineer stores these on
# Render. Neither value is written in this repository. Both must be set;
# one alone leaves storage on local SQLite.
DATABASE_URL = 'TURSO_DATABASE_URL'
AUTH_TOKEN = 'TURSO_AUTH_TOKEN'
# Tenancy for Turso: unset/refuse (default), shared (single-tenant only),
# or template (URL must contain {tenant}).
TENANT_MODE = 'TURSO_TENANT_MODE'
ALLOW_SHARED = 'TURSO_ALLOW_SHARED'
MODE_REFUSE = 'refuse'
MODE_SHARED = 'shared'
MODE_TEMPLATE = 'template'
TENANT_MODES = frozenset({MODE_REFUSE, MODE_SHARED, MODE_TEMPLATE})
# Test-only switch: when set to libsql-local, connect() uses the libsql
# client on the local file through the adapter, with no network. Used so
# the existing unittest job can exercise the Turso client path.
TEST_BACKEND = 'STORAGE_TEST_BACKEND'
LIBSQL_LOCAL = 'libsql-local'
DEMO_TENANT = re.compile(r'^[0-9a-f]{64}$')


class StorageConfigError(RuntimeError):
    """Turso/tenancy misconfiguration — refuse to open a shared multi-visitor DB."""


# libsql/pyo3 can hold the GIL while waiting for a file lock, so a second
# thread that also needs the database cannot commit. Serialize libsql use
# in this process; one Gunicorn worker is the hosted setup already.
_LIBSQL_GATE = threading.RLock()


def _env_value(env, name):
    value = env.get(name)
    return value.strip() if isinstance(value, str) else ''


def turso_configured(env=None):
    env = os.environ if env is None else env
    return bool(_env_value(env, DATABASE_URL) and _env_value(env, AUTH_TOKEN))


def tenant_mode(env=None):
    env = os.environ if env is None else env
    raw = _env_value(env, TENANT_MODE).lower() or MODE_REFUSE
    if raw not in TENANT_MODES:
        raise StorageConfigError(
            f'{TENANT_MODE} must be one of: {", ".join(sorted(TENANT_MODES))}.')
    return raw


def tenant_from_path(database):
    """Derive the visitor tenant id from .../<64-hex>/payments.sqlite3 demo paths."""
    if not database or str(database) == ':memory:':
        return None
    path = Path(database)
    if path.name == 'payments.sqlite3' and DEMO_TENANT.fullmatch(path.parent.name):
        return path.parent.name
    return None


def resolve_turso_url(database, tenant_id=None, env=None):
    """Return the Turso URL for this connection, or raise if sharing would cross tenants."""
    env = os.environ if env is None else env
    base = _env_value(env, DATABASE_URL)
    mode = tenant_mode(env)
    tenant = tenant_id or tenant_from_path(database)
    if mode == MODE_TEMPLATE:
        if '{tenant}' not in base:
            raise StorageConfigError(
                f'{DATABASE_URL} must include {{tenant}} when {TENANT_MODE}={MODE_TEMPLATE}.')
        if not tenant:
            raise StorageConfigError(
                'A demo tenant id is required for per-tenant Turso URLs.')
        return base.replace('{tenant}', tenant)
    if mode == MODE_SHARED:
        if _env_value(env, ALLOW_SHARED) != '1':
            raise StorageConfigError(
                f'Set {ALLOW_SHARED}=1 only for a single-tenant deploy that intentionally '
                f'shares one Turso database. Multi-visitor demos must unset Turso or use '
                f'{TENANT_MODE}={MODE_TEMPLATE}.')
        return base
    raise StorageConfigError(
        f'{DATABASE_URL} is set but tenancy is not configured. '
        f'Unset Turso for per-visitor local SQLite, or set {TENANT_MODE}={MODE_TEMPLATE} '
        f'with {{tenant}} in the URL, or {TENANT_MODE}={MODE_SHARED} with {ALLOW_SHARED}=1 '
        f'for a deliberate single-tenant database.')


def _local_marker(database):
    """Keep the per-visitor file as a session marker when data lives on Turso."""
    if not database or str(database) == ':memory:':
        return
    path = Path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix:
        path.touch(exist_ok=True)


def _translate(exc):
    """Map libsql ValueError (and libsql.Error) onto sqlite3 exception types."""
    if isinstance(exc, sqlite3.Error):
        return exc
    module = getattr(type(exc), '__module__', '')
    if not isinstance(exc, ValueError) and module != 'libsql':
        return exc
    message = str(exc)
    if 'constraint' in message.lower():
        return sqlite3.IntegrityError(message)
    return sqlite3.OperationalError(message)


def _run(action, *args, **kwargs):
    try:
        return action(*args, **kwargs)
    except Exception as exc:
        mapped = _translate(exc)
        if mapped is exc:
            raise
        raise mapped from exc


# FEATURE 02 — Wrap the libsql cursor
# libsql's cursor has fetchone/fetchall but is not iterable, and the app
# iterates db.execute(...) in several places. Buffer rows so a for-loop
# and fetchone/fetchall see the same results.
class LibsqlCursor:
    def __init__(self, cursor):
        self._cursor = cursor
        self._rows = None
        self._index = 0

    def _buffer(self):
        if self._rows is None:
            self._rows = list(_run(lambda: self._cursor.fetchall() or ()))
            self._index = 0
        return self._rows

    def fetchall(self):
        rows = self._buffer()[self._index:]
        self._index = len(self._rows)
        return rows

    def fetchone(self):
        rows = self._buffer()
        if self._index >= len(rows):
            return None
        row = rows[self._index]
        self._index += 1
        return row

    def fetchmany(self, size=None):
        if size is None:
            size = getattr(self._cursor, 'arraysize', 1) or 1
        rows = self._buffer()
        out = rows[self._index:self._index + size]
        self._index += len(out)
        return out

    def __iter__(self):
        return iter(self.fetchall())

    def execute(self, sql, parameters=()):
        self._rows = None
        self._index = 0
        _run(self._cursor.execute, sql, parameters)
        return self

    def executemany(self, sql, seq_of_parameters):
        self._rows = None
        self._index = 0
        _run(self._cursor.executemany, sql, seq_of_parameters)
        return self

    def close(self):
        closer = getattr(self._cursor, 'close', None)
        if closer is not None:
            _run(closer)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


# FEATURE 03 — Wrap the libsql connection
# execute/executemany return the iterable cursor above, and libsql errors
# become sqlite3.OperationalError or IntegrityError so existing handlers
# (for example hosted_app.py catching sqlite3.Error) keep working.
class LibsqlConnection:
    def __init__(self, connection):
        self._conn = connection
        self._held = True

    def _release(self):
        if self._held:
            self._held = False
            _LIBSQL_GATE.release()

    def __del__(self):
        try:
            self._release()
        except Exception:
            pass

    def execute(self, sql, parameters=()):
        return LibsqlCursor(_run(self._conn.execute, sql, parameters))

    def executemany(self, sql, seq_of_parameters):
        return LibsqlCursor(_run(self._conn.executemany, sql, seq_of_parameters))

    def executescript(self, sql):
        return LibsqlCursor(_run(self._conn.executescript, sql))

    def cursor(self):
        return LibsqlCursor(_run(self._conn.cursor))

    def commit(self):
        return _run(self._conn.commit)

    def rollback(self):
        return _run(self._conn.rollback)

    def close(self):
        try:
            return _run(self._conn.close)
        finally:
            self._release()

    def __enter__(self):
        _run(self._conn.__enter__)
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            return _run(self._conn.__exit__, exc_type, exc, tb)
        finally:
            self._release()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _libsql_connect(database, auth_token=None, timeout=5.0):
    import libsql
    kwargs = {'database': str(database), 'timeout': timeout}
    if auth_token is not None:
        kwargs['auth_token'] = auth_token
    _LIBSQL_GATE.acquire()
    try:
        return LibsqlConnection(_run(libsql.connect, **kwargs))
    except Exception:
        _LIBSQL_GATE.release()
        raise


# FEATURE 04 — Open one connection
# timeout and other sqlite3 keyword arguments apply only to the local
# sqlite3 file. The Turso client is imported only when it is used, so the
# local sqlite3 fallback does not depend on it at import time.
# tenant_id is optional; demo paths under <64-hex>/payments.sqlite3 supply it.
def connect(database, timeout=5.0, tenant_id=None, **options):
    if os.environ.get(TEST_BACKEND) == LIBSQL_LOCAL:
        return _libsql_connect(database, timeout=timeout)
    if turso_configured():
        url = resolve_turso_url(database, tenant_id=tenant_id)
        _local_marker(database)
        return _libsql_connect(url,
                               auth_token=_env_value(os.environ, AUTH_TOKEN),
                               timeout=timeout)
    return sqlite3.connect(database, timeout=timeout, **options)
