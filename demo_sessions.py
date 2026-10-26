# START HERE — Gives each browser its own private, temporary demo (its own database and
# logs), identified by a cookie, and deletes demos that have been idle too long.
"""Browser demo selection. Cookie possession grants access to fictional demo data."""
import hashlib
import re
import secrets
import os
import shutil
import time
from http.cookies import SimpleCookie, CookieError
from pathlib import Path
from threading import RLock
from event_log import make_logger

COOKIE_NAME = "payments_demo"
COOKIE_AGE = 365 * 24 * 60 * 60


# FEATURE 01 — Explain a full demo service
# Signal that a new browser workspace cannot be allocated within configured limits.
class DemoCapacityError(Exception):
    """Temporary demo capacity, not a rejected payment."""


# FEATURE 02 — Separate browser workspaces
# Manage temporary databases, cookies and expiry together so one visitor does not see
# another visitor balances or logs.
class DemoStore:
    # FEATURE 03 — Set workspace limits
    # Create the storage folder and load positive session, size, inactivity and payment
    # limits. A lock coordinates simultaneous requests in this process.
    def __init__(self, directory, payments_factory, secure_cookie=False):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.payments_factory = payments_factory
        self.secure_cookie = secure_cookie
        self.lock = RLock()
        self.loaded = {}
        self.active = {}
        # Safety limits, overridable with environment variables: max demos, idle time before
        # deletion (default 1 day), total disk use (default 128 MB), payments per demo.
        self.max_sessions = int(os.environ.get('DEMO_MAX_SESSIONS', '50'))
        self.idle_seconds = int(os.environ.get('DEMO_IDLE_SECONDS', '86400'))
        self.max_bytes = int(os.environ.get('DEMO_MAX_BYTES', '134217728'))
        self.max_payments = int(os.environ.get('DEMO_MAX_PAYMENTS', '1000'))
        if min(self.max_sessions, self.idle_seconds, self.max_bytes, self.max_payments) <= 0:
            raise ValueError('Demo limits must be positive.')

    # FEATURE 04 — Recognize only managed data
    # Consider only correctly named non-symlink folders containing a demo database.
    # Unrelated files must never become cleanup targets.
    def _folders(self):
        return [p for p in self.directory.iterdir() if p.is_dir() and not p.is_symlink()
                and re.fullmatch(r'[0-9a-f]{64}', p.name) and (p / 'payments.sqlite3').is_file()]

    # FEATURE 05 — Remove inactive managed demos
    # Check last-use time when resolving a session, skipping any database currently in
    # use. Expiry deletes the managed demo folder; this is temporary demonstration
    # storage, not durable financial history.
    def _expire(self):
        now = time.time()
        for folder in self._folders():
            marker = folder / '.last-used'
            touched = marker.stat().st_mtime if marker.exists() else (folder / 'payments.sqlite3').stat().st_mtime
            if not self.active.get(folder.name) and now - touched > self.idle_seconds:
                cached = self.loaded.pop(folder.name, None)
                if cached:
                    for handler in cached[1].handlers[:]:
                        handler.close()
                        cached[1].removeHandler(handler)
                shutil.rmtree(folder)

    # FEATURE 06 — Mark a request finished
    # Decrease the active-request count so a later expiry check can reclaim an idle
    # demo.
    def release(self, demo_id):
        with self.lock:
            self.active[demo_id] = max(0, self.active.get(demo_id, 0) - 1)
            if not self.active[demo_id]:
                self.active.pop(demo_id, None)

    # FEATURE 07 — Find or start the browser demo
    # Read the cookie's random token, turn it into a folder name (via a one-way "hash"),
    # and reuse that demo's database. Whoever holds the cookie gets that demo; there is
    # no login. If there's no valid demo: a page view may start a new one (if under the
    # limits), a payment submission is refused. Marks the demo as recently used and
    # "leased" (in use) so cleanup won't delete it until the web layer calls release().
    def resolve(self, cookie_header, create=False):
        # Never use a caller-supplied value as a filesystem path. Only hash a
        # correctly shaped random token, and accept it only for an existing demo.
        cookie = SimpleCookie()
        try:
            cookie.load(cookie_header or "")
            token = cookie[COOKIE_NAME].value if COOKIE_NAME in cookie else ""
        except CookieError:
            token = ""
        with self.lock:
            self._expire()
            demo_id = hashlib.sha256(token.encode()).hexdigest() if re.fullmatch(r"[0-9a-f]{64}", token) else None
            if demo_id is None or not (self.directory / demo_id / "payments.sqlite3").is_file():
                if not create:
                    return None  # A POST must never silently create a replacement demo.
                folders = self._folders()
                size = sum(f.stat().st_size for folder in folders for f in folder.rglob('*') if f.is_file())
                if len(folders) >= self.max_sessions or size >= self.max_bytes:
                    raise DemoCapacityError('Demo capacity reached. Please try again after inactive demos expire.')
                token = secrets.token_hex(32)
                demo_id = hashlib.sha256(token.encode()).hexdigest()
            folder = self.directory / demo_id
            if demo_id not in self.loaded:
                folder.mkdir(parents=True, exist_ok=True)
                payments = self.payments_factory(folder / "payments.sqlite3", max_payments=self.max_payments)
                self.loaded[demo_id] = (payments, make_logger(folder / "logs"))
            payments, logger = self.loaded[demo_id]
            (folder / '.last-used').touch()
            self.active[demo_id] = self.active.get(demo_id, 0) + 1
            # Cookie lasts a year. HttpOnly = page scripts can't read it; SameSite=Strict =
            # other websites can't use it; Secure (online only) = sent over HTTPS only.
            value = f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={COOKIE_AGE}"
            if self.secure_cookie:
                value += "; Secure"
            return demo_id, payments, logger, value

    # FEATURE 08 — Close cached log writers
    # Release open handlers without deleting the stored demo folders.
    def close(self):
        with self.lock:
            for _, logger in self.loaded.values():
                for handler in logger.handlers:
                    handler.close()
            self.loaded.clear()
            self.active.clear()
