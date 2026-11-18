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
_status_cache: dict[str, tuple[float, dict | None]] = {}

# 2026-11-16: circuit breaker. After TrustOps could not be reached (connection refused, DNS, timeout), further calls
# are not attempted for BREAKER_SECONDS: they fail at once with the same "unreachable" result, so pages and the bell
# do not wait TIMEOUT seconds per call. Decisions stay fail-closed (unreachable = DENY). An HTTP answer (even 4xx/5xx)
# means TrustOps is up and does not open the breaker.
BREAKER_SECONDS = float(os.environ.get("TRUSTOPS_BREAKER_SECONDS", "15"))
FAILED_STATUS_SECONDS = 10.0
_breaker = {"open_until": 0.0}


class Unreachable(Exception):
    """TrustOps could not be reached (or the breaker is open)."""


def breaker_open() -> bool:
    return time.monotonic() < _breaker["open_until"]


def reset_breaker() -> None:
    _breaker["open_until"] = 0.0


def url() -> str:
    return os.environ.get("TRUSTOPS_URL", "").strip().rstrip("/")


def key() -> str:
    return os.environ.get("TRUSTOPS_KEY", "").strip()


def configured() -> bool:
    return bool(url() and key())


def console_link(path: str = "") -> str | None:
    return f"{url()}/portal/{path}" if url() else None


def _call(method: str, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
    if breaker_open():
        raise Unreachable("TrustOps could not be reached")
    req = urllib.request.Request(url() + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "X-TrustOps-Key": key(),
                                          "User-Agent": "LedgerSight"})
    try:
        with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as r:   # noqa: S310 - fixed https origin from settings
            out = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError:
        raise                                       # TrustOps answered: it is up
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        _breaker["open_until"] = time.monotonic() + BREAKER_SECONDS
        raise Unreachable("TrustOps could not be reached") from e
    _breaker["open_until"] = 0.0
    return out


def decide(actor_type: str, actor_id: str, action: str, **ctx) -> dict:
    """Ask TrustOps. Returns {decision, policy_id, reason, decision_id}. Unreachable -> DENY (fail closed)."""
    body = {"actor": {"type": actor_type, "id": actor_id}, "action": action,
            **{k: v for k, v in ctx.items() if v is not None}}
    try:
        return _call("POST", "/v1/decisions", body)
    except urllib.error.HTTPError as e:
        code = e.code
        err, msg = _error_body(e)
        if code == 401:
            return {"decision": "DENY", "policy_id": "TRUSTOPS_UNAVAILABLE", "reason": "TrustOps rejected the LedgerSight key"}
        # TrustOps refused the request itself (e.g. 422 not_a_reviewer for the chosen approver): pass its words on.
        return {"decision": "DENY", "policy_id": "TRUSTOPS_REFUSED" if 400 <= code < 500 else "TRUSTOPS_UNAVAILABLE",
                "error": err, "reason": msg or f"TrustOps answered HTTP {code}"}
    except Exception:
        return {"decision": "DENY", "policy_id": "TRUSTOPS_UNAVAILABLE", "reason": "TrustOps could not be reached"}


def decide_each(bodies: list[dict]) -> list[dict]:
    """One decide() per body (TrustOps without POST /v1/decisions/batch)."""
    return [decide(b["actor"]["type"], b["actor"]["id"], b["action"],
                   **{k: v for k, v in b.items() if k not in ("actor", "action")}) for b in bodies]


BATCH_TIMEOUT = float(os.environ.get("TRUSTOPS_BATCH_TIMEOUT", "60"))


