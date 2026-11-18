"""Action items 2026-11-16 (lead's list + LedgerSight <-> TrustOps integration findings).

1 FinTwin scrolling · 2 no deleting files under review work · 3 one date format · 4 earlier runs' reports ·
5 Export reviews · 6 select all + bulk send · 7 runs list by day / month / person · 8 Overview across all runs ·
9 rejected rows are not "validated" · 10 never send to yourself · 11 TrustOps down does not hang pages ·
12 FinTwin says why TrustOps stopped it · 13 PDF confirm refuses bad input with 422."""
import time
import urllib.error
from pathlib import Path

import pytest

from test_audit_fixes_p1 import PACK, WS, acknowledge_rejections, pack_run, upload_confirm  # noqa: F401
from test_ledgersight import H, appmod, make_user  # noqa: F401
from test_trustops_integration import FakeTrustOps
from ledgersight import trustops_client
from ledgersight.core import reports

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "ledgersight" / "static" / "avocet.js").read_text(encoding="utf-8")
CSS = (ROOT / "ledgersight" / "static" / "avocet.css").read_text(encoding="utf-8")
HTML = (ROOT / "ledgersight" / "static" / "index.html").read_text(encoding="utf-8")


@pytest.fixture()
def tops(monkeypatch):
    fake = FakeTrustOps()
    monkeypatch.setenv("TRUSTOPS_URL", "https://trustops.example.test")
    monkeypatch.setenv("TRUSTOPS_KEY", "tok_test_key")
    monkeypatch.setattr(trustops_client, "decide", fake.decide)
    monkeypatch.setattr(trustops_client, "decide_many", trustops_client.decide_each)
    monkeypatch.setattr(trustops_client, "status", fake.status)
    monkeypatch.setattr(trustops_client, "app_access", lambda email, max_age=60: "reviewer")
    monkeypatch.setattr(trustops_client, "reviewers", lambda max_age=30: [
        {"email": "approver@firm.test", "role": "Approver"}, {"email": "ai-cpa@demo.test", "role": "Admin"}])
    monkeypatch.setattr(trustops_client, "assign", lambda did, to, by: {"decision_id": did, "assigned_to": to})
    return fake


@pytest.fixture(scope="module")
def cpa():
    return make_user("ai-cpa@demo.test", "owner_reviewer", ["client-a"])


@pytest.fixture(scope="module")
def prep():
    return make_user("ai-prep@demo.test", "preparer", ["client-a"])


def _excs(c, run):
    return c.get(f"{WS}/runs/{run['id']}", headers=H).json()["result"]["exceptions"]


# ---- 2. files under review work cannot be deleted --------------------------------------------------------
def test_files_cannot_be_cleared_once_review_work_exists(cpa, prep):
    run = pack_run(cpa)
    assert prep.post(WS + "/sources/ap/clear", headers=H).status_code == 200        # no review work yet: allowed
    run = pack_run(cpa)
    e0 = run["result"]["exceptions"][0]
    r = prep.post(f"{WS}/runs/{run['id']}/exceptions/{e0['id']}/proposals", headers=H, json={"note": "matches the receipt"})
    assert r.status_code == 200, r.text
    for who in (prep, cpa):                                                          # CPA reviewer included
        r = who.post(WS + "/sources/bank/clear", headers=H)
        assert r.status_code == 403 and r.json()["detail"]["reason_code"] == "DECISIONS_EXIST"
    assert cpa.get(f"{WS}/runs/{run['id']}", headers=H).status_code == 200          # nothing was withdrawn
    s = cpa.get(WS + "/sources", headers=H).json()
    assert s["locked"] is True and "new period" in s["locked_reason"]
    assert cpa.post(WS + "/reset", headers=H).status_code == 200                     # a new period still works
    assert cpa.get(WS + "/sources", headers=H).json()["locked"] is False


