"""TrustOps mode, user feedback 2026-11-13: a person sends each review item with "Send to TrustOps" (nothing is sent
when the run completes); sending is confirmed (toast + bell), approvals
made in TrustOps show in the bell for both roles, review items show one status bar above one row of buttons, and
the six agent cards are the same height."""
from pathlib import Path

import pytest

from test_audit_fixes_p1 import WS, pack_run  # noqa: F401
from test_ledgersight import H, appmod, make_user  # noqa: F401
from test_trustops_integration import FakeTrustOps
from ledgersight import trustops_client

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "ledgersight" / "static" / "avocet.js").read_text(encoding="utf-8")
CSS = (ROOT / "ledgersight" / "static" / "avocet.css").read_text(encoding="utf-8")
SVC = (ROOT / "ledgersight" / "service.py").read_text(encoding="utf-8")


@pytest.fixture()
def tops(monkeypatch):
    fake = FakeTrustOps()
    monkeypatch.setenv("TRUSTOPS_URL", "https://trustops.example.test")
    monkeypatch.setenv("TRUSTOPS_KEY", "tok_test_key")
    monkeypatch.setattr(trustops_client, "decide", fake.decide)
    monkeypatch.setattr(trustops_client, "status", fake.status)
    fake.access = {"tops-notify@demo.test": "reviewer", "tops-prep@demo.test": "preparer"}
    monkeypatch.setattr(trustops_client, "app_access", lambda email, max_age=60: fake.access.get(email))
    return fake


@pytest.fixture(scope="module")
def cpa():
    return make_user("tops-notify@demo.test", "owner_reviewer", ["client-a"])


@pytest.fixture(scope="module")
def prep():
    return make_user("tops-prep@demo.test", "preparer", ["client-a"])


def _items(c):
    r = c.get(f"{WS}/notifications", headers=H)
    assert r.status_code == 200, r.text
    return r.json()["items"]


def _send(c, run, e, **body):
    return c.post(f"{WS}/runs/{run['id']}/exceptions/{e['id']}/send-to-trustops", headers=H, json=body)


def test_nothing_is_sent_until_a_person_presses_send(tops, cpa):
    run = pack_run(cpa)
    assert run["result"]["exceptions"] and all("trustops_review" not in e for e in run["result"]["exceptions"])
    assert not [c for c in tops.calls if c[2] == "submit_for_review"]


def test_send_to_trustops_sends_one_item_with_the_note_and_tells_the_other_person(tops, cpa, prep):
    run = pack_run(cpa)
    e0, e1 = run["result"]["exceptions"][:2]
    r = _send(prep, run, e0, note="Customer paid the rest on 2 Nov, see remittance")   # a preparer may send
    assert r.status_code == 200, r.text
    assert r.json()["trustops_review"]["status"] == "PENDING"
    sub = [c for c in tops.calls if c[2] == "submit_for_review"]
    assert len(sub) == 1 and sub[0][1] == "tops-prep@demo.test"
    facts = {f["label"]: f["value"] for f in sub[0][3]["facts"]}
    assert facts["Note from tops-prep@demo.test"] == "Customer paid the rest on 2 Nov, see remittance"
    again = cpa.get(f"{WS}/runs/{run['id']}", headers=H).json()["result"]["exceptions"]
    assert next(e for e in again if e["id"] == e1["id"]).get("trustops_review") is None        # only the one pressed
    sent = [it for it in _items(cpa) if it["kind"] == "SENT" and it["exception_id"] == e0["id"]]
    assert sent and sent[0]["by"] == "tops-prep@demo.test" and sent[0]["via"] == "TrustOps"
    assert not [it for it in _items(prep) if it["kind"] == "SENT" and it["exception_id"] == e0["id"]]  # not your own
    again = _send(cpa, run, e0)
    assert again.status_code == 422 and "Already sent" in again.text


def test_send_is_refused_without_trustops_or_for_a_decided_item(tops, cpa, monkeypatch):
    run = pack_run(cpa)
    e0 = run["result"]["exceptions"][0]
    assert _send(cpa, run, e0).status_code == 200
    tops.final[cpa.get(f"{WS}/runs/{run['id']}", headers=H).json()["result"]["exceptions"][0]["trustops_review"]["decision_id"]] =         ("APPROVED", "approver@firm.test", "ok")
    cpa.get(f"{WS}/runs/{run['id']}", headers=H)                       # picks up the approval
    r = _send(cpa, run, e0)
    assert r.status_code == 422 and "already has a decision" in r.text
    monkeypatch.delenv("TRUSTOPS_URL", raising=False)
    assert _send(cpa, run, run["result"]["exceptions"][1]).status_code == 409


def test_an_approval_made_in_trustops_reaches_the_bell_without_opening_the_run(tops, cpa, prep):
    run = pack_run(cpa)
    e0 = run["result"]["exceptions"][0]
    did = _send(cpa, run, e0).json()["trustops_review"]["decision_id"]
    tops.final[did] = ("APPROVED", "approver@firm.test", "matched to the remittance")
    for c in (cpa, prep):
        hit = [it for it in _items(c) if it["kind"] == "APPROVED" and it["exception_id"] == e0["id"]]
        assert hit and hit[0]["via"] == "TrustOps" and hit[0]["case_id"] == e0["case_id"]
        assert hit[0]["note"] == "matched to the remittance" and hit[0]["by"] == "approver@firm.test"


def test_without_trustops_nothing_new_is_logged(cpa, monkeypatch):
    monkeypatch.delenv("TRUSTOPS_URL", raising=False)
    monkeypatch.delenv("TRUSTOPS_KEY", raising=False)
    run = pack_run(cpa)
    assert not [it for it in _items(cpa) if it["kind"] == "SENT" and it["run_id"] == run["id"]]


def test_page_confirms_the_send_and_lays_out_the_trustops_controls():
    assert "tropsSentNote(S.run)" in JS and "from the Review queue" in JS and "self.submit_to_trustops" not in SVC
    assert '"Send to TrustOps"' in JS and "/send-to-trustops`" in JS and "x._noteEl = note;" in JS
    assert 'SENT: "sent"' in JS and 'it.via === "TrustOps"' in JS
    body = JS.split("function tropsStatus(x, tops)", 1)[1].split("\n}\n", 1)[0]
    assert "bar: el(\"div\", { class: \"tro-bar\" }" in body and "actions: [open]" in body
    # 2026-11-13: no "Check now" button (a refresh icon in the bar); the TrustOps link only for approvers there
    assert '"Check now"' not in body and "tro-refresh" in body and '"Approve in TrustOps ↗"' in body
    assert "items.some((r) => String(r.email" in body and '"Open TrustOps ↗"' not in body
    assert '"Sent to ", el("b", {}, to)' in body
    assert "tro ? tro.bar : null" in JS and "tro ? tro.actions" in JS
    assert "#ag-grid{align-items:stretch}" in CSS and "#ag-grid>.card.agent{display:flex;flex-direction:column;height:100%}" in CSS
