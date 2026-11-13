"""LedgerSight connected to TrustOps: agents ask TrustOps, blocked attempts are recorded there,
exceptions are approved only in TrustOps and the result comes back to the review queue."""
import pytest

from test_audit_fixes_p1 import WS, pack_run  # noqa: F401
from test_ledgersight import H, appmod, make_user  # noqa: F401
from ledgersight import trustops_client


class FakeTrustOps:
    def __init__(self):
        self.calls, self.final = [], {}

    def decide(self, actor_type, actor_id, action, **ctx):
        self.calls.append((actor_type, actor_id, action, ctx))
        if action in ("ledger.post", "email.send"):
            return {"decision": "DENY", "policy_id": "LS-01"}
        if action == "submit_for_review":
            return {"decision": "ESCALATE", "policy_id": "FIN-00", "decision_id": f"dec_{len(self.calls)}"}
        return {"decision": "ALLOW", "policy_id": "LS-02", "decision_id": f"dec_{len(self.calls)}"}

    def status(self, decision_id, max_age=0):
        f = self.final.get(decision_id)
        return {"final": f[0], "resolved_by": f[1], "resolution_reason": f[2]} if f else {"final": "PENDING"}


def sent_run(c):
    """A run whose review items a person has sent to TrustOps ("Send to TrustOps", 2026-11-13: nothing is sent
    automatically when the run completes any more)."""
    run = pack_run(c)
    for e in run["result"]["exceptions"]:
        r = c.post(f"{WS}/runs/{run['id']}/exceptions/{e['id']}/send-to-trustops", headers=H, json={})
        assert r.status_code == 200, r.text
    r = c.get(f"{WS}/runs/{run['id']}", headers=H)
    return r.json()


@pytest.fixture()
def tops(monkeypatch):
    fake = FakeTrustOps()
    monkeypatch.setenv("TRUSTOPS_URL", "https://trustops.example.test")
    monkeypatch.setenv("TRUSTOPS_KEY", "tok_test_key")
    monkeypatch.setattr(trustops_client, "decide", fake.decide)
    monkeypatch.setattr(trustops_client, "status", fake.status)
    fake.access = {"tops-owner@demo.test": "reviewer"}
    monkeypatch.setattr(trustops_client, "app_access", lambda email, max_age=60: fake.access.get(email))
    return fake


@pytest.fixture(scope="module")
def owner():
    return make_user("tops-owner@demo.test", "owner_reviewer", ["client-a"])


def test_exceptions_go_to_trustops_and_cannot_be_approved_here(tops, owner):
    run = sent_run(owner)
    assert run["trustops"]["connected"] is True
    exc = run["result"]["exceptions"]
    assert exc and all(e["trustops_review"]["status"] == "PENDING" for e in exc)
    submitted = [c for c in tops.calls if c[2] == "submit_for_review"]
    assert len(submitted) == len(exc)
    details = submitted[0][3]
    assert details["title"] and details["consequence"].startswith("Local review decision only")
    assert details["compare"] or details["lines"]
    assert any(c[0] == "agent" for c in tops.calls)                       # agents asked TrustOps
    r = owner.post(f"{WS}/runs/{run['id']}/exceptions/{exc[0]['id']}/decisions", headers=H, json={"decision": "ACCEPT", "note": "x"})
    assert r.status_code == 422 and "TrustOps" in r.text


def test_trustops_approval_comes_back_to_the_review_queue(tops, owner):
    run = sent_run(owner)
    e0 = run["result"]["exceptions"][0]
    tops.final[e0["trustops_review"]["decision_id"]] = ("APPROVED", "controller@firm.test", "checked the bank statement")
    again = owner.get(f"{WS}/runs/{run['id']}", headers=H).json()
    done = next(e for e in again["result"]["exceptions"] if e["id"] == e0["id"])
    assert done["status"] == "ACCEPTED" and done["reviewer"] == "controller@firm.test"
    assert done["review_note"] == "checked the bank statement" and "TrustOps" in done["effect"]


def test_blocked_agent_action_is_recorded_in_trustops(tops, owner):
    pack_run(owner)
    r = owner.post(f"{WS}/actions/attempt", headers=H, json={"action": "ledger.post", "as_agent": "AG-03"})
    assert r.status_code == 403
    assert any(c[2] == "ledger.post" for c in tops.calls)


def test_load_details_needs_the_trustops_key(tops, owner):
    run = sent_run(owner)
    ref = run["result"]["exceptions"][0]["trustops_review"]["ref"]
    assert owner.get(f"/trustops/items/{ref}").status_code == 401
    assert owner.get(f"/trustops/items/{ref}", headers={"X-TrustOps-Key": "wrong"}).status_code == 401
    ok = owner.get(f"/trustops/items/{ref}", headers={"X-TrustOps-Key": "tok_test_key"})
    assert ok.status_code == 200 and ok.json()["title"]


