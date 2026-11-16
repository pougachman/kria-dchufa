"""Curator accounts: password hashing and signed session cookies.

Passwords are hashed with scrypt (Python standard library) and a random salt. The session
cookie holds the curator id and an expiry, signed with a server secret the app creates for
itself on first use (table app_secrets), so no extra environment variable is needed. The
signature also covers the curator's current password hash: changing the password signs out
every other session.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

from fastapi import Request
from sqlalchemy.orm import Session

from kria.db.models import AppSecret, Curator

COOKIE = "kria_curator"
MAX_AGE = 30 * 24 * 3600
SCRYPT = {"n": 2**14, "r": 8, "p": 1}
MIN_PASSWORD = 10


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_b64, digest_b64 = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    salt, digest = base64.b64decode(salt_b64), base64.b64decode(digest_b64)
    candidate = hashlib.scrypt(password.encode(), salt=salt, dklen=len(digest), **SCRYPT)
    return hmac.compare_digest(candidate, digest)


def temporary_password() -> str:
    """Easy to read aloud or type from a message: four groups of four letters and digits."""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(4))


def password_problem(password: str) -> str | None:
    if len(password) < MIN_PASSWORD:
        return f"הסיסמה צריכה להיות באורך {MIN_PASSWORD} תווים לפחות"
    return None


def server_secret(session: Session) -> bytes:
    row = session.get(AppSecret, "session_key")
    if row is None:
        row = AppSecret(name="session_key", value=secrets.token_hex(32))
        session.add(row)
        session.commit()
    return bytes.fromhex(row.value)


def _sign(session: Session, curator: Curator, expires: int) -> str:
    msg = f"{curator.id}.{expires}.{curator.password_hash}".encode()
    return hmac.new(server_secret(session), msg, hashlib.sha256).hexdigest()


def make_cookie(session: Session, curator: Curator) -> str:
    expires = int(time.time()) + MAX_AGE
    return f"{curator.id}.{expires}.{_sign(session, curator, expires)}"


def current_curator(request: Request, session: Session) -> Curator | None:
    token = request.cookies.get(COOKIE, "")
    parts = token.split(".")
    if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    curator_id, expires, sig = int(parts[0]), int(parts[1]), parts[2]
    if expires < time.time():
        return None
    curator = session.get(Curator, curator_id)
    if curator is None or not curator.active:
        return None
    if not hmac.compare_digest(_sign(session, curator, expires), sig):
        return None
    return curator
