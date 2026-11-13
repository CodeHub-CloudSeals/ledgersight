"""TrustOps mode, user feedback 2026-11-13: sending review items to TrustOps is confirmed (toast + bell), approvals
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


def test_sending_to_trustops_is_one_signed_record_and_a_bell_item_for_both_roles(tops, cpa, prep):
    run = pack_run(cpa)
    sent_ids = [e["id"] for e in run["result"]["exceptions"] if e["trustops_review"]["status"] == "PENDING"]
    assert sent_ids
    for c in (cpa, prep):   # the person who ran it is told too: the sender is AG-04, not them
        sent = [it for it in _items(c) if it["kind"] == "SENT" and it["run_id"] == run["id"]]
        assert len(sent) == 1 and sorted(sent[0]["lines"]) == sorted(sent_ids) and sent[0]["via"] == "TrustOps"


def test_an_approval_made_in_trustops_reaches_the_bell_without_opening_the_run(tops, cpa, prep):
    run = pack_run(cpa)
    e0 = run["result"]["exceptions"][0]
    tops.final[e0["trustops_review"]["decision_id"]] = ("APPROVED", "approver@firm.test", "matched to the remittance")
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
    assert "tropsSentNote(S.run)" in JS and "sent to TrustOps for approval" in JS
    assert 'SENT: "sent"' in JS and 'it.via === "TrustOps"' in JS
    body = JS.split("function tropsStatus(x, tops)", 1)[1].split("\n}\n", 1)[0]
    assert "bar: el(\"div\", { class: \"tro-bar\" }" in body and "actions: [open]" in body
    # 2026-11-13: no "Check now" button (a refresh icon in the bar); the TrustOps link only for approvers there
    assert '"Check now"' not in body and "tro-refresh" in body and '"Approve in TrustOps ↗"' in body
    assert "items.some((r) => String(r.email" in body and '"Open TrustOps ↗"' not in body
    assert '"Sent to ", el("b", {}, to)' in body
    assert "tro ? tro.bar : null" in JS and "tro ? tro.actions" in JS
    assert "#ag-grid{align-items:stretch}" in CSS and "#ag-grid>.card.agent{display:flex;flex-direction:column;height:100%}" in CSS
