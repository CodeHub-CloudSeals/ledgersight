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
    # 2026-11-13: every item is sent to a named approver in TrustOps
    monkeypatch.setattr(trustops_client, "reviewers", lambda max_age=30: [{"email": "approver@firm.test", "role": "Approver"}])
    monkeypatch.setattr(trustops_client, "assign", lambda did, to, by: {"decision_id": did, "assigned_to": to})
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
    body.setdefault("to", "approver@firm.test")
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
    assert "bar: el(\"div\", { class: \"tro-bar\" }" in body and "actions: [open]" not in body
    # 2026-11-13: no "Check now" button (a refresh icon in the bar); the TrustOps link only for approvers there
    # 2026-11-13 (user request): no "Approve in TrustOps" button and no "Any approver"; a person must be chosen
    assert '"Check now"' not in body and "tro-refresh" in body and "Approve in TrustOps" not in body
    assert '"Open TrustOps ↗"' not in body and '"Any approver"' not in JS and '"Choose an approver"' in body
    assert "send.disabled = !pick.value" in body
    assert '"Sent to ", el("b", {}, to)' in body
    assert "tro ? tro.bar : null" in JS and "tro ? tro.actions" in JS
    assert "#ag-grid{align-items:stretch}" in CSS and "#ag-grid>.card.agent{display:flex;flex-direction:column;height:100%}" in CSS


def test_send_needs_a_named_approver_in_trustops(tops, cpa):
    """2026-11-13 (user request): no "any approver"; the sender chooses the person, who must be an approver there."""
    run = pack_run(cpa)
    e0 = run["result"]["exceptions"][0]
    for to in ("", None):
        r = _send(cpa, run, e0, to=to)
        assert r.status_code == 422 and "Choose who to send it to" in r.text
    r = _send(cpa, run, e0, to="someone@else.test")
    assert r.status_code == 422 and "not an approver" in r.text
    assert not [c for c in tops.calls if c[2] == "submit_for_review"]               # nothing reached TrustOps
    r = _send(cpa, run, e0)
    assert r.status_code == 200 and r.json()["trustops_review"]["assigned_to"] == "approver@firm.test"
    url = f"{WS}/runs/{run['id']}/exceptions/{e0['id']}/reviewer"
    assert cpa.post(url, headers=H, json={"to": None}).status_code == 422          # cannot go back to "any approver"


