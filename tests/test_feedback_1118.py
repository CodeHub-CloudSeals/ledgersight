"""Feedback 2026-11-18: bulk send arrives in one TrustOps request (items no longer time out one by one), the reason
an item was not sent is shown, stale "not sent" bell items clear, plain locked-files text, run picker at the top of
Reconciliation / Review queue / Trace, and Trace can show any run."""
import json
import urllib.error
from pathlib import Path

import pytest

from test_audit_fixes_p1 import WS, pack_run  # noqa: F401
from test_ledgersight import H, appmod, make_user  # noqa: F401
from test_trustops_integration import FakeTrustOps
from ledgersight import trustops_client

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "ledgersight" / "static" / "avocet.js").read_text(encoding="utf-8")
HTML = (ROOT / "ledgersight" / "static" / "index.html").read_text(encoding="utf-8")
APPROVER = "approver@firm.test"


@pytest.fixture()
def tops(monkeypatch):
    fake = FakeTrustOps()
    monkeypatch.setenv("TRUSTOPS_URL", "https://trustops.example.test")
    monkeypatch.setenv("TRUSTOPS_KEY", "tok_test_key")
    monkeypatch.setattr(trustops_client, "decide", fake.decide)
    batches = []

    def many(bodies):
        batches.append(len(bodies))
        return trustops_client.decide_each(bodies)
    monkeypatch.setattr(trustops_client, "decide_many", many)
    fake.batches = batches
    monkeypatch.setattr(trustops_client, "status", fake.status)
    monkeypatch.setattr(trustops_client, "app_access", lambda email, max_age=60: "reviewer")
    monkeypatch.setattr(trustops_client, "reviewers", lambda max_age=30: [
        {"email": APPROVER, "role": "Approver"}, {"email": "ai-cpa@demo.test", "role": "Admin"}])
    return fake


@pytest.fixture(scope="module")
def cpa():
    return make_user("fb18-cpa@demo.test", "owner_reviewer", ["client-a"])


@pytest.fixture(scope="module")
def prep():
    return make_user("fb18-prep@demo.test", "preparer", ["client-a"])


def test_bulk_send_is_one_trustops_request(tops, prep, cpa):
    run = pack_run(cpa)
    ids = [e["id"] for e in run["result"]["exceptions"]]
    r = prep.post(f"{WS}/runs/{run['id']}/send-to-trustops", headers=H, json={"exception_ids": ids, "to": APPROVER, "note": "Oct"})
    assert r.status_code == 200, r.text
    assert tops.batches == [len(ids)]                                   # one request for every item
    subs = [c for c in tops.calls if c[2] == "submit_for_review"]
    assert len(subs) == len(ids) and all(c[3].get("reuse_pending") is True for c in subs)
    assert r.json()["sent"] == len(ids)


def test_not_sent_items_say_why_and_clear_from_the_bell_after_sending(tops, prep, cpa, monkeypatch):
    run = pack_run(cpa)
    ids = [e["id"] for e in run["result"]["exceptions"]][:2]
    down = lambda bodies: [{"decision": "DENY", "policy_id": "TRUSTOPS_UNAVAILABLE",
                            "reason": "TrustOps did not answer in time. Try again"}] * len(bodies)
    monkeypatch.setattr(trustops_client, "decide_many", down)
    out = prep.post(f"{WS}/runs/{run['id']}/send-to-trustops", headers=H, json={"exception_ids": ids, "to": APPROVER}).json()
    assert out["not_sent"] == 2
    assert all("Not sent to" in x["reason"] and "did not answer in time" in x["reason"] for x in out["results"])
    items = lambda: [i for i in prep.get(WS + "/notifications", headers=H).json()["items"]
                     if i["kind"] == "NOT_RECEIVED" and i["exception_id"] in ids and i["run_id"] == run["id"]]
    assert len(items()) == 2 and all("did not answer in time" in i["note"] for i in items())
    # Try again on one item: it arrives, and its "not sent" warning leaves the bell
    r = prep.post(f"{WS}/runs/{run['id']}/exceptions/{ids[0]}/send-to-trustops", headers=H, json={"to": APPROVER})
    assert r.json()["trustops_review"]["status"] == "PENDING"
    assert [i["exception_id"] for i in items()] == [ids[1]]


