"""Report presentation: the PDF and Excel files are laid out for people, the data stays identical.

JSON and CSV are machine formats and are not restyled; the PDF/XLSX only change how values are shown."""
import io

import pytest

from test_ledgersight import golden_run, make_user  # noqa: F401
from ledgersight.core import reports

WS = "/v1/workspaces/client-a"


@pytest.fixture(scope="module")
def rev():
    return make_user("report-format@test", "owner_reviewer", ["client-a"])


def _pdf_text(blob: bytes) -> str:
    import pdfplumber
    with pdfplumber.open(io.BytesIO(blob)) as pdf:
        return "\n".join(p.extract_text() or "" for p in pdf.pages)


def test_finance_pdf_has_header_key_figures_and_page_numbers(rev):
    run = golden_run(rev)
    fin = rev.get(f"{WS}/runs/{run['id']}/reports/finance.json").json()
    txt = _pdf_text(rev.get(f"{WS}/runs/{run['id']}/reports/finance.pdf").content)
    s = dict(fin["summary"])
    assert "Finance Reconciliation Report" in txt and run["id"] in txt and fin["report_id"] in txt
    assert reports._usd(s["Variance (USD)"]) in txt  # key figure shown as $1,234.56
    assert "Still owed on invoices" in txt and "$300.00" in txt
    assert "Page 1 of" in txt and "Read-only review output" in txt
    assert "PARTIAL RECEIPT" in txt  # exception codes readable, still uppercase
    for e in run["result"]["exceptions"]:
        assert e["id"] in txt  # every exception is in the PDF


def test_governance_pdf_shows_decisions_and_evidence_state(rev):
    run = golden_run(rev)
    txt = _pdf_text(rev.get(f"{WS}/runs/{run['id']}/reports/governance.pdf").content)
    assert "TrustOps Governance Report" in txt and "Evidence chain" in txt and "PASS" in txt
    assert "Agent steps in this run" in txt and "ESCALATE" in txt


def test_json_data_is_unchanged_by_presentation(rev):
    run = golden_run(rev)
    fin = rev.get(f"{WS}/runs/{run['id']}/reports/finance.json").json()
    s = dict(fin["summary"])
    assert s["Outstanding on partial receipts (USD)"] == "300.00"  # raw value, no $ or commas
    types = {r[0] for r in fin["sections"][1]["rows"]}
    assert "PARTIAL_RECEIPT" in types  # machine codes kept as-is


def test_xlsx_uses_real_money_numbers_and_frozen_headers(rev):
    from openpyxl import load_workbook
    run = golden_run(rev)
    wb = load_workbook(io.BytesIO(rev.get(f"{WS}/runs/{run['id']}/reports/finance.xlsx").content))
    summ = {r[0].value: r[1] for r in wb["Summary"].iter_rows(min_row=4) if r[0].value}
    cell = summ["Variance (USD)"]
    assert isinstance(cell.value, float) and "$" in cell.number_format
    sheet = wb["Exceptions and reviewer status"]
    assert sheet.freeze_panes == "A2" and sheet["A1"].font.bold
    amt_col = [c.value for c in sheet[1]].index("Amount (USD)") + 1
    assert "$" in sheet.cell(row=2, column=amt_col).number_format


def test_lowercase_client_name_is_capitalised_in_pdf_only():
    assert reports._proper("cloud seals") == "Cloud Seals" and reports._proper("ai tech") == "AI Tech"
    assert reports._proper("eBay") == "eBay" and reports._proper("Client 1") == "Client 1"


def test_prose_amounts_and_sentence_case():
    assert reports._prose("received 900.00 against invoice INV-1005 open 1200.00") == \
        "Received $900.00 against invoice INV-1005 open $1,200.00"
    assert reports._prose("policy trustops-policy-2026.11.1") == "Policy trustops-policy-2026.11.1"  # versions untouched


def test_long_words_never_split_when_they_fit():
    w = reports._tw("exception.triage", "F1", 7.5)
    assert reports._wrap("exception.triage", "F1", 7.5, w - 0.01) == ["exception.triage"]


# ---- CloudSeals logo on reports ---------------------------------------------------------------------
def test_pdf_reports_carry_the_cloudseals_logo_and_name(rev):
    run = golden_run(rev)
    for kind in ("finance", "governance"):
        blob = rev.get(f"{WS}/runs/{run['id']}/reports/{kind}.pdf").content
        assert b"/XObject << /Logo" in blob and b"/SMask" in blob  # logo image with transparent background
        txt = _pdf_text(blob)
        assert "CLOUDSEALS" in txt and "CloudSeals" in txt  # header wordmark + footer


def test_xlsx_summary_has_the_logo(rev):
    from openpyxl import load_workbook
    run = golden_run(rev)
    wb = load_workbook(io.BytesIO(rev.get(f"{WS}/runs/{run['id']}/reports/finance.xlsx").content))
    assert len(wb["Summary"]._images) == 1 and "CloudSeals" in wb["Summary"]["A2"].value


def test_reports_still_render_if_the_logo_file_is_missing(rev, monkeypatch, tmp_path):
    run = golden_run(rev)
    monkeypatch.setattr(reports, "LOGO_PNG", tmp_path / "missing.png")
    monkeypatch.setattr(reports, "_LOGO_CACHE", {})
    blob = rev.get(f"{WS}/runs/{run['id']}/reports/finance.pdf").content
    assert blob.startswith(b"%PDF") and b"/Logo" not in blob and run["id"] in _pdf_text(blob)
    assert rev.get(f"{WS}/runs/{run['id']}/reports/finance.xlsx").status_code == 200
