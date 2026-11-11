"""Regression tests for the 2026-11-11 readiness audit (finding ids in each test name / docstring)."""
import io
import zipfile
from datetime import date
from pathlib import Path

import pytest

from test_ledgersight import H, appmod, golden_run, make_user  # noqa: F401
from ledgersight.core import ingest

ROOT = Path(__file__).resolve().parent.parent
WS = "/v1/workspaces/client-a"


@pytest.fixture(scope="module")
def owner():
    return make_user("audit-owner@demo.test", "owner_reviewer", ["client-a"])


@pytest.fixture(scope="module")
def prep():
    return make_user("audit-prep@demo.test", "preparer", ["client-a"])


def up(c, name, blob, kind="auto"):
    return c.post(WS + "/uploads", headers=H, data={"kind": kind}, files={"file": (name, blob)})


# ---- SEC-01: sample route path traversal (backslash) -----------------------------
@pytest.mark.parametrize("name", ["client-a_%5C..%5C..%5C..%5C.env", "client-a_..%5C..%5Cpyproject.toml",
                                  "client-a_%5C..%5Cexpectations.json"])
def test_sec01_sample_route_refuses_traversal(owner, name):
    assert owner.get(f"{WS}/samples/{name}").status_code == 404


# ---- SEC-02: review CSV export goes through the formula guard ----------------------
def test_sec02_review_csv_neutralises_formulas(owner):
    run = golden_run(owner)
    owner.post(WS + "/profile", headers=H, json={"name": "@SUM(1+1)*cmd", "period": "October 2026"})
    exc = next(e for e in run["result"]["exceptions"] if e["status"] == "OPEN")
    owner.post(f"{WS}/runs/{run['id']}/exceptions/{exc['id']}/decisions", headers=H,
               json={"decision": "REJECT", "note": "=HYPERLINK(\"http://x\",\"y\")"})
    body = owner.get(f"{WS}/runs/{run['id']}/export.csv").text
    assert "'=HYPERLINK" in body and "'@SUM(1+1)" in body
    assert "\n=HYPERLINK" not in body and ",=HYPERLINK" not in body and ",@SUM" not in body
    owner.post(WS + "/profile", headers=H, json={"name": "Client 1", "period": "October 2026"})


# ---- SEC-08 / PROD-14: local data folders never leave the machine -------------------
@pytest.mark.parametrize("f", [".gitignore", ".dockerignore", ".gcloudignore"])
def test_sec08_data_folders_ignored(f):
    assert ".ledgersight-data*/" in (ROOT / f).read_text(encoding="utf-8")


# ---- FUNC-05 / PROD-12: malformed input is refused with a reason, never a 500 -------
@pytest.mark.parametrize("raw", ["inf", "-Infinity", "NaN", "1E400", "99999999999999999999.99"])
def test_func05_non_finite_or_huge_amounts_rejected(raw):
    with pytest.raises(ValueError):
        ingest.to_cents(raw)


@pytest.mark.parametrize("raw,cents", [("1,234.56", 123456), ("1.234,56", 123456), ("1,50", 150), ("(12.00)", -1200),
                                        ("$12.00", 1200), ("-0,5", -50), ("1e5", 10000000)])
def test_amount_formats_never_misread(raw, cents):
    assert ingest.to_cents(raw) == cents


@pytest.mark.parametrize("raw", ["12,3456", "1,23,456", "1,2,3"])
def test_ambiguous_amounts_refused(raw):
    with pytest.raises(ValueError):
        ingest.to_cents(raw)


def test_func05_csv_with_huge_field_is_422(owner):
    blob = ("line_id,date,description,reference,amount,currency\nB1,2026-10-01," + "x" * 200000 + ",R1,10.00,USD\n").encode()
    r = up(owner, "bank.csv", blob)
    assert r.status_code == 422 and "CSV could not be read" in r.text


def test_func05_corrupt_and_fake_xlsx_are_422(owner):
    assert up(owner, "broken.xlsx", b"PK\x03\x04garbage").status_code == 422
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("hello.txt", "not a workbook")
    r = up(owner, "zip.xlsx", buf.getvalue())
    assert r.status_code == 422, r.text


def test_prod12_ask_rejects_non_text_question(owner):
    assert owner.post(WS + "/ask", headers=H, json={"question": 123}).status_code == 422
    assert owner.post(WS + "/ask", headers=H, json={"question": "x" * 5000}).status_code == 422


# ---- FUNC-06: US / UK dates; FUNC-12: UTF-16 and semicolon CSVs ---------------------
def test_func06_us_and_uk_dates():
    assert ingest.parse_date("10/05/2026") == date(2026, 10, 5)
    assert ingest.parse_date("10/05/2026", "DMY") == date(2026, 5, 10)
    assert ingest.date_order(["01/02/2026", "25/02/2026"]) == "DMY"
    assert ingest.date_order(["10/05/2026", "10/31/2026"]) == "MDY"
    with pytest.raises(ValueError):
        ingest.parse_date("13/31/2026")


def test_func06_us_date_bank_file_imports(owner):
    blob = b"line_id,date,description,reference,amount,currency\nB1,10/05/2026,Coffee,R1,-4.50,USD\nB2,10/31/2026,Deposit,R2,100.00,USD\n"
    r = up(owner, "us_dates.csv", blob)
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "bank"


def test_func12_utf16_and_semicolon_csv():
    text = "line_id,date,description,reference,amount,currency\nB1,2026-10-01,Fee,R1,-5.00,USD\n"
    h, rows = ingest.read_table("u16.csv", text.encode("utf-16"))
    assert h[0] == "line_id" and rows[0][0] == "B1"
    semi = "line_id;date;description;reference;amount;currency\nB1;2026-10-01;Fee;R1;-5,00;EUR\n".encode()
    h, rows = ingest.read_table("semi.csv", semi)
    assert h == ["line_id", "date", "description", "reference", "amount", "currency"] and rows[0][4] == "-5,00"
    assert ingest.to_cents(rows[0][4]) == -500


# ---- PROD-10: font served with a font content type ----------------------------------
def test_prod10_font_mime(owner):
    r = owner.get("/static/vendor/fonts/urbanist-latin-wght-normal.woff2")
    assert r.status_code == 200 and r.headers["content-type"].startswith("font/woff2")
