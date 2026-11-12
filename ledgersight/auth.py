"""Identity for LedgerSight: invitation-only, password + TOTP MFA,
short sessions, account expiry and revocation. No shared reviewer accounts.

In-memory by design (same single-process constraint as the STC demo). The
bootstrap owner is created from AVOCET_BOOTSTRAP_* env vars, so credentials come
from a secret store, never from source.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import struct
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

SESSION_TTL = timedelta(hours=8)
ACCOUNT_TTL = timedelta(days=14)
INVITE_TTL = timedelta(days=3)
MAX_FAILED = 5
LOCK_FOR = timedelta(minutes=15)  # temporary lock after MAX_FAILED wrong attempts
MAX_ACCOUNT_DAYS = 365
# Authenticator-code step. On for any shared/hosted deployment; the local demo script turns it off.
REQUIRE_MFA = os.environ.get("AVOCET_REQUIRE_MFA", "1") != "0"


def now() -> datetime:
    return datetime.now(timezone.utc)


# ---- TOTP (RFC 6238, SHA1, 6 digits, 30 s) --------------------------------
def totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_code(secret: str, t: float | None = None, step: int = 30) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    counter = int((t if t is not None else time.time()) // step)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = digest[-1] & 0x0F
    return f"{(struct.unpack('>I', digest[off:off + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def totp_step(secret: str, code: str) -> int | None:
    """The 30-second step a code belongs to (within one step either side), or None."""
    code = (code or "").strip()
    now_step = int(time.time() // 30)
    for d in (-1, 0, 1):
        if hmac.compare_digest(totp_code(secret, (now_step + d) * 30), code):
            return now_step + d
    return None


def totp_verify(secret: str, code: str) -> bool:
    return totp_step(secret, code) is not None


def otpauth_uri(secret: str, user_id: str) -> str:
    return f"otpauth://totp/LedgerSight:{user_id}?secret={secret}&issuer=LedgerSight"


# ---- passwords ------------------------------------------------------------
def hash_pw(pw: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1)
    return salt.hex() + ":" + h.hex()


def check_pw(pw: str, stored: str) -> bool:
    salt, h = stored.split(":")
    return hmac.compare_digest(hash_pw(pw, bytes.fromhex(salt)).split(":")[1], h)


@dataclass
class User:
    user_id: str
    display_name: str
    role: str  # owner_reviewer | preparer | support
    workspaces: set[str]
    pw_hash: str | None = None
    totp: str | None = None
    active: bool = False
    expires_at: datetime = field(default_factory=lambda: now() + ACCOUNT_TTL)
    revoked: bool = False
    failed: int = 0
    terms_accepted_at: datetime | None = None
    locked_until: datetime | None = None
    last_totp_step: int = 0  # an authenticator code works once

    def public(self):
        return {"user_id": self.user_id, "display_name": self.display_name, "role": self.role,
                "workspaces": sorted(self.workspaces), "expires_at": self.expires_at.isoformat(),
                "active": self.active, "revoked": self.revoked}


@dataclass
class Invite:
    token: str
    user_id: str
    expires_at: datetime
    totp: str
    used: bool = False


def _locked(fn):
    """Requests run on a thread pool, so every change to users, invitations and sessions holds one lock."""
    def wrapper(self, *a, **k):
        with self._lock:
            return fn(self, *a, **k)
    wrapper.__name__, wrapper.__doc__ = fn.__name__, fn.__doc__
    return wrapper


class AuthStore:
    def __init__(self):
        self.users: dict[str, User] = {}
        self.invites: dict[str, Invite] = {}
        self.sessions: dict[str, tuple[str, datetime]] = {}
        self._lock = threading.RLock()

    def _prune_sessions(self):
        """Drop expired sessions (otherwise a session is only removed when its own token comes back)."""
        t = now()
        for tok in [k for k, (_, exp) in self.sessions.items() if exp < t]:
            del self.sessions[tok]

    # invitations
    @_locked
    def invite(self, user_id: str, display_name: str, role: str, workspaces: set[str],
               account_days: int = 14) -> Invite:
        if role not in ("owner_reviewer", "preparer", "support"):
            raise ValueError("unknown role")
        if not 1 <= int(account_days) <= MAX_ACCOUNT_DAYS:
            raise ValueError(f"account_days must be between 1 and {MAX_ACCOUNT_DAYS}")
        for old in self.invites.values():  # a new invitation replaces any earlier one for the same person
            if old.user_id == user_id:
                old.used = True
        u = self.users.get(user_id)
        if u and u.active:
            raise ValueError("user already active")
        self.users[user_id] = User(user_id, display_name, role, set(workspaces),
                                   expires_at=now() + timedelta(days=account_days))
        inv = Invite(secrets.token_urlsafe(24), user_id, now() + INVITE_TTL, totp_secret())
        self.invites[inv.token] = inv
        return inv

    def get_invite(self, token: str) -> Invite:
        inv = self.invites.get(token)
        if not inv or inv.used or inv.expires_at < now():
            raise PermissionError("invitation invalid or expired")
        return inv

    @_locked
    def accept(self, token: str, password: str, code: str, terms: bool) -> User:
        inv = self.get_invite(token)
        if not terms:
            raise ValueError("terms must be accepted")
        if len(password) < 12:
            raise ValueError("password must be at least 12 characters")
        if REQUIRE_MFA and not totp_verify(inv.totp, code):
            raise ValueError("authenticator code incorrect")
        u = self.users[inv.user_id]
        u.pw_hash, u.totp, u.active, u.terms_accepted_at = hash_pw(password), inv.totp, True, now()
        inv.used = True
        return u

    # sessions
    def login(self, user_id: str, password: str, code: str) -> str:
        u = self.users.get(user_id)
        # One message for every failure, so a sign-in attempt never reveals whether an account exists,
        # is locked, expired or revoked.
        generic = PermissionError("Sign-in failed. Check your details and try again. After 5 failed attempts "
                                  "sign-in pauses for 15 minutes.")
        with self._lock:
            if not u or not u.active or u.revoked or not u.pw_hash or u.expires_at < now():
                raise generic
            if u.locked_until and u.locked_until > now():
                raise generic
            if u.locked_until and u.locked_until <= now():  # lock expired: start counting again
                u.locked_until, u.failed = None, 0
            pw_hash, secret = u.pw_hash, u.totp
        # The slow password hash runs outside the lock, so one sign-in never stalls every other request.
        pw_ok = check_pw(password, pw_hash)
        step = totp_step(secret, code) if REQUIRE_MFA else None
        with self._lock:
            # checked again under the lock: two simultaneous sign-ins cannot both spend the same authenticator code
            mfa_ok = not REQUIRE_MFA or (step is not None and step > u.last_totp_step)
            if not (pw_ok and mfa_ok):
                u.failed += 1
                if u.failed >= MAX_FAILED:
                    u.locked_until = now() + LOCK_FOR
                raise generic
            u.failed, u.locked_until = 0, None
            if step is not None:
                u.last_totp_step = step
            self._prune_sessions()
            tok = secrets.token_urlsafe(32)
            self.sessions[tok] = (user_id, now() + SESSION_TTL)
            return tok

    # Google sign-in: any verified account on the allowed domain (checked by the caller) is let in.
    # A never-seen email is auto-provisioned with AVOCET_GOOGLE_DEFAULT_ROLE (default: preparer, least
    # privilege; set it to owner_reviewer to make every Google user a CPA reviewer). An unknown value
    # falls back to preparer. An email already on file (bootstrap owner or a password-account invite)
    # keeps whatever role it was given there.
    GOOGLE_ROLES = ("preparer", "owner_reviewer")

    @_locked
    def google_login(self, email: str, display_name: str, workspaces: set[str]) -> str:
        uid = email.lower()
        u = self.users.get(uid)
        if u and u.revoked:
            raise PermissionError("account revoked; contact your administrator")
        if not u:
            role = os.environ.get("AVOCET_GOOGLE_DEFAULT_ROLE", "preparer")
            role = role if role in self.GOOGLE_ROLES else "preparer"
            reviewers = {e.strip().lower() for e in os.environ.get("AVOCET_GOOGLE_REVIEWERS", "").split(",") if e.strip()}
            if reviewers:  # when an allow-list is set, only those people are CPA reviewers
                role = "owner_reviewer" if uid in reviewers else "preparer"
            u = User(uid, display_name, role, set(workspaces), active=True,
                     expires_at=now() + timedelta(days=365), terms_accepted_at=now())
            self.users[uid] = u
        elif u.expires_at < now():
            raise PermissionError("account expired")
        elif not u.active:
            u.active, u.terms_accepted_at = True, now()
        for inv in self.invites.values():  # an invitation cannot later reset a Google-activated account
            if inv.user_id == uid:
                inv.used = True
        u.display_name = display_name or u.display_name
        self._prune_sessions()
        tok = secrets.token_urlsafe(32)
        self.sessions[tok] = (uid, now() + SESSION_TTL)
        return tok

    @_locked
    def resolve(self, token: str | None) -> User | None:
        if not token or token not in self.sessions:
            return None
        uid, exp = self.sessions[token]
        u = self.users.get(uid)
        if exp < now() or not u or u.revoked or u.expires_at < now():
            self.sessions.pop(token, None)
            return None
        return u

    @_locked
    def logout(self, token: str):
        self.sessions.pop(token, None)

    @_locked
    def unlock(self, user_id: str):
        u = self.users[user_id]
        u.failed, u.locked_until = 0, None

    @_locked
    def revoke(self, user_id: str):
        u = self.users[user_id]
        u.revoked = True
        for t, (uid, _) in list(self.sessions.items()):
            if uid == user_id:
                del self.sessions[t]

    # bootstrap from secret store / env
    @_locked
    def bootstrap(self, workspaces: set[str]):
        uid = os.environ.get("AVOCET_BOOTSTRAP_USER")
        pw = os.environ.get("AVOCET_BOOTSTRAP_PASSWORD")
        secret = os.environ.get("AVOCET_BOOTSTRAP_TOTP")
        if uid and pw and secret:
            old = self.users.get(uid)
            u = User(uid, os.environ.get("AVOCET_BOOTSTRAP_NAME", uid), "owner_reviewer",
                     set(workspaces), hash_pw(pw), secret, True,
                     expires_at=now() + timedelta(days=30), terms_accepted_at=now())
            if old:  # keep a revocation (and an existing later expiry) across restarts
                u.revoked = old.revoked
                u.expires_at = max(u.expires_at, old.expires_at)
            self.users[uid] = u
            return None
        # Local development: print a one-time invite link to stdout.
        inv = self.invite("presenter", "Demo presenter", "owner_reviewer", workspaces)
        return inv
