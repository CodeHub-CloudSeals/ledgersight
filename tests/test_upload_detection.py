"""Files come from the user's device / Drive / email, never from a built-in loader.

The server works out what each file is from its CONTENT (columns + rows), refuses anything that is not a
recognised finance record with the reason, and routes non-table PDFs to receipt/invoice intake.
"""
import json
from pathlib import Path

import pytest

from test_ledgersight import EXP, H, appmod, make_user  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
PACK = ROOT / "demo_files" / "0_client1_october"
DEMO = ROOT / "demo_files"
WS = "/v1/workspaces/client-a"


@pytest.fixture(scope="module")
def cpa():
    return make_user("uploader@demo.test", "owner_reviewer", ["client-a"])


def up(c, path, name=None, hint=None):
    data = {"kind": "auto"} | ({"hint": hint} if hint else {})
    return c.post(WS + "/uploads", headers=H, data=data, files={"file": (name or path.name, path.read_bytes())})


@pytest.mark.parametrize("fname,kind", [
    ("01_bank_statement_October.csv", "bank"), ("02_general_ledger_October.xlsx", "ledger"),
    ("03_open_invoices.csv", "ar"), ("04_vendor_bills.csv", "ap"),
    ("05_supporting_documents.csv", "documents"), ("06_customers_and_vendors.csv", "parties")])
def test_type_comes_from_content_not_file_name(cpa, fname, kind):
    cpa.post(WS + "/reset", headers=H)
    ext = fname.rsplit(".", 1)[1]
    r = up(cpa, PACK / fname, name=f"export (3).{ext}", hint="bank")  # misleading name and hint
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == kind and r.json()["detected"] is True


def test_misleading_hint_never_overrides_content(cpa):
    r = up(cpa, PACK / "02_general_ledger_October.xlsx", name="bank_statement.xlsx", hint="bank")
    assert r.json()["kind"] == "ledger"


@pytest.mark.parametrize("path", [PACK / "08_staff_rota_should_be_refused.csv", DEMO / "3_messy_files" / "04_not_a_statement.json"])
def test_wrong_content_is_refused_with_reason(cpa, path):
    r = up(cpa, path)
    assert r.status_code == 422
    d = r.json()["detail"]
    assert "Not accepted" in d or "JSON must be a list" in d
    if path.suffix == ".csv":
        assert "Columns found: employee, role" in d and "missing" in d


@pytest.mark.parametrize("path", [DEMO / "3_messy_files" / "03_meeting_notes_should_be_refused.docx",
                                  DEMO / "1_standard_month" / "10_should_be_rejected_macro.xlsm"])
def test_unsupported_or_dangerous_formats_refused(cpa, path):
    assert up(cpa, path).status_code == 422


def test_messy_headers_still_recognised(cpa):
    r = up(cpa, DEMO / "3_messy_files" / "01_bank_export_messy.csv", name="download.csv")
    assert r.status_code == 200 and r.json()["kind"] == "bank"


def test_pdf_statement_detected_and_receipt_routed(cpa):
    st = up(cpa, DEMO / "1_standard_month" / "11_bank_statement_October.pdf", name="scan_0042.pdf")
    assert st.status_code == 200, st.text
    assert st.json()["kind"] == "bank"
    rc = up(cpa, PACK / "07_receipt_OD-7741.pdf", name="document.pdf")
    assert rc.status_code == 200 and rc.json()["routed"] == "document" and rc.json()["kind"] == "pdf"
    assert rc.json()["fields"]["reference"]["value"] == "OD-7741"


