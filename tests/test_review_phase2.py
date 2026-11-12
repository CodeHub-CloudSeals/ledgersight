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


# ---- #11 more real-world column layouts (deterministic; no AI) ----
import csv
import io


def _rows(name):
    return list(csv.DictReader(io.StringIO((FIXTURES / name).read_text())))


def _csv(header, rows):
    buf = io.StringIO(); w = csv.writer(buf, lineterminator="\n"); w.writerow(header); w.writerows(rows)
    return buf.getvalue()


def _auto(c, name, text):
    up = c.post(WS + "/uploads", headers=H, files={"file": (name, text.encode())})   # kind=auto: worked out from content
    assert up.status_code == 200, up.text
    up = up.json()
    r = c.post(f"{WS}/uploads/{up['id']}/mapping", headers=H, json={"mapping": up["proposed_mapping"]})
    assert r.status_code == 200, r.text
    return up, r.json()


def test_r11_paid_in_out_bank_and_signed_ledger_reconcile_exactly_like_the_golden_files(cpa):
    golden = golden_run(cpa)["result"]
    bank = [[r["line_id"], r["date"], r["description"], r["reference"],
             r["amount"] if float(r["amount"]) > 0 else "0.00", f"{-float(r['amount']):.2f}" if float(r["amount"]) < 0 else "",
             r["currency"]] for r in _rows("client-a_bank.csv")]
    ledger = [[r["entry_id"], r["posted_date"], r["account"], r["description"], r["reference"],
               r["debit"] or f"-{r['credit']}", r["currency"]] for r in _rows("client-a_ledger.csv")]
    up_b, res_b = _auto(cpa, "statement_paid_in_out.csv",
                        _csv(["Txn ID", "Date", "Description", "Reference", "Paid in", "Paid out", "Currency"], bank))
    up_l, res_l = _auto(cpa, "gl_signed_amount.csv",
                        _csv(["entry_id", "posted_date", "account", "description", "reference", "amount", "currency"], ledger))
    assert up_b["kind"] == "bank" and up_l["kind"] == "ledger"
    assert "paid_in" in up_b["required"] and "amount" not in up_b["required"]          # what the mapping step marks
    assert res_b["health"]["rejected"] == [] and res_l["health"]["rejected"] == []
    run = cpa.post(WS + "/reconciliation-runs", headers=H).json()["result"]
    key = lambda res: (sorted((e["type"], e["bank_id"] or "", e["amount_cents"]) for e in res["exceptions"]),
                       sorted((m["bank_id"], m["ledger_id"]) for m in res["matches"]), res["variance_cents"])
    assert key(run) == key(golden)                                                     # same money result


def test_r11_bank_debit_credit_columns_and_bad_rows(cpa):
    assert cpa.post(WS + "/reset", headers=H).status_code == 200
    text = _csv(["line_id", "date", "description", "reference", "debit", "credit", "currency"],
                [["B1", "2026-10-02", "DEPOSIT", "R1", "", "250.00", "USD"],
                 ["B2", "2026-10-03", "CARD", "R2", "40.00", "0.00", "USD"],          # 0.00 in the other column is fine
                 ["B3", "2026-10-04", "BOTH", "R3", "10.00", "10.00", "USD"]])         # both filled: refused, with a reason
    up, res = _auto(cpa, "bank_dr_cr.csv", text)
    assert up["kind"] == "bank"
    rows = {r["id"]: r["amount_cents"] for r in appmod.svc.ws["client-a"].batches["bank"]["rows"]}
    assert rows == {"B1": 25000, "B2": -4000}
    assert [x["row"] for x in res["health"]["rejected"]] == [4] and "paid in / paid out" in res["health"]["rejected"][0]["reason"]


def test_r11_ledger_zero_in_unused_column_is_accepted(cpa):
    assert cpa.post(WS + "/reset", headers=H).status_code == 200
    text = _csv(["entry_id", "posted_date", "account", "description", "reference", "debit", "credit", "currency"],
                [["L1", "2026-10-02", "Sales", "receipt", "R1", "250.00", "0.00", "USD"],
                 ["L2", "2026-10-03", "Supplies", "card", "R2", "0", "40.00", "USD"]])
    up, res = _auto(cpa, "gl_zeros.csv", text)
    assert up["kind"] == "ledger" and res["health"]["rejected"] == []
    assert {r["id"]: r["amount_cents"] for r in appmod.svc.ws["client-a"].batches["ledger"]["rows"]} == {"L1": 25000, "L2": -4000}


# ---- #5 (labels) a non-USD book's reports are labelled and formatted in its own currency ----
def test_r05_gbp_book_reports_use_gbp_labels_and_formats(cpa):
    import openpyxl
    import pdfplumber
    assert cpa.post(WS + "/reset", headers=H).status_code == 200
    _auto(cpa, "gbp_bank.csv", _csv(["line_id", "date", "description", "reference", "amount", "currency"],
                                    [["G1", "2026-10-02", "FPS CREDIT CUSTOMER 01", "INV-G1", "500.00", "GBP"],
                                     ["G2", "2026-10-05", "CARD PRINT SUPPLIER", "PS-9", "-40.00", "GBP"]]))
    _auto(cpa, "gbp_gl.csv", _csv(["entry_id", "posted_date", "account", "description", "reference", "debit", "credit", "currency"],
                                  [["L1", "2026-10-02", "Sales", "receipt", "INV-G1", "500.00", "", "GBP"]]))
    run = cpa.post(WS + "/reconciliation-runs", headers=H).json()
    assert run["result"]["base_currency"] == "GBP"
    base = f"{WS}/runs/{run['id']}/reports/finance"
    fin = cpa.get(base + ".json").json()
    keys = [k for k, _ in fin["summary"]]
    assert fin["currency"] == "GBP" and "Variance (GBP)" in keys and not any("(USD)" in k for k in keys)
    assert all("(USD)" not in c for s in fin["sections"] for c in s["columns"])
    wb = openpyxl.load_workbook(io.BytesIO(cpa.get(base + ".xlsx").content))
    fmts = {c.number_format for row in wb["Summary"].iter_rows() for c in row if isinstance(c.value, (int, float))}
    assert any("GBP" in f for f in fmts) and not any("$" in f for f in fmts)
    with pdfplumber.open(io.BytesIO(cpa.get(base + ".pdf").content)) as pdf:
        txt = "\n".join(p.extract_text() or "" for p in pdf.pages)
    assert "GBP 460.00" in txt and "$460.00" not in txt and "Variance" in txt
    # a USD book is unchanged
    usd = golden_run(cpa)
    assert "Variance (USD)" in dict(cpa.get(f"{WS}/runs/{usd['id']}/reports/finance.json").json()["summary"])
