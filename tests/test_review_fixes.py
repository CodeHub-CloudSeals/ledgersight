"""Architecture review, Phase 1 quick fixes (review item numbers in the test names)."""
from test_ledgersight import FIXTURES, H, appmod, golden_run, make_user  # noqa: F401

import pytest

WS = "/v1/workspaces/client-a"


@pytest.fixture(scope="module")
def cpa():
    return make_user("review-cpa@demo.test", "owner_reviewer", ["client-a"])


def _confirm_receipt(c):
    up = c.post(WS + "/documents", headers=H, files={"file": ("client-a_receipt_OD-7741.pdf",
               (FIXTURES / "client-a_receipt_OD-7741.pdf").read_bytes(), "application/pdf")}).json()
    f = {k: v["value"] for k, v in up["fields"].items()}
    assert c.post(f"{WS}/documents/{up['id']}/confirm", headers=H, json={"fields": f, "doc_type": "receipt"}).status_code == 200
    return up


def test_r01_documents_table_after_pdf_keeps_confirmed_receipt(cpa):
    run1 = golden_run(cpa)
    assert any(x["bank_id"] == "HB009" and x["type"] == "MISSING_RECEIPT" for x in run1["result"]["exceptions"])
    _confirm_receipt(cpa)
    # a revised documents table arrives AFTER the PDF receipt was confirmed (one extra row, so not a duplicate file)
    csv = (FIXTURES / "client-a_documents.csv").read_text() + "DOC-H-09,lease,LEASE-NOV,VEND-LANDLORD,1200.00,lease_nov.pdf\n"
    up = cpa.post(WS + "/uploads", headers=H, data={"kind": "documents"},
                  files={"file": ("documents_revised.csv", csv.encode())}).json()
    assert cpa.post(f"{WS}/uploads/{up['id']}/mapping", headers=H, json={"mapping": up["proposed_mapping"]}).status_code == 200
    rows = appmod.svc.ws["client-a"].batches["documents"]["rows"]
    assert {"DOC-H-09", "DOC-H-01"} <= {r["id"] for r in rows}            # the new table is in
    assert any(r.get("ref_norm") == "OD7741" and r.get("confirmed_by") for r in rows)  # the confirmed PDF is still there
    run2 = cpa.post(WS + "/reconciliation-runs", headers=H).json()
    assert not any(x["bank_id"] == "HB009" for x in run2["result"]["exceptions"])