def test_attached_proof_also_locks_the_files(cpa):
    run = pack_run(cpa)
    e0 = run["result"]["exceptions"][0]
    up = cpa.post(f"{WS}/runs/{run['id']}/exceptions/{e0['id']}/attachments", headers=H,
                  files={"file": ("note.txt", b"bank letter", "text/plain")})
    assert up.status_code == 200, up.text
    assert cpa.post(WS + "/sources/ledger/clear", headers=H).status_code == 403


# ---- 3. one date format ----------------------------------------------------------------------------------
def test_shared_date_format_helpers():
    assert reports.fmt_date("2026-11-16") == "16 Nov 2026" and reports.fmt_date("2026-01-05") == "5 Jan 2026"
    assert reports.fmt_datetime("2026-11-16T14:05:09.123+00:00") == "16 Nov 2026, 14:05 UTC"
    assert reports.fmt_datetime("2026-11-16T15:05:00+01:00") == "16 Nov 2026, 14:05 UTC"   # shown in UTC
    assert reports.fmt_datetime(0) == "1 Jan 1970, 00:00 UTC"
    assert reports.fmt_any("HB005") == "HB005" and reports.fmt_any(12) == 12 and reports.fmt_date("2026-13-01") == "2026-13-01"


def test_reports_use_the_shared_date_format(cpa):
    run = pack_run(cpa)
    csv_ = cpa.get(f"{WS}/runs/{run['id']}/reports/finance.csv", headers=H).content.decode("utf-8-sig")
    gen = next(line for line in csv_.splitlines() if line.startswith("Generated at"))
    import re
    assert re.search(r'"\d{1,2} (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{4}, \d{2}:\d{2} UTC"', gen), gen
    review = cpa.get(f"{WS}/runs/{run['id']}/export.csv", headers=H).content.decode("utf-8-sig")
    gen = next(line for line in review.splitlines() if line.startswith("Generated at"))
    assert " UTC" in gen and "+00:00" not in gen
    # the page uses one helper for every date (the TrustOps console uses the same wording)
    assert "function fmtDate(" in JS and "function fmtDateTime(" in JS
    assert 'en-GB' in JS.split("function fmtDateTime(", 1)[1][:600]


# ---- 4 / 7 / 8. every run: list, filters, totals, earlier reports ---------------------------------------------
def test_runs_list_filters_and_totals(cpa, prep):
    first = pack_run(cpa)
    fixed = (PACK / "01_bank_statement_October.csv").read_bytes().replace(b"RENT OCT LANDLORD", b"RENT OCTOBER LANDLORD", 1)
    upload_confirm(prep, "01_bank_statement_October_v2.csv", fixed)
    second = prep.post(WS + "/reconciliation-runs", headers=H).json()
    assert second["id"] != first["id"]
    e0 = first["result"]["exceptions"][0]
    assert cpa.post(f"{WS}/runs/{first['id']}/exceptions/{e0['id']}/decisions", headers=H,
                    json={"decision": "ACCEPT", "note": "checked"}).status_code == 200
    out = cpa.get(WS + "/runs", headers=H).json()
    ids = [x["id"] for x in out["items"]]
    assert ids == [second["id"], first["id"]] and out["latest_run"] == second["id"] and out["items"][0]["latest"]
    assert set(out["people"]) == {"ai-cpa@demo.test", "ai-prep@demo.test"}
    one = next(x for x in out["items"] if x["id"] == first["id"])
    assert one["reviews"]["approved"] == 1 and one["review_items"] == len(first["result"]["exceptions"])
    assert out["totals"]["review_items"] == sum(x["review_items"] for x in out["items"])
    assert out["totals"]["approved"] == 1 and out["totals"]["open"] == out["totals"]["review_items"] - 1
    assert one["files"]["bank"] == "01_bank_statement_October.csv"
    # filters: who ran it, day, month (UTC dates)
    assert [x["id"] for x in cpa.get(WS + "/runs?by=ai-prep@demo.test", headers=H).json()["items"]] == [second["id"]]
    day = first["created_at"][:10]
    assert len(cpa.get(f"{WS}/runs?day={day}", headers=H).json()["items"]) == 2
    assert cpa.get(f"{WS}/runs?month={day[:7]}", headers=H).json()["total"] == 2
    assert cpa.get(WS + "/runs?month=1999-01", headers=H).json()["items"] == []
    assert cpa.get(WS + "/runs?day=yesterday", headers=H).status_code == 422
    # an earlier run's reports still download
    r = cpa.get(f"{WS}/runs/{first['id']}/reports/finance.pdf", headers=H)
    assert r.status_code == 200 and r.content[:4] == b"%PDF"


