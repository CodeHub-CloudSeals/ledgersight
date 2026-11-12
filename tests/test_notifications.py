"""Notifications between the staff preparer and the CPA reviewer (2026-11-12).

A proposal notifies the CPA reviewer; an approve / reject / return notifies the staff preparer. Both are read from
the signed evidence log, so every browser sees the same thing. Your own actions never notify you. "Seen" is kept
per person and per client book, on the server.
"""
from test_ledgersight import H, appmod, golden_run, make_user  # noqa: F401

import pytest

WS = "/v1/workspaces/client-a"


@pytest.fixture(scope="module")
def cpa():
    return make_user("notif-cpa@demo.test", "owner_reviewer", ["client-a"])


@pytest.fixture(scope="module")
def prep():
    return make_user("notif-prep@demo.test", "preparer", ["client-a"])


def _n(c):
    r = c.get(WS + "/notifications")
    assert r.status_code == 200, r.text
    return r.json()


def test_proposal_notifies_the_reviewer_and_decisions_notify_the_preparer(cpa, prep):
    assert cpa.post(WS + "/reset", headers=H).status_code == 200
    run = golden_run(cpa)
    assert _n(cpa)["unread"] == 0 and _n(prep)["unread"] == 0
    xs = run["result"]["exceptions"]
    a, b = xs[0], xs[1]
    assert prep.post(f"{WS}/runs/{run['id']}/exceptions/{a['id']}/proposals", headers=H, json={"note": "Vendor bill supports it"}).status_code == 200
    n = _n(cpa)
    assert n["for"] == "reviewer" and n["unread"] == 1
    it = n["items"][0]
    assert it["kind"] == "PROPOSED" and it["by"] == "notif-prep@demo.test" and it["by_name"] == "notif-prep@demo.test" and it["exception_id"] == a["id"]
    assert it["case_id"] == a["case_id"] and it["note"] == "Vendor bill supports it" and it["ref"].startswith("PRP-") and it["unread"]
    assert _n(prep)["unread"] == 0                                  # your own proposal does not notify you
    # the reviewer approves one and returns the other: the preparer hears about both, newest first
    assert cpa.post(f"{WS}/runs/{run['id']}/exceptions/{a['id']}/decisions", headers=H, json={"decision": "ACCEPT", "note": "Agreed"}).status_code == 200
    assert cpa.post(f"{WS}/runs/{run['id']}/exceptions/{b['id']}/decisions", headers=H, json={"decision": "RETURN", "note": "Attach the receipt"}).status_code == 200
    p = _n(prep)
    assert p["for"] == "preparer" and p["unread"] == 2
    assert [i["kind"] for i in p["items"][:2]] == ["RETURNED", "APPROVED"]
    assert p["items"][0]["note"] == "Attach the receipt" and p["items"][0]["exception_id"] == b["id"] and p["items"][0]["ref"].startswith("APR-")
    assert _n(cpa)["unread"] == 1                                   # the reviewer's own decisions do not notify them


def test_seen_is_per_person_and_kept_on_the_server(cpa, prep):
    p = _n(prep)
    assert p["unread"] >= 1
    newest = p["items"][0]["seq"]
    assert prep.post(WS + "/notifications/seen", headers=H, json={"upto": newest}).json()["seen"] == newest
    p2 = _n(prep)
    assert p2["unread"] == 0 and not any(i["unread"] for i in p2["items"])
    assert _n(cpa)["unread"] >= 1                                   # marking yours does not touch a colleague's
    # kept on the server with the book (so another browser of the same person sees it read too)
    assert appmod.svc.ws["client-a"].to_state()["notif_seen"]["notif-prep@demo.test"] == newest
    # "seen" never goes backwards and never past the end of the log
    assert prep.post(WS + "/notifications/seen", headers=H, json={"upto": 0}).json()["seen"] == newest
    assert prep.post(WS + "/notifications/seen", headers=H, json={"upto": 10 ** 9}).json()["seen"] == len(appmod.svc.ws["client-a"].log.events) - 1
    assert prep.post(WS + "/notifications/seen", headers=H, json={"upto": "x"}).status_code == 422


def test_notifications_are_scoped_to_the_client(prep):
    other = make_user("notif-other@demo.test", "owner_reviewer", ["client-b"])
    assert other.get(WS + "/notifications").status_code == 404     # another client's book: concealed
    assert prep.get("/v1/workspaces/client-b/notifications").status_code == 404


def test_two_clears_in_a_row_do_not_collide(cpa):
    # Found while writing these tests: two clears inside one clock tick archived under the same name (503).
    for _ in range(4):
        assert cpa.post(WS + "/reset", headers=H).status_code == 200
    if appmod.svc.store:
        logs = appmod.svc.store.archived_logs("client-a")
        assert len(logs) == len(set(logs)) >= 4
