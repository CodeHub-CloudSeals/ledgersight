"""TrustOps action gate.

Deny by default. Every request, from a person or from an agent, is checked for
client scope, role authority, data class, model route and maker-checker, and
gets one of three answers:

  ALLOW     the specific operation may run (it never authorises a posting)
  ESCALATE  a human with `required_role` must decide
  DENY      the operation is refused and the refusal is recorded

Each decision names the policy (POL-xx) and policy-set version that produced it.
Policies, roles and agent tool grants are loaded from ledgersight/config, not
written into this file.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from ..config import load

_CFG = load("policies.json")
_AGENTS = load("agents.json")["agents"]

POLICY_VERSION = _CFG["version"]
POLICIES: dict[str, dict] = {p["id"]: p for p in _CFG["policies"]}
FORBIDDEN = set(_CFG["forbidden_actions"])  # never granted to anyone in this product boundary
ALLOWED_MODEL_ROUTES = set(_CFG["allowed_model_routes"])
ALLOWED_DATA_CLASSES = set(_CFG["allowed_data_classes"])  # real client data blocked until data gates pass
ROLE_LABELS: dict[str, str] = _CFG.get("role_labels", {})
ROLE_ACTIONS: dict[str, set[str]] = {r: set(a) for r, a in _CFG["role_actions"].items()}

AGENTS: dict[str, dict] = {a["code"]: a for a in _AGENTS}            # "AG-03" -> agent
AGENT_BY_ACTOR: dict[str, dict] = {a["actor_id"]: a for a in _AGENTS}  # "agent:recon" -> agent
for _a in _AGENTS:  # an agent's role is its own actor id; its grants are its registered tools
    ROLE_ACTIONS[_a["actor_id"]] = set(_a["tools"])

# exception type -> escalation policy id ("*" is the catch-all)
ESCALATION: dict[str, str] = {}
for _p in _CFG["policies"]:
    if _p["effect"] == "ESCALATE":
        for _t in _p.get("exception_types", []):
            ESCALATION.setdefault(_t, _p["id"])

QUERY_REFUSALS = {"sql": "SQL_NOT_ALLOWED", "write": "READ_ONLY_QUERY", "cross_client": "WORKSPACE_SCOPE"}


@dataclass
class Decision:
    decision: str  # ALLOW | ESCALATE | DENY
    reason_code: str
    policy_id: str = ""
    required_role: str | None = None
    policy_version: str = POLICY_VERSION

    @property
    def policy_name(self) -> str:
        return POLICIES.get(self.policy_id, {}).get("name", "")

    def label(self) -> str:
        return f"{self.decision}:{self.reason_code}"

    def to_dict(self) -> dict:
        return {**asdict(self), "policy_name": self.policy_name,
                "policy_rule_version": POLICIES.get(self.policy_id, {}).get("version")}


def escalation_policy(exception_type: str | None) -> dict:
    return POLICIES[ESCALATION.get(exception_type or "", ESCALATION.get("*", "POL-09"))]


def evaluate(req: dict, memberships: dict[str, set[str]]) -> Decision:
    """memberships: actor_id -> set of workspace_ids (within the request tenant)."""
    action = req.get("tool_action")
    actor = str(req.get("actor_id") or "")
    role = req.get("role")
    if action in FORBIDDEN:
        return Decision("DENY", "ACTION_OUTSIDE_PRODUCT_BOUNDARY", "POL-03")
    if req.get("workspace_id") not in memberships.get(actor, set()):
        return Decision("DENY", "WORKSPACE_SCOPE", "POL-01")
    if action == "exception.decide":
        # Checked before role so a preparer approving their own work is told why: maker-checker.
        if req.get("maker_id") and actor == req.get("maker_id"):
            return Decision("DENY", "MAKER_CHECKER_SELF_APPROVAL", "POL-06")
        if actor.startswith("agent:"):
            return Decision("DENY", "AGENT_CANNOT_APPROVE", "POL-06")
    if action not in ROLE_ACTIONS.get(role, set()):
        return Decision("DENY", "ROLE_NOT_AUTHORISED", "POL-02")
    if req.get("data_class") not in ALLOWED_DATA_CLASSES:
        return Decision("DENY", "DATA_CLASS_NOT_APPROVED", "POL-07")
    if req.get("model_route") not in ALLOWED_MODEL_ROUTES:
        return Decision("DENY", "MODEL_ROUTE_NOT_ALLOWLISTED", "POL-08")
    if action == "exception.decide":
        need = req.get("required_role")
        if need and role != need:
            return Decision("DENY", "REQUIRED_ROLE_MISSING", req.get("escalation_policy_id") or "POL-02", need)
        return Decision("ALLOW", "MAKER_CHECKER_PASSED", "POL-06")
    if action == "exception.route":
        p = escalation_policy(req.get("exception_type"))
        return Decision("ESCALATE", p["name"], p["id"], p.get("required_role"))
    if action == "query.answer":
        cls = req.get("query_class", "read")
        if cls != "read":
            return Decision("DENY", QUERY_REFUSALS.get(cls, "READ_ONLY_QUERY"), "POL-01" if cls == "cross_client" else "POL-10")
    return Decision("ALLOW", "OK", "POL-02")