def test_without_trustops_settings_nothing_changes(owner, monkeypatch):
    monkeypatch.delenv("TRUSTOPS_URL", raising=False)
    monkeypatch.delenv("TRUSTOPS_KEY", raising=False)
    run = pack_run(owner)
    assert run["trustops"]["connected"] is False
    assert all("trustops_review" not in e for e in run["result"]["exceptions"])


def test_people_are_managed_in_trustops(tops, owner):
    r = owner.post("/v1/invites", headers=H, json={"user_id": "x@firm.test", "display_name": "X", "role": "preparer", "workspaces": ["client-a"], "account_days": 14})
    assert r.status_code == 409 and "TrustOps" in r.text
    me = owner.get("/v1/me", headers=H).json()
    assert me["trustops"]["connected"] is True


def test_trustops_login_uses_the_trustops_role():
    from ledgersight.app import auth
    tok = auth.trustops_login("new.reviewer@firm.test", "New Reviewer", {"client-a"}, "owner_reviewer")
    u = auth.resolve(tok)
    assert u.role == "owner_reviewer" and u.workspaces == {"client-a"}
    auth.revoke("new.reviewer@firm.test")
    tok = auth.trustops_login("new.reviewer@firm.test", "New Reviewer", {"client-a"}, "preparer")   # re-granted in TrustOps
    assert auth.resolve(tok).role == "preparer"



def test_access_removed_in_trustops_signs_the_person_out(tops, owner):
    assert owner.get("/v1/me", headers=H).status_code == 200
    tops.access.pop("tops-owner@demo.test")
    r = owner.get("/v1/me", headers=H)
    assert r.status_code == 401 and "TrustOps" in r.text


def test_reviewer_is_shown_and_can_be_changed(tops, monkeypatch):
    tops.access["tops-rev@demo.test"] = "reviewer"
    owner = make_user("tops-rev@demo.test", "owner_reviewer", ["client-a"])
    revs = [{"email": "approver@firm.test", "role": "Approver"}, {"email": "admin@firm.test", "role": "Admin"}]
    assigned = {}
    monkeypatch.setattr(trustops_client, "reviewers", lambda max_age=30: revs)

    def fake_assign(did, to, by):
        if to and to not in {r["email"] for r in revs}:
            raise ValueError("This person is not an Admin or Approver in TrustOps")
        assigned[did] = (to, by)
        return {"decision_id": did, "assigned_to": to}
    monkeypatch.setattr(trustops_client, "assign", fake_assign)
    rv = owner.get("/v1/trustops/reviewers", headers=H)
    assert rv.status_code == 200, rv.text
    assert rv.json()["items"] == revs
    run = sent_run(owner)
    e0 = run["result"]["exceptions"][0]
    url = f"{WS}/runs/{run['id']}/exceptions/{e0['id']}/reviewer"
    r = owner.post(url, headers=H, json={"to": "approver@firm.test"})
    assert r.status_code == 200 and r.json()["assigned_to"] == "approver@firm.test"
    assert assigned[e0["trustops_review"]["decision_id"]] == ("approver@firm.test", "tops-rev@demo.test")
    assert owner.post(url, headers=H, json={"to": "someone@else.test"}).status_code == 422
    again = owner.get(f"{WS}/runs/{run['id']}", headers=H).json()
    assert next(e for e in again["result"]["exceptions"] if e["id"] == e0["id"])["trustops_review"]["assigned_to"] == "approver@firm.test"


def test_finance_report_shows_trustops_approvals(tops):
    tops.access["tops-rep@demo.test"] = "reviewer"
    owner = make_user("tops-rep@demo.test", "owner_reviewer", ["client-a"])
    run = sent_run(owner)
    e0 = run["result"]["exceptions"][0]
    tops.final[e0["trustops_review"]["decision_id"]] = ("APPROVED", "controller@firm.test", "checked")
    owner.get(f"{WS}/runs/{run['id']}", headers=H)
    rep = owner.get(f"{WS}/runs/{run['id']}/reports/finance.json", headers=H).json()
    sec = next(s for s in rep["sections"] if s["title"] == "Approvals in TrustOps")
    row = next(r for r in sec["rows"] if r[0] == e0["id"])
    assert row[3] == "APPROVED" and row[5] == "controller@firm.test"
    assert any(k == "Approvals" and "trustops.example.test" in v for k, v in rep["summary"])
    assert owner.get(f"{WS}/runs/{run['id']}/reports/finance.pdf", headers=H).status_code == 200