def test_reports_overview_and_reconciliation_pages_offer_every_run():
    assert 'id="rep-run-pick"' in HTML and "function renderRunPicker(" in JS and "S.repRun" in JS
    assert 'id="runs-list"' in HTML and "function renderRunsList(" in JS
    body = JS.split("function renderRunsList(", 1)[1][:4000]
    assert "runs-day" in body and "runs-month" in body and "runs-by" in body
    assert "function renderAllRunsSummary(" in JS and 'id="ov-allruns"' in HTML


# ---- 5. Export reviews: one set of options; the review pack can be read on screen ------------------------------
def test_export_reviews_has_one_set_of_options():
    body = JS.split("async function renderReportsFor()", 1)[1].split(chr(10) + "}" + chr(10), 1)[0]
    assert body.count("export.csv") == 1 and body.count("review-pack.json") == 1 and body.count("pack.html") == 1
    assert "View review pack" in body and '"Export reviews"' in body
    assert 'id="exp-csv"' not in HTML and 'id="exp-pack"' not in HTML and 'id="exp-print"' not in HTML   # no second copy
    assert '$("exp-csv")' not in JS and "exp-print" not in JS
    pack = (ROOT / "ledgersight" / "static" / "pack.js").read_text(encoding="utf-8")
    assert "Download JSON" in pack and "toLocaleString()" not in pack and "Review decisions" in pack