class _Resp:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_decide_many_client(monkeypatch):
    monkeypatch.setenv("TRUSTOPS_URL", "https://trustops.example.test")
    monkeypatch.setenv("TRUSTOPS_KEY", "tok_test_key")
    trustops_client.reset_breaker()
    seen = []

    def ok(req, timeout):
        seen.append((req.full_url, timeout, json.loads(req.data)))
        return _Resp({"results": [{"ok": True, "decision_id": "dec_1", "decision": "ESCALATE", "assigned_to": APPROVER},
                                  {"ok": False, "status": 422, "error": "not_a_reviewer", "message": "Not an approver"}]})
    monkeypatch.setattr(trustops_client.urllib.request, "urlopen", ok)
    b = {"actor": {"type": "human", "id": "a@b.test"}, "action": "submit_for_review", "resource": "x"}
    out = trustops_client.decide_many([b, b])
    assert seen[0][0].endswith("/v1/decisions/batch") and seen[0][1] == trustops_client.BATCH_TIMEOUT
    assert out[0]["decision_id"] == "dec_1" and "ok" not in out[0]
    assert out[1]["error"] == "not_a_reviewer" and out[1]["reason"] == "Not an approver" and out[1]["policy_id"] == "TRUSTOPS_REFUSED"

    def timeout(req, timeout):
        raise TimeoutError()
    monkeypatch.setattr(trustops_client.urllib.request, "urlopen", timeout)
    out = trustops_client.decide_many([b])
    assert "did not answer in time" in out[0]["reason"] and not out[0].get("decision_id")
    trustops_client.reset_breaker()

    # an older TrustOps without the batch endpoint: item by item
    def old(req, timeout):
        if req.full_url.endswith("/batch"):
            raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)
        return _Resp({"decision_id": "dec_2", "decision": "ESCALATE"})
    monkeypatch.setattr(trustops_client.urllib.request, "urlopen", old)
    assert [x["decision_id"] for x in trustops_client.decide_many([b, b])] == ["dec_2", "dec_2"]


def test_locked_files_text_is_plain(tops, cpa, prep):
    run = pack_run(cpa)
    e0 = run["result"]["exceptions"][0]
    assert prep.post(f"{WS}/runs/{run['id']}/exceptions/{e0['id']}/send-to-trustops", headers=H, json={"to": APPROVER}).status_code == 200
    s = cpa.get(WS + "/sources", headers=H).json()
    assert s["locked"] and "already being worked on" in s["locked_reason"] and "Start next period" in s["locked_reason"]
    assert "archived" not in s["locked_reason"]
    assert "These files can't be cleared" in JS and "Files are locked" not in JS
    cpa.post(WS + "/reset", headers=H)


def test_trace_can_show_an_earlier_run(cpa):
    first = pack_run(cpa)
    cpa.post(WS + "/reset", headers=H)
    second = pack_run(cpa)
    runs = cpa.get(WS + "/runs", headers=H).json()
    ids = [r["id"] for r in runs["items"]]
    latest, older = runs["latest_run"], ids[-1]
    assert cpa.get(WS + "/trace", headers=H).json()["run_id"] == latest
    if older != latest:
        t = cpa.get(f"{WS}/trace?run_id={older}", headers=H).json()
        assert t["run_id"] == older and t["latest_run"] == latest and t["lines"]
        line = t["lines"][0]["id"]
        assert cpa.get(f"{WS}/trace/{line}?run_id={older}", headers=H).status_code == 200
    assert cpa.get(f"{WS}/trace?run_id=RUN-NOPE", headers=H).status_code in (400, 404, 422)


def test_run_picker_at_the_top_and_send_report_in_the_ui():
    for box in ("rc-runbar", "exc-runbar", "tr-runbar"):
        assert f'id="{box}"' in HTML
    assert HTML.index('id="rc-runbar"') < HTML.index('id="runs-list"') < HTML.index('id="recon-ready"')   # runs at the top
    assert HTML.index('id="exc-runbar"') < HTML.index('id="exc-list"')
    assert "function renderRunBars(" in JS and "S.runPinned" in JS and '"/trace" + (S.run ? `?run_id=' in JS
    assert "function sendReport(" in JS and '"notsent"' in JS and "Not sent" in JS
    assert "Send selected to TrustOps" not in JS


def test_run_by_filter_only_for_reviewers_and_actions_not_clipped():
    css = (ROOT / "ledgersight" / "static" / "avocet.css").read_text(encoding="utf-8")
    assert "const byFilter = isCPA();" in JS and 'byFilter ? el("label", {}, "Run by ", by) : null' in JS
    assert '"Run by", "Files"' in JS                                         # the column stays for everyone
    assert ".runs-act{display:flex" not in css and ".runs-act-in{display:flex" in css   # a flex <td> broke the table


def test_runs_are_numbered_and_numbers_are_never_reused(cpa):
    cpa.post(WS + "/reset", headers=H)
    first = pack_run(cpa)["id"]
    assert first.startswith("RUN-") and len(first) == 8 and first[4:].isdigit()
    from ledgersight.service import Workspace
    fresh = Workspace("client-a", appmod.svc._key)
    assert fresh.next_run_id() == "RUN-0001" and fresh.next_run_id() == "RUN-0002"
    ids = [r["id"] for r in cpa.get(WS + "/runs", headers=H).json()["items"]]
    assert all(i.startswith("RUN-") and len(i) == 8 and i[4:].isdigit() for i in ids)
    cpa.post(WS + "/reset", headers=H)                     # a new period continues the numbering
    again = pack_run(cpa)["id"]
    assert int(again[4:]) > int(max(ids)[4:])
    w = appmod.svc.ws["client-a"]
    assert w.to_state()["run_seq"] == int(again[4:])
