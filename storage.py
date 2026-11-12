# START HERE — One connection factory for demo storage (#30).
# Callers still pass the per-visitor SQLite path they use today. If
# TURSO_DATABASE_URL and TURSO_AUTH_TOKEN are both set, the connection
# goes to that one Turso database; otherwise this opens the local file
# with the standard library sqlite3 module, exactly as before.
"""Open a local SQLite file, or a Turso database when both env vars are set."""
import os
import sqlite3
from pathlib import Path

# FEATURE 01 — Name the two Turso settings
# Jeffrey creates the database and token. Platform Engineer stores these on
# Render. Neither value is written in this repository. Both must be set;
# one alone leaves storage on local SQLite.
DATABASE_URL = 'TURSO_DATABASE_URL'
AUTH_TOKEN = 'TURSO_AUTH_TOKEN'


def turso_configured(env=None):
    env = os.environ if env is None else env
    return bool(env.get(DATABASE_URL) and env.get(AUTH_TOKEN))


def _local_marker(database):
    """Keep the per-visitor file as a session marker when data lives on Turso."""
    if not database or str(database) == ':memory:':
        return
    path = Path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix:
        path.touch(exist_ok=True)


# FEATURE 02 — Open one connection
# timeout and other sqlite3 keyword arguments apply only to the local file.
# The Turso client is imported only when it is used, so the local fallback
# does not depend on it at import time.
def connect(database, timeout=5.0, **options):
    if turso_configured():
        import libsql
        _local_marker(database)
        return libsql.connect(database=os.environ[DATABASE_URL],
                              auth_token=os.environ[AUTH_TOKEN])
    return sqlite3.connect(database, timeout=timeout, **options)