def test_sources_and_reconciliation_pages_are_simpler():
    """2026-11-13 (user request): no Connections card, no "Already reconciled" card, no step IDs beside the agents."""
    html = (ROOT / "ledgersight" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="connectors"' not in html and '"callout gap ran"' not in JS and "`Already reconciled as" not in JS
    assert "(s.step_ids || []).join" not in JS


def test_review_queue_splits_decided_into_approved_and_rejected():
    """2026-11-13 (user request): Approved and Rejected chips instead of one "Decided" chip."""
    body = JS.split("function renderExceptions()", 1)[1].split(chr(10) + "}" + chr(10), 1)[0]
    assert 'chip("approved", "Approved"' in body and 'chip("rejected", "Rejected"' in body and '"Decided"' not in body
    assert 'S.excFilter === "approved" ? x.status === "ACCEPTED"' in body and 'S.excFilter === "rejected" ? x.status === "REJECTED"' in body


def test_bell_picks_up_trustops_decisions_from_older_runs_too(tops, cpa, prep):
    """2026-11-13 cross-check: an approval in TrustOps for an item from an older run still rings the bell."""
    from test_audit_fixes_p1 import PACK, upload_confirm
    old = pack_run(cpa)
    e0 = old["result"]["exceptions"][0]
    did = _send(cpa, old, e0).json()["trustops_review"]["decision_id"]
    # a corrected bank file and a new run, without resetting the workspace: the old run is no longer the latest
    fixed = (PACK / "01_bank_statement_October.csv").read_bytes().replace(b"RENT OCT LANDLORD", b"RENT OCTOBER LANDLORD", 1)
    upload_confirm(cpa, "01_bank_statement_October_v2.csv", fixed)
    new = cpa.post(WS + "/reconciliation-runs", headers=H).json()
    assert new["id"] != old["id"]
    tops.final[did] = ("REJECTED", "approver@firm.test", "wrong period")
    hit = [it for it in _items(prep) if it["kind"] == "REJECTED" and it["exception_id"] == e0["id"] and it["run_id"] == old["id"]]
    assert hit and hit[0]["via"] == "TrustOps" and hit[0]["note"] == "wrong period"


def test_sender_is_told_when_the_approver_did_not_receive_it(tops, cpa, prep, monkeypatch):
    """2026-11-13 action items: when TrustOps does not get the item, or the chosen approver cannot be assigned, the
    person who sent it gets a NOT_RECEIVED notification (only them) and the item says what happened."""
    run = pack_run(cpa)
    e0, e1 = run["result"]["exceptions"][:2]
    real = tops.decide
    monkeypatch.setattr(trustops_client, "decide", lambda *a, **k: {"decision": "DENY", "reason": "TrustOps is down"}
                        if a[2] == "submit_for_review" else real(*a, **k))
    r = _send(prep, run, e0)
    assert r.status_code == 200 and r.json()["trustops_review"]["status"] == "NOT_SENT"
    mine = [it for it in _items(prep) if it["kind"] == "NOT_RECEIVED" and it["exception_id"] == e0["id"]]
    assert mine and "did not receive it" in mine[0]["note"] and "TrustOps is down" in mine[0]["note"] and mine[0]["by"] == "TrustOps"
    assert not [it for it in _items(cpa) if it["kind"] == "NOT_RECEIVED"]          # only the sender is told
    # TrustOps gets it, but the approver cannot be assigned
    monkeypatch.setattr(trustops_client, "decide", real)

    def no_assign(did, to, by):
        raise ValueError("This person is not an Admin or Approver in TrustOps")
    monkeypatch.setattr(trustops_client, "assign", no_assign)
    r = _send(prep, run, e1)
    assert r.status_code == 200 and r.json()["trustops_review"]["status"] == "PENDING"
    hit = [it for it in _items(prep) if it["kind"] == "NOT_RECEIVED" and it["exception_id"] == e1["id"]]
    assert hit and "could not be made the approver" in hit[0]["note"]
    assert 'NOT_RECEIVED: "did not receive"' in JS and "k-NOT_RECEIVED" in CSS


def test_sent_button_stays_greyed_out_after_sending():
    """2026-11-13 action items: after sending, "Send to TrustOps" stays where it was (greyed out), never "Approve in TrustOps"."""
    body = JS.split("function tropsStatus(x, tops)", 1)[1].split(chr(10) + "}" + chr(10), 1)[0]
    pending = body.split('if (tr.status === "PENDING") return {', 1)[1]
    assert '"Sent to TrustOps")' in pending and "disabled: true" in pending and "Approve in TrustOps" not in body


def test_rejected_item_can_be_sent_again_with_new_proof(tops, cpa, prep):
    """2026-11-13 action items: an item rejected in TrustOps can be sent again once new proof is attached; TrustOps
    is told it is a resubmission (previous decision, earlier rejection, new proof)."""
    run = pack_run(cpa)
    e0 = run["result"]["exceptions"][0]
    did = _send(prep, run, e0).json()["trustops_review"]["decision_id"]
    url = f"{WS}/runs/{run['id']}/exceptions/{e0['id']}"
    assert prep.post(url + "/resend-to-trustops", headers=H, json={"to": "approver@firm.test"}).status_code == 422   # not rejected yet
    tops.final[did] = ("REJECTED", "approver@firm.test", "No receipt")
    cpa.get(f"{WS}/runs/{run['id']}", headers=H)                                                                  # picks up the rejection
    r = prep.post(url + "/resend-to-trustops", headers=H, json={"to": "approver@firm.test"})
    assert r.status_code == 422 and "Attach new proof" in r.text
    up = prep.post(url + "/attachments", headers=H, files={"file": ("receipt.pdf", b"%PDF-1.4 receipt", "application/pdf")}, data={"note": "receipt"})
    assert up.status_code == 200, up.text                                                                         # proof allowed on a rejected item
    assert prep.post(url + "/resend-to-trustops", headers=H, json={}).status_code == 422                           # approver required
    r = prep.post(url + "/resend-to-trustops", headers=H, json={"to": "approver@firm.test", "note": "Receipt attached"})
    assert r.status_code == 200, r.text
    assert r.json()["trustops_review"]["status"] == "PENDING" and r.json()["trustops_review"]["decision_id"] != did
    sub_calls = [c for c in tops.calls if c[2] == "submit_for_review"]
    ctx = sub_calls[-1][3]
    assert ctx["context"] == {"resubmission": 1, "resubmission_of": did}
    facts = {f["label"]: f["value"] for f in ctx["facts"]}
    assert "No receipt" in facts["Resubmitted"] and facts["New proof since the rejection"] == "receipt.pdf"
    x = next(e for e in cpa.get(f"{WS}/runs/{run['id']}", headers=H).json()["result"]["exceptions"] if e["id"] == e0["id"])
    assert x["status"] == "OPEN" and x["resubmitted"] == 1 and x["trustops_history"][0]["reason"] == "No receipt"
    assert 'chip("rejected", "Rejected", all.filter((x) => x.status === "REJECTED").length, "pl-rej")' in JS and "function resendBox(" in JS


def test_same_person_in_both_apps_still_hears_the_trustops_decision(tops, cpa, monkeypatch):
    """Live issue 2026-11-13: one Google account sent an item to itself and approved / rejected it in TrustOps; the
    LedgerSight bell hid the decision as 'your own action'. A decision made in TrustOps is always news here."""
    monkeypatch.setattr(trustops_client, "reviewers", lambda max_age=30: [{"email": "tops-notify@demo.test", "role": "Admin"}])
    run = pack_run(cpa)
    e0, e1 = run["result"]["exceptions"][:2]
    d0 = _send(cpa, run, e0, to="tops-notify@demo.test").json()["trustops_review"]["decision_id"]
    d1 = _send(cpa, run, e1, to="tops-notify@demo.test").json()["trustops_review"]["decision_id"]
    assert not [it for it in _items(cpa) if it["kind"] == "SENT"]               # sending is still your own action
    tops.final[d0] = ("APPROVED", "tops-notify@demo.test", "ok")
    tops.final[d1] = ("REJECTED", "tops-notify@demo.test", "no receipt")
    got = {(it["kind"], it["exception_id"]) for it in _items(cpa) if it["via"] == "TrustOps"}
    assert ("APPROVED", e0["id"]) in got and ("REJECTED", e1["id"]) in got
