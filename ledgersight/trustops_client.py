"""Connection to the TrustOps service (https://trustops.cloudseals.com or a local TrustOps).

Configured with two settings issued by TrustOps (Applications -> Add application):
    TRUSTOPS_URL=https://trustops.cloudseals.com
    TRUSTOPS_KEY=tok_...

When both are set, LedgerSight asks TrustOps before agent actions, records forbidden attempts there, and sends
exceptions to TrustOps for approval (approval happens only in TrustOps). When they are not set, LedgerSight runs
with its local policy gate only, as before.

Fail closed: if TrustOps cannot be reached, the answer is DENY.
"""
from __future__ import annotations

import hmac
import json
import os
import time
import urllib.error
import urllib.request

TIMEOUT = float(os.environ.get("TRUSTOPS_TIMEOUT", "6"))
_status_cache: dict[str, tuple[float, dict]] = {}


def url() -> str:
    return os.environ.get("TRUSTOPS_URL", "").strip().rstrip("/")


def key() -> str:
    return os.environ.get("TRUSTOPS_KEY", "").strip()


def configured() -> bool:
    return bool(url() and key())


def console_link(path: str = "") -> str | None:
    return f"{url()}/portal/{path}" if url() else None


def _call(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(url() + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "X-TrustOps-Key": key(),
                                          "User-Agent": "LedgerSight"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:   # noqa: S310 - fixed https origin from settings
        return json.loads(r.read() or b"{}")


def decide(actor_type: str, actor_id: str, action: str, **ctx) -> dict:
    """Ask TrustOps. Returns {decision, policy_id, reason, decision_id}. Unreachable -> DENY (fail closed)."""
    body = {"actor": {"type": actor_type, "id": actor_id}, "action": action,
            **{k: v for k, v in ctx.items() if v is not None}}
    try:
        return _call("POST", "/v1/decisions", body)
    except urllib.error.HTTPError as e:
        code = e.code
        return {"decision": "DENY", "policy_id": "TRUSTOPS_UNAVAILABLE",
                "reason": "TrustOps rejected the LedgerSight key" if code == 401 else f"TrustOps answered HTTP {code}"}
    except Exception:
        return {"decision": "DENY", "policy_id": "TRUSTOPS_UNAVAILABLE", "reason": "TrustOps could not be reached"}


def status(decision_id: str, max_age: float = 8.0) -> dict | None:
    """Final state of an escalated item: APPROVED | REJECTED | PENDING. None if TrustOps cannot be reached."""
    hit = _status_cache.get(decision_id)
    if hit and time.monotonic() - hit[0] < max_age:
        return hit[1]
    try:
        out = _call("GET", f"/v1/decisions/{decision_id}/status")
    except Exception:
        return None
    _status_cache[decision_id] = (time.monotonic(), out)
    return out


_access_cache: dict[str, tuple[float, str | None]] = {}
UNREACHABLE = "__unreachable__"


def app_access(email: str, max_age: float = 60.0) -> str | None:
    """Role this person has in this application, as set in TrustOps Team & access ('preparer' | 'reviewer'),
    None if they have no access, or UNREACHABLE if TrustOps cannot be asked."""
    email = (email or "").strip().lower()
    hit = _access_cache.get(email)
    if hit and time.monotonic() - hit[0] < max_age:
        return hit[1]
    try:
        from urllib.parse import quote
        role = _call("GET", f"/v1/app-access?email={quote(email)}").get("role")
    except Exception:
        return UNREACHABLE
    _access_cache[email] = (time.monotonic(), role)
    return role


def valid_key(presented: str | None) -> bool:
    """Requests from TrustOps (Load details) carry the same key."""
    return bool(key()) and bool(presented) and hmac.compare_digest(presented, key())
