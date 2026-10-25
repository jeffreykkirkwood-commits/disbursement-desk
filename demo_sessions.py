"""Browser demo selection. Cookie possession grants access to fictional demo data."""
import hashlib
import re
import secrets
from http.cookies import SimpleCookie, CookieError
from pathlib import Path
from threading import RLock
from event_log import make_logger

COOKIE_NAME = "payments_demo"
COOKIE_AGE = 365 * 24 * 60 * 60


class DemoStore:
    def __init__(self, directory, payments_factory, secure_cookie=False):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.payments_factory = payments_factory
        self.secure_cookie = secure_cookie
        self.lock = RLock()
        self.loaded = {}

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
            demo_id = hashlib.sha256(token.encode()).hexdigest() if re.fullmatch(r"[0-9a-f]{64}", token) else None
            if demo_id is None or not (self.directory / demo_id / "payments.sqlite3").is_file():
                if not create:
                    return None  # A POST must never silently create a replacement demo.
                token = secrets.token_hex(32)
                demo_id = hashlib.sha256(token.encode()).hexdigest()
            folder = self.directory / demo_id
            if demo_id not in self.loaded:
                folder.mkdir(parents=True, exist_ok=True)
                payments = self.payments_factory(folder / "payments.sqlite3")
                self.loaded[demo_id] = (payments, make_logger(folder / "logs"))
            payments, logger = self.loaded[demo_id]
            # Persistent cookie survives closing a browser. HttpOnly hides it from JS;
            # SameSite limits cross-site use. Secure is for a future HTTPS deployment.
            value = f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={COOKIE_AGE}"
            if self.secure_cookie:
                value += "; Secure"
            return demo_id, payments, logger, value

    def close(self):
        # Called on shutdown/tests, after requests finish, to release log file handles.
        with self.lock:
            for _, logger in self.loaded.values():
                for handler in logger.handlers:
                    handler.close()
            self.loaded.clear()