def test_full_month_from_uploaded_files_matches_golden_results(cpa):
    """The Monday journey with no built-in data: upload the pack, confirm each mapping, run."""
    cpa.post(WS + "/reset", headers=H)
    for i, f in enumerate(sorted(PACK.glob("0[1-6]_*"))):
        r = up(cpa, f, name=f"file{i}{f.suffix}")  # names carry no meaning
        assert r.status_code == 200, (f.name, r.text)
        u = r.json()
        c = cpa.post(f"{WS}/uploads/{u['id']}/mapping", headers=H, json={"mapping": u["proposed_mapping"]}).json()
        assert c["state"] == "VALIDATED" and c["health"]["rejected"] == [], (f.name, c)
    run = cpa.post(WS + "/reconciliation-runs", headers=H).json()
    res, e = run["result"], EXP["client-a"]
    assert {m["bank_id"]: m["ledger_id"] for m in res["matches"]} == e["matches"]
    assert {(x["bank_id"], x["type"]) for x in res["exceptions"]} == set(e["exceptions"].items())
    assert res["variance_cents"] == e["variance_cents"] and res["unexplained_variance_cents"] == 0
    src = {k["kind"]: k for k in cpa.get(WS + "/sources").json()["kinds"]}
    assert src["bank"]["status"] == "VERIFIED_UPLOAD" and src["bank"]["envelope"]["origin"] == "upload"


def test_built_in_loader_is_off_unless_enabled(cpa, monkeypatch):
    monkeypatch.setenv("AVOCET_SHOW_SAMPLES", "0")
    assert cpa.post(WS + "/samples/load", headers=H).status_code == 404
    assert cpa.get(WS + "/samples").status_code == 404
    assert cpa.get(WS + "/samples/client-a_bank.csv").status_code == 404


def test_ui_refreshes_status_after_uploads():
    """Regression: after uploading from the device, the Run button stayed disabled ("upload bank statement and
    ledger first") because only the removed built-in loader refreshed the book's status."""
    js = (ROOT / "ledgersight" / "static" / "avocet.js").read_text(encoding="utf-8")
    line = next(l for l in js.splitlines() if l.startswith("async function refreshWs()"))
    assert "await progress()" in line
    assert 'if (t.dataset.tab === "recon") progress();' in js


def test_all_three_intake_routes_are_offered():
    """Load All Files (built-in synthetic set), upload from this device, and Google Drive/Sheets/Docs/Slides."""
    html = (ROOT / "ledgersight" / "static" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "ledgersight" / "static" / "avocet.js").read_text(encoding="utf-8")
    assert 'id="sample-btn"' in html and "Load All Files" in html and "/samples/load" in js
    assert 'id="choose-btn"' in html and 'id="dz-drive-btn"' in html
    for kind in ("spreadsheet", "document", "presentation"):  # Sheets -> xlsx, Docs/Slides -> PDF, then content detection
        assert f"application/vnd.google-apps.{kind}" in js
    assert '"AVOCET_SHOW_SAMPLES": "1"' in (ROOT / ".vscode" / "launch.json").read_text(encoding="utf-8")
    assert "AVOCET_SHOW_SAMPLES=1" in (ROOT / "deploy" / "cloudrun.sh").read_text(encoding="utf-8")
    assert json.loads((ROOT / ".vscode" / "launch.json").read_text(encoding="utf-8"))  # still valid JSON


def test_load_all_files_gives_the_same_result_as_uploading_the_pack(cpa):
    cpa.post(WS + "/reset", headers=H)
    assert cpa.post(WS + "/samples/load", headers=H).status_code == 200
    a = cpa.post(WS + "/reconciliation-runs", headers=H).json()["result"]
    cpa.post(WS + "/reset", headers=H)
    for f in sorted(PACK.glob("0[1-6]_*")):
        u = up(cpa, f).json()
        cpa.post(f"{WS}/uploads/{u['id']}/mapping", headers=H, json={"mapping": u["proposed_mapping"]})
    b = cpa.post(WS + "/reconciliation-runs", headers=H).json()["result"]
    assert a["matches"] == b["matches"] and [(e["type"], e["case_id"]) for e in a["exceptions"]] == \
        [(e["type"], e["case_id"]) for e in b["exceptions"]]
    assert a["variance_cents"] == b["variance_cents"] and b["unexplained_variance_cents"] == 0
