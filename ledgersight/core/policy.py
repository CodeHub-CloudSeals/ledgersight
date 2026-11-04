"""Minimal TrustOps-style action gate for the demo slice.

Deny by default. Every request is checked for tenant/workspace membership, role
authority for the tool action, data class, model route allowlist and
maker-checker. Swap this for the real TrustOps gateway client when integrated;
the request/response shape follows plan section 9.
"""
from __future__ import annotations

from dataclasses import dataclass

POLICY_VERSION = "ledgersight-policy-1"

ROLE_ACTIONS = {
    "owner_reviewer": {"workspace.read", "upload.create", "mapping.commit", "run.create",
                       "exception.decide", "export.read", "workspace.reset", "party.confirm"},
    "preparer": {"workspace.read", "upload.create", "mapping.commit", "run.create", "exception.propose", "export.read", "party.confirm"},
    "support": {"health.read"},
    "agent:intake": {"upload.parse"},
    "agent:mapping": {"mapping.propose"},
    "agent:recon": {"run.compute"},
    "agent:explainer": {"exception.explain"},
}
# Never granted to anyone in this product boundary.
FORBIDDEN = {"ledger.post", "bank.payment", "email.send", "model_route.change", "credential.manage"}
ALLOWED_MODEL_ROUTES = {None, "deterministic", "us-approved-small-model"}
ALLOWED_DATA_CLASSES = {"synthetic"}  # real client data blocked until data gates pass


@dataclass
class Decision:
    decision: str  # ALLOW | DENY | ESCALATE
    reason_code: str
    policy_version: str = POLICY_VERSION


def evaluate(req: dict, memberships: dict[str, set[str]]) -> Decision:
    """memberships: actor_id -> set of workspace_ids (within the request tenant)."""
    action = req.get("tool_action")
    if action in FORBIDDEN:
        return Decision("DENY", "ACTION_OUTSIDE_PRODUCT_BOUNDARY")
    if req.get("workspace_id") not in memberships.get(req.get("actor_id"), set()):
        return Decision("DENY", "WORKSPACE_SCOPE")
    if action not in ROLE_ACTIONS.get(req.get("role"), set()):
        return Decision("DENY", "ROLE_NOT_AUTHORISED")
    if req.get("data_class") not in ALLOWED_DATA_CLASSES:
        return Decision("DENY", "DATA_CLASS_NOT_APPROVED")
    if req.get("model_route") not in ALLOWED_MODEL_ROUTES:
        return Decision("DENY", "MODEL_ROUTE_NOT_ALLOWLISTED")
    if action == "exception.decide":
        if req.get("actor_id") == req.get("maker_id"):
            return Decision("DENY", "MAKER_CHECKER_SELF_APPROVAL")
        if str(req.get("actor_id", "")).startswith("agent:"):
            return Decision("DENY", "AGENT_CANNOT_APPROVE")
    return Decision("ALLOW", "OK")
