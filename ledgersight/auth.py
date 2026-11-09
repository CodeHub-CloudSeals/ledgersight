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
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

SESSION_TTL = timedelta(hours=8)
ACCOUNT_TTL = timedelta(days=14)
INVITE_TTL = timedelta(days=3)
MAX_FAILED = 5
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


def totp_verify(secret: str, code: str) -> bool:
    code = (code or "").strip()
    return any(hmac.compare_digest(totp_code(secret, time.time() + d * 30), code) for d in (-1, 0, 1))


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


class AuthStore:
    def __init__(self):
        self.users: dict[str, User] = {}
        self.invites: dict[str, Invite] = {}
        self.sessions: dict[str, tuple[str, datetime]] = {}

    # invitations
    def invite(self, user_id: str, display_name: str, role: str, workspaces: set[str],
               account_days: int = 14) -> Invite:
        if role not in ("owner_reviewer", "preparer", "support"):
            raise ValueError("unknown role")
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
        generic = PermissionError("invalid credentials")
        if not u or not u.active or u.revoked or not u.pw_hash:
            raise generic
        if u.expires_at < now():
            raise PermissionError("account expired")
        if u.failed >= MAX_FAILED:
            raise PermissionError("account locked; contact support")
        if not (check_pw(password, u.pw_hash) and (not REQUIRE_MFA or totp_verify(u.totp, code))):
            u.failed += 1
            raise generic
        u.failed = 0
        tok = secrets.token_urlsafe(32)
        self.sessions[tok] = (user_id, now() + SESSION_TTL)
        return tok

    # Google sign-in: an administrator still invites the person first (same /v1/invites flow,
    # same roles and workspace scoping as password accounts) - Google only replaces the password +
    # authenticator-code step for that already-registered email. No invite on file, no sign-in.
    def google_login(self, email: str, display_name: str) -> str:
        uid = email.lower()
        u = self.users.get(uid)
        if not u:
            raise PermissionError("no invitation on file for this email; ask your administrator")
        if u.revoked:
            raise PermissionError("account revoked; contact your administrator")
        if u.expires_at < now():
            raise PermissionError("account expired")
        if not u.active:
            u.active, u.terms_accepted_at = True, now()
        u.display_name = display_name or u.display_name
        tok = secrets.token_urlsafe(32)
        self.sessions[tok] = (uid, now() + SESSION_TTL)
        return tok

    def resolve(self, token: str | None) -> User | None:
        if not token or token not in self.sessions:
            return None
        uid, exp = self.sessions[token]
        u = self.users.get(uid)
        if exp < now() or not u or u.revoked or u.expires_at < now():
            self.sessions.pop(token, None)
            return None
        return u

    def logout(self, token: str):
        self.sessions.pop(token, None)

    def revoke(self, user_id: str):
        u = self.users[user_id]
        u.revoked = True
        for t, (uid, _) in list(self.sessions.items()):
            if uid == user_id:
                del self.sessions[t]

    # bootstrap from secret store / env
    def bootstrap(self, workspaces: set[str]):
        uid = os.environ.get("AVOCET_BOOTSTRAP_USER")
        pw = os.environ.get("AVOCET_BOOTSTRAP_PASSWORD")
        secret = os.environ.get("AVOCET_BOOTSTRAP_TOTP")
        if uid and pw and secret:
            self.users[uid] = User(uid, os.environ.get("AVOCET_BOOTSTRAP_NAME", uid), "owner_reviewer",
                                   set(workspaces), hash_pw(pw), secret, True,
                                   expires_at=now() + timedelta(days=30), terms_accepted_at=now())
            return None
        # Local development: print a one-time invite link to stdout.
        inv = self.invite("presenter", "Demo presenter", "owner_reviewer", workspaces)
        return inv
