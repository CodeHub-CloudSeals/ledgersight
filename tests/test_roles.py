"""CPA reviewer vs staff preparer (2026-11-12).

The CPA reviewer keeps every page and action. A staff preparer does the work (sources, reconciliation, review
queue proposals, counterparties, trace, reports, rulebooks, Ask) but the governance and admin views are the
reviewer's: signed audit trail, TrustOps proof, guardrail tests, data & security. The server refuses those for a
preparer under POL-02 and signs the refusal; hiding the pages in the browser is not the control.
"""
from test_ledgersight import H, appmod, golden_run, make_user  # noqa: F401

import pytest

WS = "/v1/workspaces/client-a"


@pytest.fixture(scope="module")
def cpa():
    return make_user("roles-cpa@demo.test", "owner_reviewer", ["client-a"])


@pytest.fixture(scope="module")
def prep():
    return make_user("roles-prep@demo.test", "preparer", ["client-a"])


def _denied(r):
    assert r.status_code == 403, (r.status_code, r.text[:200])
    d = r.json()["detail"] if "detail" in r.json() else r.json()
    assert d["decision"] == "DENY" and d["reason_code"] == "ROLE_NOT_AUTHORISED" and d["policy_id"] == "POL-02", d


def test_cpa_reviewer_keeps_every_governance_view(cpa):
    golden_run(cpa)
    assert cpa.get(WS + "/evidence?limit=5").status_code == 200
    assert cpa.get(WS + "/trustops/proof").status_code == 200
    assert cpa.get(WS + "/audit-trail.csv").status_code == 200
    assert cpa.post(WS + "/evidence/tamper-check", headers=H).status_code == 200
    assert cpa.get("/v1/trust").status_code == 200
    assert cpa.post(WS + "/actions/attempt", headers=H, json={"action": "ledger.post"}).json()["reason_code"] == "ACTION_OUTSIDE_PRODUCT_BOUNDARY"
    assert cpa.post(WS + "/actions/attempt", headers=H, json={"action": "ledger.post", "as_agent": "AG-03"}).json()["decision"] == "DENY"


def test_staff_preparer_is_refused_governance_views_and_the_refusal_is_signed(cpa, prep):
    golden_run(cpa)
    log = appmod.svc.ws["client-a"].log
    n = len(log.events)
    _denied(prep.get(WS + "/evidence?limit=5"))
    _denied(prep.get(WS + "/trustops/proof"))
    for f in ("pdf", "csv", "xlsx", "json"):
        _denied(prep.get(f"{WS}/audit-trail.{f}"))
    _denied(prep.post(WS + "/evidence/tamper-check", headers=H))
    _denied(prep.post(WS + "/actions/attempt", headers=H, json={"action": "ledger.post"}))
    _denied(prep.post(WS + "/actions/attempt", headers=H, json={"action": "ledger.post", "as_agent": "AG-03"}))
    new = log.events[n:]
    assert len(new) == 9 and all(e["action"] == "governance.read" and e["policy_decision"] == "DENY:ROLE_NOT_AUTHORISED"
                                 and e["policy_id"] == "POL-02" and e["actor_id"] == "roles-prep@demo.test" for e in new), new
    sec = appmod.svc.security_log
    k = len(sec.events)
    _denied(prep.get("/v1/trust"))
    assert sec.events[k]["action"] == "governance.read:trust" and sec.events[k]["policy_decision"] == "DENY:ROLE_NOT_AUTHORISED"
    assert cpa.get(WS + "/evidence?limit=1").json()["verification"]["state"] == "PASS"   # the chain still verifies


def test_staff_preparer_still_does_the_work(cpa, prep):
    run = golden_run(cpa)
    assert prep.get(WS).status_code == 200
    assert prep.get(WS + "/sources").status_code == 200
    assert prep.get(WS + "/agents").status_code == 200                    # the workflow card on Reconciliation
    assert prep.get(f"{WS}/runs/{run['id']}").status_code == 200
    assert prep.get(f"{WS}/runs/{run['id']}/reports/finance.json?preview=1").status_code == 200
    assert prep.get(f"{WS}/runs/{run['id']}/export.csv").status_code == 200
    assert prep.get("/v1/rulebooks").status_code == 200
    assert prep.get(WS + "/parties").status_code == 200
    run = prep.post(WS + "/reconciliation-runs", headers=H).json()      # the preparer runs reconciliation
    # the maker-checker refusal is still live for the preparer (the demo shows it on purpose)
    x = run["result"]["exceptions"][0]
    assert prep.post(f"{WS}/runs/{run['id']}/exceptions/{x['id']}/proposals", headers=H, json={"note": "Matches the vendor bill"}).status_code == 200
    r = prep.post(f"{WS}/runs/{run['id']}/exceptions/{x['id']}/decisions", headers=H, json={"decision": "ACCEPT", "note": "ok"})
    assert r.status_code == 403 and r.json()["detail"]["policy_id"] == "POL-06"


def test_policy_grants_governance_read_to_the_reviewer_only():
    from ledgersight.core import policy
    assert "governance.read" in policy.ROLE_ACTIONS["owner_reviewer"]
    assert "governance.read" not in policy.ROLE_ACTIONS["preparer"]
    assert policy.POLICY_VERSION == "trustops-policy-2026.11.4"
