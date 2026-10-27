"""Single-owner authentication: a predefined password and a signed session cookie.

- The password comes from UI_PASSWORD (never stored or logged by the app).
- The session cookie is signed with a random key persisted in the data volume, so sessions
  survive restarts and cannot be forged without that key.
- Failed logins are throttled per client IP and globally (lockout), so the password cannot
  be guessed online.
- State-changing requests must carry a custom header (and a same-origin Origin), which a
  cross-site form or fetch cannot send: CSRF protection on top of SameSite=Lax.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from pathlib import Path
from threading import Lock

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import Settings

COOKIE_NAME = 'mm_session'
CSRF_HEADER = 'x-requested-with'
CSRF_VALUE = 'memory-ui'


def load_or_create_key(path: Path) -> str:
    """Return the persistent signing key, creating it on first start."""
    try:
        if path.exists():
            key = path.read_text().strip()
            if len(key) >= 32:
                return key
    except OSError:
        pass
    key = secrets.token_urlsafe(48)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(key)
        path.chmod(0o600)
    except OSError:
        pass  # read-only volume: fall back to an in-memory key (sessions reset on restart)
    return key


class LoginThrottle:
    """Lock out a client after too many failed logins (and everyone after far too many)."""

    def __init__(self, max_failures: int, lockout_seconds: int):
        self.max_failures = max_failures
        self.lockout_seconds = lockout_seconds
        self._failures: dict[str, list[float]] = {}
        self._lock = Lock()

    def _recent(self, key: str, now: float) -> list[float]:
        window = [t for t in self._failures.get(key, []) if now - t < self.lockout_seconds]
        self._failures[key] = window
        return window

    def retry_after(self, client: str, now: float | None = None) -> int:
        """Seconds the client must wait, or 0 if it may try."""
        now = time.time() if now is None else now
        with self._lock:
            for key, limit in ((client, self.max_failures), ('*', self.max_failures * 4)):
                window = self._recent(key, now)
                if len(window) >= limit:
                    return max(1, int(self.lockout_seconds - (now - window[0])))
        return 0

    def record_failure(self, client: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            for key in (client, '*'):
                self._recent(key, now).append(now)

    def record_success(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)


class Authenticator:
    def __init__(self, settings: Settings):
        self.settings = settings
        key = load_or_create_key(settings.data_dir / 'session.key')
        self._serializer = URLSafeTimedSerializer(key, salt='memory-ui-session')
        self.throttle = LoginThrottle(settings.max_login_failures, settings.lockout_seconds)

    @property
    def max_age(self) -> int:
        return self.settings.session_days * 86400

    def password_matches(self, supplied: str) -> bool:
        if not self.settings.password_configured:
            return False
        # Compare fixed-length digests in constant time.
        a = hashlib.sha256(supplied.encode()).digest()
        b = hashlib.sha256(self.settings.ui_password.encode()).digest()
        return hmac.compare_digest(a, b)

    def issue_session(self) -> str:
        return self._serializer.dumps({'ok': True, 'n': secrets.token_hex(8)})

    def session_valid(self, token: str | None) -> bool:
        if not token:
            return False
        try:
            data = self._serializer.loads(token, max_age=self.max_age)
        except (BadSignature, SignatureExpired):
            return False
        return bool(data.get('ok'))


def csrf_ok(method: str, headers: dict[str, str], host: str) -> bool:
    """Safe methods pass; others need the custom header and a same-origin Origin if present."""
    if method in ('GET', 'HEAD', 'OPTIONS'):
        return True
    if headers.get(CSRF_HEADER) != CSRF_VALUE:
        return False
    origin = headers.get('origin')
    if origin:
        origin_host = origin.split('://', 1)[-1].split('/', 1)[0].lower()
        return origin_host == host.lower()
    return True
