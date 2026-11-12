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
