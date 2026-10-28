"""Accounts, sessions and CSRF for the studio.

Why this module exists: the studio used to be reachable only from ``127.0.0.1``, so it had no
authentication at all. Stage NET-1 puts it on the home LAN, where "can reach the port" can no longer
mean "may drive the GPU". Everything that decides *who* is asking lives here:

* passwords — argon2id (``argon2-cffi``), the library's RFC-9106 defaults, never logged, never
  returned by any endpoint, never written anywhere except the ``users`` table;
* sessions — an opaque 32-byte token in an ``httpOnly`` cookie. The database stores only its
  SHA-256, so a copy of ``studio.db`` cannot be replayed as a live session;
* CSRF — a per-session token, returned to the SPA by ``GET /api/auth/me`` and required in the
  ``X-CSRF-Token`` header on every unsafe method. Server-side session cookies plus ``SameSite=Lax``
  already block most cross-site posts; this stops the rest;
* roles — ``admin`` and ``user``. Admin-only surfaces are wired in ``api.py`` via ``require_admin``.

The first admin is created from the command line (``python -m app.auth adduser ...``) so that no
unauthenticated HTTP request can ever bootstrap an account.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import logging
import re
import secrets
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request, status

from .paths import Layout

log = logging.getLogger(__name__)

SESSION_COOKIE = "studio_session"
CSRF_COOKIE = "studio_csrf"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
ROLES = ("admin", "user")
MIN_PASSWORD_LEN = 8

_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
_USER_ID_RE = re.compile(r"^u_[0-9a-f]{12}$")

# argon2id with the library defaults: ~64 MiB and ~0.1 s per hash on this laptop. That is a
# deliberate cost — a family member logging in once a day will not notice it, an offline attacker
# guessing passwords out of a stolen database will.
_hasher = PasswordHasher()


class AuthError(Exception):
    """Credential or account problem that is safe to show to the user."""


# --------------------------------------------------------------------------- passwords


def hash_password(password: str) -> str:
    if len(password or "") < MIN_PASSWORD_LEN:
        raise AuthError(f"password must be at least {MIN_PASSWORD_LEN} characters")
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    if not password_hash:
        return False
    try:
        return _hasher.verify(password_hash, password or "")
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


# --------------------------------------------------------------------------- users


def normalize_username(username: str) -> str:
    name = (username or "").strip().lower()
    if not _USERNAME_RE.match(name):
        raise AuthError(
            "username must be 2-32 characters: lowercase letters, digits, dot, dash, underscore"
        )
    return name


def new_user_id() -> str:
    """Random, filesystem-safe identity — it becomes ``output/users/<id>/``."""
    return "u_" + secrets.token_hex(6)


def valid_user_id(user_id: str) -> bool:
    return bool(_USER_ID_RE.match(user_id or ""))


def create_user(db: Any, username: str, password: str, *, role: str = "user",
                display_name: str = "") -> dict[str, Any]:
    name = normalize_username(username)
    if role not in ROLES:
        raise AuthError(f"role must be one of {ROLES}")
    if db.user_by_username(name) is not None:
        raise AuthError(f"user {name!r} already exists")
    return db.create_user(
        user_id=new_user_id(),
        username=name,
        display_name=display_name or name,
        role=role,
        password_hash=hash_password(password),
    )


def set_password(db: Any, user_id: str, password: str) -> None:
    db.update_user(user_id, password_hash=hash_password(password))
    # A password change must invalidate every device that was already logged in.
    db.delete_user_sessions(user_id)


def authenticate(db: Any, username: str, password: str) -> dict[str, Any] | None:
    """Return the user row on success. Constant-ish work whether or not the name exists."""
    try:
        name = normalize_username(username)
    except AuthError:
        name = ""
    user = db.user_by_username(name) if name else None
    if user is None:
        # Hash a throwaway value so a missing user does not answer measurably faster than a wrong
        # password, and so the response time does not enumerate accounts.
        _hasher.hash(password or "x")
        return None
    if not verify_password(user["password_hash"], password):
        return None
    if user.get("disabled"):
        return None
    if needs_rehash(user["password_hash"]):
        try:
            db.update_user(user["id"], password_hash=hash_password(password))
        except AuthError:
            pass
    db.update_user(user["id"], last_login_at=utcnow())
    return db.get_user(user["id"])


# --------------------------------------------------------------------------- sessions


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def token_id(token: str) -> str:
    """The value stored as a session primary key: never the cookie itself."""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def start_session(db: Any, user_id: str, ttl_hours: float, *, client_ip: str | None = None,
                  user_agent: str | None = None) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    expires = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).isoformat(timespec="seconds")
    db.create_session(session_id=token_id(token), user_id=user_id, csrf=csrf,
                      expires_at=expires, client_ip=client_ip, user_agent=user_agent)
    return token, csrf


def end_session(db: Any, token: str | None) -> None:
    if token:
        db.delete_session(token_id(token))


@dataclass(frozen=True)
class Principal:
    """The authenticated caller of the current request."""

    id: str
    username: str
    display_name: str
    role: str
    csrf: str
    session_id: str
    token: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role,
            "is_admin": self.is_admin,
            "csrf_token": self.csrf,
        }


# --------------------------------------------------------------------------- login throttle


class LoginThrottle:
    """In-memory failure counter: N failures per (username, IP) inside a window ⇒ cooldown.

    Deliberately not persisted: a restart clearing the counters is acceptable on a home LAN, and
    keeping it out of the database means a flood of bad logins cannot grow the file.
    """

    def __init__(self, max_attempts: int = 5, window_s: float = 600.0, cooldown_s: float = 300.0):
        self.max_attempts = max_attempts
        self.window_s = window_s
        self.cooldown_s = cooldown_s
        self._failures: dict[str, list[float]] = {}
        self._blocked_until: dict[str, float] = {}

    def _key(self, username: str, ip: str | None) -> str:
        return f"{(username or '').lower()}|{ip or '-'}"

    def check(self, username: str, ip: str | None) -> float:
        """Seconds the caller must wait, 0 if it may try now."""
        key = self._key(username, ip)
        until = self._blocked_until.get(key, 0.0)
        remaining = until - time.monotonic()
        return max(0.0, remaining)

    def fail(self, username: str, ip: str | None) -> None:
        key = self._key(username, ip)
        now = time.monotonic()
        recent = [t for t in self._failures.get(key, []) if now - t < self.window_s]
        recent.append(now)
        self._failures[key] = recent
        if len(recent) >= self.max_attempts:
            self._blocked_until[key] = now + self.cooldown_s
            self._failures[key] = []

    def reset(self, username: str, ip: str | None) -> None:
        key = self._key(username, ip)
        self._failures.pop(key, None)
        self._blocked_until.pop(key, None)


def client_ip(request: Request | None) -> str | None:
    """Caller address. No proxy sits in front of the studio in NET-1, so this is the socket peer."""
    if request is None or request.client is None:
        return None
    return request.client.host


# --------------------------------------------------------------------------- FastAPI deps


def _state(request: Request) -> Any:
    return request.app.state.studio


def get_current_user(request: Request) -> Principal:
    """Authenticate the request, enforcing CSRF on unsafe methods.

    Every data route depends on this, so an endpoint cannot accidentally be left open: forgetting
    the dependency is impossible to spot in review, but forgetting it would also disable the
    parameter the handler needs for ownership checks.
    """
    state = _state(request)
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise _unauthorized("authentication required")
    session = state.db.get_session(token_id(token))
    if session is None:
        raise _unauthorized("session expired")
    if session["expires_at"] < utcnow():
        state.db.delete_session(session["id"])
        raise _unauthorized("session expired")
    user = state.db.get_user(session["user_id"])
    if user is None or user.get("disabled"):
        state.db.delete_session(session["id"])
        raise _unauthorized("account disabled")

    if request.method not in SAFE_METHODS:
        sent = request.headers.get(CSRF_HEADER) or request.headers.get("X-CSRF-Token") or ""
        if not hmac.compare_digest(sent, session["csrf"]):
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "CSRF token missing or invalid")

    # Sliding expiry, written at most once every five minutes to keep SQLite quiet.
    try:
        last = datetime.fromisoformat(session["last_seen"])
        if (datetime.now(timezone.utc) - last).total_seconds() > 300:
            state.db.touch_session(session["id"])
    except (TypeError, ValueError):
        state.db.touch_session(session["id"])

    return Principal(
        id=user["id"],
        username=user["username"],
        display_name=user["display_name"] or user["username"],
        role=user["role"],
        csrf=session["csrf"],
        session_id=session["id"],
        token=token,
    )


def require_admin(user: Principal = Depends(get_current_user)) -> Principal:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin role required")
    return user


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail,
                         headers={"WWW-Authenticate": "Session"})


# --------------------------------------------------------------------------- CLI


def _open_db(layout: Layout) -> Any:
    from .db import Database

    layout.ensure()
    return Database(layout.db_path)


def _prompt_password(confirm: bool = True) -> str:
    first = getpass.getpass("password: ")
    if confirm:
        again = getpass.getpass("repeat: ")
        if first != again:
            raise SystemExit("passwords do not match")
    return first


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.auth",
        description="Manage studio accounts (argon2id). Run on the server itself.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("adduser", help="create an account")
    p_add.add_argument("username")
    p_add.add_argument("--role", choices=ROLES, default="user")
    p_add.add_argument("--display-name", default="")
    p_add.add_argument("--password-stdin", action="store_true",
                       help="read the password from stdin (for scripts; prefer the prompt)")

    p_pw = sub.add_parser("passwd", help="change a password and revoke its sessions")
    p_pw.add_argument("username")
    p_pw.add_argument("--password-stdin", action="store_true")

    for name, help_text in (("disable", "block login"), ("enable", "allow login again"),
                            ("deluser", "delete the account (outputs are kept on disk)")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("username")

    p_role = sub.add_parser("setrole", help="change role")
    p_role.add_argument("username")
    p_role.add_argument("role", choices=ROLES)

    sub.add_parser("listusers", help="list accounts")
    sub.add_parser("purge-sessions", help="delete expired sessions")

    args = parser.parse_args(argv)
    layout = Layout()
    db = _open_db(layout)
    try:
        if args.cmd == "adduser":
            password = sys.stdin.readline().rstrip("\n") if args.password_stdin else _prompt_password()
            user = create_user(db, args.username, password, role=args.role,
                               display_name=args.display_name)
            print(f"created {user['username']} ({user['role']}) id={user['id']}")
        elif args.cmd == "passwd":
            user = _require_user(db, args.username)
            password = sys.stdin.readline().rstrip("\n") if args.password_stdin else _prompt_password()
            set_password(db, user["id"], password)
            print(f"password updated for {user['username']}; sessions revoked")
        elif args.cmd == "disable":
            user = _require_user(db, args.username)
            db.update_user(user["id"], disabled=1)
            db.delete_user_sessions(user["id"])
            print(f"disabled {user['username']}")
        elif args.cmd == "enable":
            user = _require_user(db, args.username)
            db.update_user(user["id"], disabled=0)
            print(f"enabled {user['username']}")
        elif args.cmd == "setrole":
            user = _require_user(db, args.username)
            db.update_user(user["id"], role=args.role)
            print(f"{user['username']} is now {args.role}")
        elif args.cmd == "deluser":
            user = _require_user(db, args.username)
            db.delete_user(user["id"])
            print(f"deleted {user['username']} (media under output/users/{user['id']}/ kept)")
        elif args.cmd == "listusers":
            rows = db.list_users()
            if not rows:
                print("no users yet — create one with: python -m app.auth adduser <name> --role admin")
            for row in rows:
                flag = " (disabled)" if row["disabled"] else ""
                print(f"{row['id']}  {row['username']:<16} {row['role']:<6}{flag}"
                      f"  created {row['created_at'][:10]}")
        elif args.cmd == "purge-sessions":
            print(f"purged {db.purge_sessions()} expired sessions")
    except AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()
    return 0


def _require_user(db: Any, username: str) -> dict[str, Any]:
    user = db.user_by_username((username or "").strip().lower())
    if user is None:
        raise AuthError(f"no such user: {username}")
    return user


if __name__ == "__main__":
    raise SystemExit(cli())