# ---- 6. bulk send + 10. never to yourself ------------------------------------------------------------------
def test_bulk_send_to_trustops(tops, cpa, prep):
    run = pack_run(cpa)
    ids = [e["id"] for e in run["result"]["exceptions"]]
    url = f"{WS}/runs/{run['id']}/send-to-trustops"
    assert prep.post(url, headers=H, json={"exception_ids": ids}).status_code == 422                       # approver needed
    assert prep.post(url, headers=H, json={"exception_ids": [], "to": "approver@firm.test"}).status_code == 422
    assert prep.post(url, headers=H, json={"exception_ids": ids, "to": "nobody@else.test"}).status_code == 422
    assert not [c for c in tops.calls if c[2] == "submit_for_review"]                                      # nothing went
    # one already sent: skipped, never sent twice
    one = prep.post(f"{WS}/runs/{run['id']}/exceptions/{ids[0]}/send-to-trustops", headers=H, json={"to": "approver@firm.test"})
    assert one.status_code == 200
    r = prep.post(url, headers=H, json={"exception_ids": ids + ["EX-999"], "to": "approver@firm.test", "note": "October batch"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["sent"] == len(ids) - 1 and out["skipped"] == 2 and out["not_sent"] == 0
    res = {x["exception_id"]: x for x in out["results"]}
    assert res[ids[0]]["result"] == "skipped" and "Already sent" in res[ids[0]]["reason"]
    assert res["EX-999"]["result"] == "skipped"
    subs = [c for c in tops.calls if c[2] == "submit_for_review"]
    assert len(subs) == len(ids) and all(c[3]["assign_to"] == "approver@firm.test" for c in subs)    # approver in the same call
    signed = [e for e in appmod.svc.ws["client-a"].log.events if e["action"] == "exception.sent_to_trustops" and e["run_id"] == run["id"]]
    assert len(signed) == len(ids)                                                                          # each one signed
    assert all(e["trustops_review"]["assigned_to"] == "approver@firm.test" for e in _excs(cpa, run))
    assert "function sendSelected(" in JS and "Select all" in JS and "Send to TrustOps" in JS


def test_nobody_sends_or_reassigns_an_item_to_themself(tops, cpa):
    run = pack_run(cpa)
    e0, e1 = run["result"]["exceptions"][:2]
    r = cpa.post(f"{WS}/runs/{run['id']}/exceptions/{e0['id']}/send-to-trustops", headers=H, json={"to": "AI-CPA@demo.test"})
    assert r.status_code == 422 and "yourself" in r.text
    r = cpa.post(f"{WS}/runs/{run['id']}/send-to-trustops", headers=H, json={"exception_ids": [e0["id"]], "to": "ai-cpa@demo.test"})
    assert r.status_code == 422 and "yourself" in r.text
    assert not [c for c in tops.calls if c[2] == "submit_for_review"]
    assert cpa.post(f"{WS}/runs/{run['id']}/exceptions/{e1['id']}/send-to-trustops", headers=H,
                    json={"to": "approver@firm.test"}).status_code == 200
    r = cpa.post(f"{WS}/runs/{run['id']}/exceptions/{e1['id']}/reviewer", headers=H, json={"to": "ai-cpa@demo.test"})
    assert r.status_code == 422 and "maker-checker" in r.text


def test_trustops_refusals_are_shown_in_its_own_words(monkeypatch):
    """TrustOps answers 4xx with {"error", "message"}: LedgerSight passes the message on."""
    import io
    monkeypatch.setenv("TRUSTOPS_URL", "https://trustops.example.test")
    monkeypatch.setenv("TRUSTOPS_KEY", "tok_test_key")
    trustops_client.reset_breaker()

    def refuse(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 422, "x", {}, io.BytesIO(
            b'{"detail": {"error": "not_a_reviewer", "message": "This person is not an Admin or Approver in TrustOps"}}'))
    monkeypatch.setattr(trustops_client.urllib.request, "urlopen", refuse)
    t = trustops_client.decide("human", "a@b.test", "submit_for_review", assign_to="x@y.test")
    assert t["decision"] == "DENY" and t["error"] == "not_a_reviewer" and "not an Admin or Approver" in t["reason"]
    assert not trustops_client.breaker_open()                     # an answer means TrustOps is up
    with pytest.raises(ValueError, match="not an Admin or Approver"):
        trustops_client.assign("dec_1", "x@y.test", "a@b.test")


# ---- 11. TrustOps unreachable: answers at once, fail closed ---------------------------------------------------
def test_circuit_breaker_stops_waiting_on_an_unreachable_trustops(monkeypatch):
    monkeypatch.setenv("TRUSTOPS_URL", "https://trustops.example.test")
    monkeypatch.setenv("TRUSTOPS_KEY", "tok_test_key")
    trustops_client.reset_breaker()
    trustops_client._status_cache.clear()
    calls = []

    def down(req, timeout=0):
        calls.append(req.full_url)
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(trustops_client.urllib.request, "urlopen", down)
    t = trustops_client.decide("agent", "agent:query", "query.answer")
    assert t["decision"] == "DENY" and t["policy_id"] == "TRUSTOPS_UNAVAILABLE"            # fail closed
    assert trustops_client.breaker_open() and len(calls) == 1
    t0 = time.monotonic()
    for i in range(20):                                                                       # no further attempts
        assert trustops_client.decide("agent", "agent:query", "query.answer")["decision"] == "DENY"
        assert trustops_client.status(f"dec_{i}") is None
    assert trustops_client.reviewers(max_age=0) is None and trustops_client.app_access("x@y.test", max_age=0) == trustops_client.UNREACHABLE
    assert len(calls) == 1 and time.monotonic() - t0 < 1.0
    # a failed status lookup is remembered briefly even after the breaker closes
    trustops_client.reset_breaker()
    assert trustops_client.status("dec_0") is None and len(calls) == 1
    # once TrustOps answers again, calls go through and the breaker stays closed
    monkeypatch.setattr(trustops_client.urllib.request, "urlopen", lambda req, timeout=0: _Resp(b'{"final": "PENDING"}'))
    assert trustops_client.status("dec_new") == {"final": "PENDING"} and not trustops_client.breaker_open()
    trustops_client.reset_breaker()


class _Resp:
    def __init__(self, b):
        self.b = b

    def read(self):
        return self.b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_sync_stops_when_trustops_is_unreachable(tops, cpa, monkeypatch):
    run = pack_run(cpa)
    for e in run["result"]["exceptions"]:
        assert cpa.post(f"{WS}/runs/{run['id']}/exceptions/{e['id']}/send-to-trustops", headers=H,
                        json={"to": "approver@firm.test"}).status_code == 200
    asked = []

    def slow_down(did, max_age=8.0):
        asked.append(did)
        trustops_client._breaker["open_until"] = time.monotonic() + 15     # the first lookup finds TrustOps down
        return None
    monkeypatch.setattr(trustops_client, "status", slow_down)
    t0 = time.monotonic()
    assert cpa.get(f"{WS}/runs/{run['id']}", headers=H).status_code == 200
    assert len(asked) == 1 and time.monotonic() - t0 < 2                  # one lookup, not one per item
    trustops_client.reset_breaker()


# ---- 12. FinTwin says why TrustOps stopped it -------------------------------------------------------------------
@pytest.mark.parametrize("policy_id, words", [("AUTH-SUSPENDED", "paused"), ("AUTH-SCOPE", "not authorised"),
                                               ("TRUSTOPS_UNAVAILABLE", "can't be reached"), ("FIN-99", "did not allow")])
def test_fintwin_explains_a_trustops_refusal(tops, cpa, monkeypatch, policy_id, words):
    pack_run(cpa)
    real = tops.decide
    monkeypatch.setattr(trustops_client, "decide", lambda *a, **k: {"decision": "DENY", "policy_id": policy_id}
                        if a[2] == "query.answer" else real(*a, **k))
    r = cpa.post(WS + "/ask", headers=H, json={"question": "Which items are unmatched?"}).json()
    assert r["status"] == "REFUSED" and words in r["answer"] and "read-only" not in r["answer"]
    assert r["trustops"]["policy_id"] == policy_id


def test_fintwin_read_only_refusal_is_unchanged_for_write_requests(cpa):
    pack_run(cpa)
    r = cpa.post(WS + "/ask", headers=H, json={"question": "approve EX-001 please"}).json()
    assert r["status"] == "REFUSED" and "read-only" in r["answer"]


# ---- 1. FinTwin scrolls ---------------------------------------------------------------------------------------
def test_fintwin_conversation_scrolls():
    tail = CSS.split("2026-11-16 action items: FinTwin scrolling", 1)[1]
    assert "#ask-drawer .drawer-body{flex:1 1 auto;min-height:0;overflow-y:auto" in tail
    assert "#ask-drawer .drawer-head,#ask-drawer .drawer-foot{flex:none}" in tail
    assert "function askScroll()" in JS and "requestAnimationFrame" in JS.split("function askScroll()", 1)[1][:400]
    assert JS.split("function renderAskLog()", 1)[1].split(chr(10) + "}" + chr(10), 1)[0].rstrip().endswith("askScroll();")


# ---- 9. rejected rows ---------------------------------------------------------------------------------------------
def test_rejected_rows_are_visible_downloadable_and_acknowledged(cpa):
    cpa.post(WS + "/reset", headers=H)
    bad = (PACK / "01_bank_statement_October.csv").read_text().replace("HB006,2026-10-09", "HB006,2026-13-09").encode()
    h = upload_confirm(cpa, "01_bank_statement_October.csv", bad)
    assert h["health"]["import_status"] == "IMPORTED_WITH_REJECTIONS"
    assert h["health"]["status_text"] == f"Imported with 1 of {h['health']['received']} rows rejected"
    upload_confirm(cpa, "02_general_ledger_October.xlsx", (PACK / "02_general_ledger_October.xlsx").read_bytes())
    r = cpa.post(WS + "/reconciliation-runs", headers=H)
    assert r.status_code == 422 and "1 of" in r.text and "rows rejected" in r.text
    csv_ = cpa.get(f"{WS}/uploads/{h['id']}/rejected.csv", headers=H)
    assert csv_.status_code == 200 and "attachment" in csv_.headers["content-disposition"]
    text = csv_.content.decode("utf-8-sig")
    assert text.startswith("Report,Rejected rows") and "source_row,reason" in text and "HB006" in text and "Next step" in text
    src = next(k for k in cpa.get(WS + "/sources", headers=H).json()["kinds"] if k["kind"] == "bank")
    assert src["rejected_rows"] == 1 and src["upload_id"] == h["id"] and src["rejections_acknowledged"] is None
    a = cpa.post(f"{WS}/uploads/{h['id']}/acknowledge-rejections", headers=H, json={"note": "will fix next month"})
    assert a.status_code == 200 and a.json()["rejections_acknowledged"]["count"] == 1
    assert any(e["action"] == "upload.rejections_acknowledged" for e in appmod.svc.ws["client-a"].log.events)
    run = cpa.post(WS + "/reconciliation-runs", headers=H)
    assert run.status_code == 200 and any(e["type"] == "REJECTED_SOURCE_ROW" for e in run.json()["result"]["exceptions"])
    # a clean file uploaded again replaces it and needs no acknowledgement
    assert cpa.get(f"{WS}/uploads/UP-nope/rejected.csv", headers=H).status_code == 404
    assert "Imported with" in JS and "rejected.csv" in JS and "acknowledge-rejections" in JS


def test_reuploading_an_earlier_good_file_replaces_a_bad_one(cpa):
    """Good file, then a bad version by mistake, then the good file again: the good file must become the one in use
    (it was wrongly treated as a duplicate and ignored, leaving the run blocked)."""
    cpa.post(WS + "/reset", headers=H)
    good = (PACK / "01_bank_statement_October.csv").read_bytes()
    bad = good.decode().replace("HB006,2026-10-09", "HB006,2026-13-09").encode()
    first = upload_confirm(cpa, "01_bank_statement_October.csv", good)
    upload_confirm(cpa, "01_bank_statement_October.csv", bad)
    again = upload_confirm(cpa, "01_bank_statement_October.csv", good)
    assert again["state"] == "VALIDATED" and again["id"] != first["id"]
    src = next(k for k in cpa.get(WS + "/sources", headers=H).json()["kinds"] if k["kind"] == "bank")
    assert src["upload_id"] == again["id"] and not src["rejected_rows"]
    upload_confirm(cpa, "02_general_ledger_October.xlsx", (PACK / "02_general_ledger_October.xlsx").read_bytes())
    assert cpa.post(WS + "/reconciliation-runs", headers=H).status_code == 200
    # the same file twice in a row is still a duplicate (no rows added twice)
    dup = upload_confirm(cpa, "01_bank_statement_October.csv", good)
    assert dup["state"] == "DUPLICATE_BATCH"


# ---- 13. PDF confirm -----------------------------------------------------------------------------------------------
def test_pdf_confirm_refuses_bad_input_and_accepts_extracted_objects(cpa):
    cpa.post(WS + "/reset", headers=H)
    blob = (PACK / "07_receipt_OD-7741.pdf").read_bytes()
    up = cpa.post(WS + "/uploads", headers=H, data={"kind": "auto"}, files={"file": ("07_receipt_OD-7741.pdf", blob)}).json()
    url = f"{WS}/documents/{up['id']}/confirm"
    for bad in ({"fields": {"reference": ["OD-7741"]}}, {"fields": {"reference": "OD-7741", "amount": "two hundred"}},
                {"fields": "OD-7741"}, {"fields": {"reference": {"nested": True}}}):
        r = cpa.post(url, headers=H, json=bad)
        assert r.status_code == 422, (bad, r.status_code, r.text)
    r = cpa.post(url, headers=H, json={"fields": up["fields"]})          # the extracted objects, sent straight back
    assert r.status_code == 200 and r.json()["confirmed_fields"]["reference"] == "OD-7741"
