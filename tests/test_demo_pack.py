"""The upload test pack in demo_files/ gives the results documented in demo_files/README.md,
and local demo mode (no login) works without weakening the normal sign-in."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_ledgersight import H, appmod, make_user  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent / "demo_files"
PACK = ROOT / "1_standard_month"
WS = "/v1/workspaces/client-b"  # a clean book, separate from the other tests' sample data
FILES = [("bank", "01_bank_statement.csv"), ("ledger", "02_general_ledger.xlsx"), ("ar", "03_open_invoices_AR.csv"),
         ("ap", "04_vendor_bills_AP.json"), ("documents", "05_supporting_documents.csv"), ("parties", "06_customers_and_vendors.csv")]


@pytest.fixture(scope="module")
def cpa():
    return make_user("pack@demo.test", "owner_reviewer", ["client-a", "client-b"])


def upload_pack(c):
    assert c.post(WS + "/reset", headers=H).status_code == 200
    for kind, name in FILES:
        up = c.post(WS + "/uploads", headers=H, data={"kind": kind}, files={"file": (name, (PACK / name).read_bytes())})
        assert up.status_code == 200, (name, up.text)
        up = up.json()
        assert all(up["proposed_mapping"][f] for f in ("currency",) if f in up["proposed_mapping"]), up["proposed_mapping"]
        r = c.post(f"{WS}/uploads/{up['id']}/mapping", headers=H, json={"mapping": up["proposed_mapping"]}).json()
        assert r["health"]["rejected"] == [], (name, r["health"])
    run = c.post(WS + "/reconciliation-runs", headers=H)
    assert run.status_code == 200, run.text
    return run.json()


def test_pack_results_match_readme(cpa):
    run = upload_pack(cpa)
    res = run["result"]
    assert {m["bank_id"]: m["ledger_id"] for m in res["matches"]} == {
        "DB01": "GL-A-301", "DB02": "GL-A-302", "DB03": "GL-A-303", "DB05": "GL-A-304", "DB07": "GL-A-305",
        "DB08": "GL-A-306", "DB09": "GL-A-307", "DB11": "GL-A-308", "DB12": "GL-A-309"}
    exc = {(e["bank_id"] or e["ledger_id"]): e for e in res["exceptions"]}
    assert {k: e["type"] for k, e in exc.items()} == {
        "DB04": "POSSIBLE_DUPLICATE", "DB05": "PARTIAL_RECEIPT", "DB06": "MISSING_GL", "DB09": "MISSING_RECEIPT",
        "DB10": "UNMATCHED_DEPOSIT", "GL-A-310": "MISSING_BANK"}
    assert exc["DB05"]["residual_cents"] == 75000
    assert "PK-3302" in exc["DB06"]["evidence_refs"]
    pol = {k: e["trustops"]["policy_id"] for k, e in exc.items()}
    assert pol == {"DB04": "POL-04", "DB05": "POL-09", "DB06": "POL-05", "DB09": "POL-09", "DB10": "POL-09", "GL-A-310": "POL-05"}
    assert (res["bank_net_cents"], res["ledger_net_cents"], res["variance_cents"], res["unexplained_variance_cents"]) == \
        (231060, 211460, 19600, 0)


def test_pack_receipt_pdf_clears_missing_receipt(cpa):
    upload_pack(cpa)
    up = cpa.post(WS + "/documents", headers=H, files={"file": ("07_receipt_RC-4410.pdf", (PACK / "07_receipt_RC-4410.pdf").read_bytes())}).json()
    f = {k: v["value"] for k, v in up["fields"].items()}
    assert (f["reference"], f["amount"]) == ("RC-4410", "186.40")
    assert cpa.post(f"{WS}/documents/{up['id']}/confirm", headers=H, json={"fields": f, "doc_type": "receipt"}).status_code == 200
    run2 = cpa.post(WS + "/reconciliation-runs", headers=H).json()
    assert "DB09" not in {e["bank_id"] for e in run2["result"]["exceptions"]}


def test_pack_bank_download_is_same_statement(cpa):
    cpa.post(WS + "/reset", headers=H)
    up = cpa.post(WS + "/uploads", headers=H, data={"kind": "bank"},
                  files={"file": ("08.qbo", (PACK / "08_bank_statement_bank_download.qbo").read_bytes())}).json()
    r = cpa.post(f"{WS}/uploads/{up['id']}/mapping", headers=H, json={"mapping": up["proposed_mapping"]}).json()
    assert r["health"]["accepted"] == 12 and r["health"]["net_cents"] == 231060


def test_pack_unsafe_files(cpa):
    up = cpa.post(WS + "/documents", headers=H, files={"file": ("09.pdf", (PACK / "09_suspicious_invoice.pdf").read_bytes())}).json()
    assert len(up["unsafe_content"]) == 2
    r = cpa.post(WS + "/uploads", headers=H, data={"kind": "ledger"},
                 files={"file": ("10.xlsm", (PACK / "10_should_be_rejected_macro.xlsm").read_bytes())})
    assert r.status_code == 422


def test_pack_has_no_client_names():
    from test_governance import FORMER_NAMES
    for p in ROOT.rglob("*"):
        if p.is_file():
            text = p.read_bytes().decode("latin-1").lower()
            assert not [n for n in FORMER_NAMES if n in text], p.name


# ---- local demo mode ---------------------------------------------------------------------------
def test_no_login_mode_acts_as_demo_people(monkeypatch):
    monkeypatch.setattr(appmod, "NO_LOGIN", True)
    c = TestClient(appmod.app)
    cfg = c.get("/v1/auth-config").json()
    assert cfg["login_required"] is False and {p["key"] for p in cfg["demo_people"]} == {"reviewer", "preparer"}
    assert c.get("/v1/me").json()["role"] == "owner_reviewer"
    assert c.post("/v1/demo/act-as", headers=H, json={"as": "preparer"}).status_code == 200
    assert c.get("/v1/me").json()["role"] == "preparer"
    assert c.post("/v1/demo/act-as", headers=H, json={"as": "admin"}).status_code == 422
    assert c.post("/v1/workspaces/client-a/reconciliation-runs").status_code == 403  # CSRF header still required


def test_no_login_maker_checker_still_enforced(monkeypatch):
    monkeypatch.setattr(appmod, "NO_LOGIN", True)
    rev, prep = TestClient(appmod.app), TestClient(appmod.app)
    prep.post("/v1/demo/act-as", headers=H, json={"as": "preparer"})
    rev.post(WS + "/reset", headers=H)
    rev.post(WS + "/samples/load", headers=H)
    run = rev.post(WS + "/reconciliation-runs", headers=H).json()
    exc = run["result"]["exceptions"][0]
    base = f"{WS}/runs/{run['id']}/exceptions/{exc['id']}"
    assert prep.post(base + "/proposals", headers=H, json={"note": "x"}).status_code == 200
    r = prep.post(base + "/decisions", headers=H, json={"decision": "ACCEPT"})
    assert r.status_code == 403 and r.json()["detail"]["reason_code"] == "MAKER_CHECKER_SELF_APPROVAL"
    assert rev.post(base + "/decisions", headers=H, json={"decision": "ACCEPT"}).status_code == 200


def test_login_still_required_by_default():
    assert appmod.NO_LOGIN is False
    assert TestClient(appmod.app).get("/v1/me").status_code == 401
    assert TestClient(appmod.app).post("/v1/demo/act-as", headers=H, json={"as": "reviewer"}).status_code == 401


# ---- PDF bank statements and invoices -----------------------------------------------------------
def _upload_confirm(c, kind, name):
    up = c.post(WS + "/uploads", headers=H, data={"kind": kind}, files={"file": (name, (PACK / name).read_bytes())})
    assert up.status_code == 200, up.text
    up = up.json()
    r = c.post(f"{WS}/uploads/{up['id']}/mapping", headers=H, json={"mapping": up["proposed_mapping"]})
    assert r.status_code == 200, r.text
    return up, r.json()


def test_pdf_bank_statement_reconciles_like_csv(cpa):
    csv_run = upload_pack(cpa)
    cpa.post(WS + "/sources/bank/clear", headers=H)
    up, r = _upload_confirm(cpa, "bank", "11_bank_statement_October.pdf")
    assert "PDF table" in up["mapping_method"] and up["proposed_mapping"]["line_id"] == "Txn ID"
    assert r["health"]["accepted"] == 12 and r["health"]["rejected"] == [] and r["health"]["net_cents"] == 231060
    pdf_run = cpa.post(WS + "/reconciliation-runs", headers=H).json()
    assert pdf_run["result"]["matches"] == csv_run["result"]["matches"]
    assert [(e["bank_id"], e["type"]) for e in pdf_run["result"]["exceptions"]] == \
           [(e["bank_id"], e["type"]) for e in csv_run["result"]["exceptions"]]


def test_pdf_statement_with_paid_in_out_columns(cpa):
    cpa.post(WS + "/reset", headers=H)
    up, r = _upload_confirm(cpa, "bank", "12_bank_statement_paid_in_out.pdf")
    assert up["proposed_mapping"]["amount"] == "Amount" and up["proposed_mapping"]["date"] == "Date"
    assert r["health"]["accepted"] == 12 and r["health"]["net_cents"] == 231060
    assert up["preview"][0][1] == "2026-10-02"  # "02 Oct 2026" read as an ISO date


@pytest.mark.parametrize("name,ref,amount", [("13_invoice_PK-3302_packaging.pdf", "PK-3302", "420.00"),
                                             ("14_invoice_CH-7781_hosting.pdf", "CH-7781", "129.00"),
                                             ("15_invoice_TR-551_training.pdf", "TR-551", "310.00"),
                                             ("16_customer_invoice_INV-2105.pdf", "INV-2105", "1750.00")])
def test_invoice_pdfs_extract_reference_and_total(cpa, name, ref, amount):
    up = cpa.post(WS + "/documents", headers=H, files={"file": (name, (PACK / name).read_bytes())}).json()
    assert (up["fields"]["reference"]["value"], up["fields"]["amount"]["value"]) == (ref, amount)
    assert up["fields"]["amount"]["source_text"].startswith("Amount Due") and up["unsafe_content"] == []


def test_pdf_that_is_not_a_statement_is_refused(cpa):
    r = cpa.post(WS + "/uploads", headers=H, data={"kind": "bank"},
                 files={"file": ("x.pdf", (PACK / "13_invoice_PK-3302_packaging.pdf").read_bytes())})
    assert r.status_code == 422 and "table" in r.json()["detail"]


# ---- scenario 2: clean month, scenario 3: messy files ----------------------------------------------
def _health(c, folder, kind, name):
    r = c.post(WS + "/uploads", headers=H, data={"kind": kind}, files={"file": (name, (ROOT / folder / name).read_bytes())})
    if r.status_code != 200:
        return r
    u = r.json()
    return c.post(f"{WS}/uploads/{u['id']}/mapping", headers=H, json={"mapping": u["proposed_mapping"]}).json()["health"]


def test_clean_month_reports_no_problems(cpa):
    cpa.post(WS + "/reset", headers=H)
    for kind, name in [("bank", "01_bank_statement.csv"), ("ledger", "02_general_ledger.xlsx"),
                       ("ar", "03_open_invoices_AR.csv"), ("documents", "05_supporting_documents.csv")]:
        assert _health(cpa, "2_clean_month", kind, name)["rejected"] == []
    r = cpa.post(WS + "/reconciliation-runs", headers=H).json()["result"]
    assert len(r["matches"]) == 8 and r["exceptions"] == [] and r["variance_cents"] == 0


def test_messy_files_reject_bad_rows_with_reasons(cpa):
    cpa.post(WS + "/reset", headers=H)
    bank = _health(cpa, "3_messy_files", "bank", "01_bank_export_messy.csv")
    assert (bank["received"], bank["accepted"], bank["control_total_ok"]) == (8, 4, True)
    reasons = [x["reason"] for x in bank["rejected"]]
    assert any("2 decimal places" in x for x in reasons) and any("currency" in x for x in reasons)
    assert any("formula" in x for x in reasons) and any("duplicate source id" in x for x in reasons)
    ledger = _health(cpa, "3_messy_files", "ledger", "02_ledger_export_messy.tsv")
    assert (ledger["received"], ledger["accepted"]) == (5, 3)
    assert _health(cpa, "3_messy_files", "ledger", "03_meeting_notes_should_be_refused.docx").status_code == 422
    assert _health(cpa, "3_messy_files", "bank", "04_not_a_statement.json").status_code == 422
    r = cpa.post(WS + "/reconciliation-runs", headers=H).json()["result"]
    assert {(e["bank_id"], e["type"]) for e in r["exceptions"]} == {("MX07", "UNMATCHED_DEPOSIT"), ("MX02", "MISSING_RECEIPT"), ("MX06", "MISSING_RECEIPT")}
    assert r["unexplained_variance_cents"] == 0


def test_run_without_bank_and_ledger_says_what_is_missing(cpa):
    cpa.post(WS + "/reset", headers=H)
    r = cpa.post(WS + "/reconciliation-runs", headers=H)
    assert r.status_code == 422 and "Bank statement and General ledger" in r.json()["detail"] and "Sources" in r.json()["detail"]
    _health(cpa, "1_standard_month", "bank", "01_bank_statement.csv")
    r = cpa.post(WS + "/reconciliation-runs", headers=H)
    assert "Missing: General ledger." in r.json()["detail"]


def test_same_pdf_twice_is_flagged_as_duplicate(cpa):
    cpa.post(WS + "/reset", headers=H)
    blob = (PACK / "13_invoice_PK-3302_packaging.pdf").read_bytes()
    first = cpa.post(WS + "/documents", headers=H, files={"file": ("a.pdf", blob)}).json()
    second = cpa.post(WS + "/documents", headers=H, files={"file": ("a.pdf", blob)}).json()
    assert first["duplicate_of"] is None and second["duplicate_of"] == first["id"]
