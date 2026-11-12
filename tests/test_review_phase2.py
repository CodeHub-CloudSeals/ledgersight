"""Architecture review, Phase 2 (UX + domain). Review item numbers in the test names."""
from test_ledgersight import FIXTURES, H, appmod, golden_run, make_user  # noqa: F401

import pytest

WS = "/v1/workspaces/client-a"


@pytest.fixture(scope="module")
def cpa():
    return make_user("p2r-cpa@demo.test", "owner_reviewer", ["client-a"])


def _status(c):
    return next(w for w in c.get("/v1/workspaces").json() if w["id"] == "client-a")["status"]


# ---- #7 the Report step is a server fact (signed audit trail), not this browser's memory ----
def test_r07_report_step_comes_from_the_signed_trail(cpa):
    run = golden_run(cpa)
    assert _status(cpa)["report_delivered"] is False
    assert cpa.get(f"{WS}/runs/{run['id']}/reports/finance.json?preview=1").status_code == 200
    assert _status(cpa)["report_delivered"] is False                    # a preview on screen is not a delivery
    other = make_user("p2r-colleague@demo.test", "owner_reviewer", ["client-a"])
    assert cpa.get(f"{WS}/runs/{run['id']}/reports/finance.pdf").status_code == 200
    assert _status(other)["report_delivered"] is True                   # a colleague in another browser sees it too
    # the files change (a revised documents table) and the book is reconciled again: a new run starts over
    csv = (FIXTURES / "client-a_documents.csv").read_text() + "DOC-H-09,lease,LEASE-NOV,VEND-LANDLORD,1200.00,lease_nov.pdf\n"
    up = cpa.post(WS + "/uploads", headers=H, data={"kind": "documents"}, files={"file": ("docs_v2.csv", csv.encode())}).json()
    assert cpa.post(f"{WS}/uploads/{up['id']}/mapping", headers=H, json={"mapping": up["proposed_mapping"]}).status_code == 200
    run2 = cpa.post(WS + "/reconciliation-runs", headers=H).json()
    assert run2["id"] != run["id"] and _status(cpa)["report_delivered"] is False
    assert cpa.get(f"{WS}/runs/{run2['id']}/export.csv").status_code == 200
    assert _status(cpa)["report_delivered"] is True                     # the review pack counts as delivery


def test_r07_audit_trail_downloads_in_every_format(cpa):
    golden_run(cpa)
    for f, t in (("pdf", "application/pdf"), ("xlsx", "spreadsheet"), ("csv", "text/csv"), ("json", "application/json")):
        r = cpa.get(f"{WS}/audit-trail.{f}")
        assert r.status_code == 200 and t in r.headers["content-type"], (f, r.headers.get("content-type"))


# ---- #8 mapping step: required fields marked; the day/month order is shown and confirmed, never silently assumed ----
def _upload(c, kind, name, text):
    r = c.post(WS + "/uploads", headers=H, data={"kind": kind}, files={"file": (name, text.encode())})
    assert r.status_code == 200, r.text
    return r.json()


BANK_HEAD = "line_id,date,description,reference,amount,currency\n"


def test_r08_ambiguous_dates_are_confirmed_by_a_person(cpa):
    assert cpa.post(WS + "/reset", headers=H).status_code == 200
    up = _upload(cpa, "bank", "uk_bank.csv", BANK_HEAD + "U1,05/10/2026,FPS CREDIT CUSTOMER 01,INV-1,100.00,GBP\n"
                                                         "U2,07/10/2026,CARD PRINT SUPPLIER,PS-1,-20.00,GBP\n")
    assert up["required"] == ["line_id", "date", "amount", "currency"]
    assert up["dates"]["order"] == "MDY" and up["dates"]["certain"] is False and "either way" in up["dates"]["reason"]
    n = len(appmod.svc.ws["client-a"].log.events)
    r = cpa.post(f"{WS}/uploads/{up['id']}/mapping", headers=H, json={"mapping": up["proposed_mapping"], "date_order": "DMY"})
    assert r.status_code == 200 and r.json()["date_order"] == "DMY"
    rows = appmod.svc.ws["client-a"].batches["bank"]["rows"]
    assert str(rows[0]["date"]) == "2026-10-05"                                  # 5 October, as the person confirmed
    ev = next(e for e in appmod.svc.ws["client-a"].log.events[n:] if e["action"] == "mapping.confirmed")
    assert ev["source_line_refs"] == ["dates:DMY"]                               # the choice is on the signed record


def test_r08_without_a_choice_ambiguous_dates_keep_the_us_default(cpa):
    assert cpa.post(WS + "/reset", headers=H).status_code == 200
    up = _upload(cpa, "bank", "us_bank.csv", BANK_HEAD + "A1,05/10/2026,X,R1,10.00,USD\n")
    assert cpa.post(f"{WS}/uploads/{up['id']}/mapping", headers=H, json={"mapping": up["proposed_mapping"]}).status_code == 200
    assert str(appmod.svc.ws["client-a"].batches["bank"]["rows"][0]["date"]) == "2026-05-10"


@pytest.mark.parametrize("dates,order,certain", [(["25/10/2026", "05/10/2026"], "DMY", True),
                                                 (["10/25/2026", "10/05/2026"], "MDY", True),
                                                 (["2026-10-05", "2026-10-07"], "ISO", True)])
def test_r08_dates_the_file_proves_need_no_choice(cpa, dates, order, certain):
    body = "".join(f"D{i},{d},X,R{i},10.00,USD\n" for i, d in enumerate(dates))
    up = _upload(cpa, "bank", f"b_{order}.csv", BANK_HEAD + body)
    assert up["dates"]["order"] == order and up["dates"]["certain"] is certain


def test_r08_unknown_date_order_is_refused(cpa):
    up = _upload(cpa, "bank", "bad_order.csv", BANK_HEAD + "Z1,05/10/2026,X,R1,10.00,USD\n")
    r = cpa.post(f"{WS}/uploads/{up['id']}/mapping", headers=H, json={"mapping": up["proposed_mapping"], "date_order": "YMD"})
    assert r.status_code == 422