def decide_many(bodies: list[dict]) -> list[dict]:
    """2026-11-18: several items in one request (POST /v1/decisions/batch): TrustOps stores its state once for all of
    them, so a bulk send no longer times out item by item. One answer per body, in order, shaped like decide().
    A TrustOps without the batch endpoint (404/405) is asked item by item, as before."""
    if not bodies:
        return []
    try:
        out = _call("POST", "/v1/decisions/batch", {"items": bodies}, timeout=BATCH_TIMEOUT).get("results") or []
    except urllib.error.HTTPError as e:
        if e.code in (404, 405):
            return decide_each(bodies)
        if e.code == 401:
            return [{"decision": "DENY", "policy_id": "TRUSTOPS_UNAVAILABLE", "reason": "TrustOps rejected the LedgerSight key"}] * len(bodies)
        err, msg = _error_body(e)
        return [{"decision": "DENY", "policy_id": "TRUSTOPS_UNAVAILABLE", "error": err,
                 "reason": msg or f"TrustOps answered HTTP {e.code}. Nothing was sent; try again."}] * len(bodies)
    except Unreachable:
        return [{"decision": "DENY", "policy_id": "TRUSTOPS_UNAVAILABLE",
                 "reason": "TrustOps did not answer in time. Try again: an item that did arrive is not sent twice."}] * len(bodies)
    except Exception:
        return [{"decision": "DENY", "policy_id": "TRUSTOPS_UNAVAILABLE", "reason": "TrustOps could not be reached"}] * len(bodies)
    res = []
    for i in range(len(bodies)):
        r = out[i] if i < len(out) and isinstance(out[i], dict) else None
        if r is None:
            res.append({"decision": "DENY", "policy_id": "TRUSTOPS_UNAVAILABLE", "reason": "TrustOps gave no answer for this item"})
        elif r.get("ok"):
            res.append({k: v for k, v in r.items() if k != "ok"})
        else:
            code = int(r.get("status") or 0)
            res.append({"decision": "DENY", "policy_id": "TRUSTOPS_REFUSED" if 400 <= code < 500 else "TRUSTOPS_UNAVAILABLE",
                        "error": r.get("error"), "reason": r.get("message") or f"TrustOps answered HTTP {code}"})
    return res


def _error_body(e: urllib.error.HTTPError) -> tuple[str | None, str | None]:
    """TrustOps error answers look like {"error": code, "message": text} (or FastAPI's {"detail": {...}})."""
    try:
        b = json.loads(e.read() or b"{}")
    except Exception:
        return None, None
    if isinstance(b.get("detail"), dict):
        b = b["detail"]
    msg = b.get("message") or (b.get("detail") if isinstance(b.get("detail"), str) else None)
    return b.get("error") or b.get("code"), (str(msg)[:300] if msg else None)


def status(decision_id: str, max_age: float = 8.0) -> dict | None:
    """Final state of an escalated item: APPROVED | REJECTED | PENDING. None if TrustOps cannot be reached."""
    hit = _status_cache.get(decision_id)
    if hit and time.monotonic() - hit[0] < (max_age if hit[1] is not None else FAILED_STATUS_SECONDS):
        return hit[1]
    try:
        out = _call("GET", f"/v1/decisions/{decision_id}/status")
    except Exception:
        _status_cache[decision_id] = (time.monotonic(), None)   # a failed lookup is not retried for a few seconds
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


_rev_cache: list = [0.0, None]


def reviewers(max_age: float = 30.0) -> list[dict] | None:
    """Admins and Approvers in TrustOps who can review this application's items. None if TrustOps cannot be asked."""
    if _rev_cache[1] is not None and time.monotonic() - _rev_cache[0] < max_age:
        return _rev_cache[1]
    try:
        items = _call("GET", "/v1/reviewers").get("items") or []
    except Exception:
        return None
    _rev_cache[:] = [time.monotonic(), items]
    return items


def assign(decision_id: str, to: str | None, by: str) -> dict:
    """Change who reviews an item in TrustOps (None = any approver). Raises on failure with TrustOps' message."""
    try:
        out = _call("POST", f"/v1/decisions/{decision_id}/app-assign", {"to": to, "by": by})
    except urllib.error.HTTPError as e:
        raise ValueError(_error_body(e)[1] or f"TrustOps answered HTTP {e.code}") from None
    except Unreachable:
        raise ValueError("TrustOps could not be reached. Try again shortly.") from None
    _status_cache.pop(decision_id, None)
    return out


def valid_key(presented: str | None) -> bool:
    """Requests from TrustOps (Load details) carry the same key."""
    return bool(key()) and bool(presented) and hmac.compare_digest(presented, key())
