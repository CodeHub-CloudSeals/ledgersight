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
    assert "doesn't look like a bank statement" in d or "JSON must be a list" in d
    if path.suffix == ".csv":  # plain words: what was found, what is needed, and where to look
        assert "Columns found: employee, role" in d and "also needs:" in d and "template" in d
        assert "_" not in d.split("also needs:")[1].split(".")[0]  # field names in plain words, not line_id


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
    body = js.split("async function refreshWs()", 1)[1].split("\n}\n", 1)[0]
    assert "await progress()" in body
    assert 'if (t.dataset.tab === "recon") progress();' in js


def test_ui_handles_cleared_data_and_offers_the_run():
    """After a clear (here or by a colleague) no stale pending upload stays on screen; clear buttons are off when
    there is nothing to clear; Run re-checks the client; after importing, the user is taken to Run reconciliation."""
    js = (ROOT / "ledgersight" / "static" / "avocet.js").read_text(encoding="utf-8")
    html = (ROOT / "ledgersight" / "static" / "index.html").read_text(encoding="utf-8")
    body = js.split("async function refreshWs()", 1)[1].split("\n}\n", 1)[0]
    assert "staleUpload()" in body and "w.uploads.some((u) => u.id === S.upload.id)" in body
    assert "resetIntakeUi(); await switchWs();" in js  # clear all
    assert '$("clear-all").disabled = !hasData' in js and '$("reset-btn").disabled = S.me.role !== "owner_reviewer" || !hasData' in js
    handler = js.split('$("run-btn").onclick', 1)[1][:500]
    assert "await progress();" in handler and "if (readiness())" in handler and "if (alreadyReconciled())" in handler
    assert 'id="up-next"' in html and "Go to Reconciliation →" in js and "offerRun();" in js
    assert 'window.addEventListener("focus", syncIfStale)' in js


def test_sources_prompt_only_navigates_and_agents_page_cannot_run():
    """Action items 6 and 8: Sources links to Reconciliation (no auto-run); only the Reconciliation page runs."""
    js = (ROOT / "ledgersight" / "static" / "avocet.js").read_text(encoding="utf-8")
    html = (ROOT / "ledgersight" / "static" / "index.html").read_text(encoding="utf-8")
    offer = js.split("async function offerRun()", 1)[1].split("\n}\n", 1)[0]
    assert 'go("recon")' in offer and "click()" not in offer
    assert 'id="ag-run"' not in html and "Run reconciliation now" not in html and '$("ag-run")' not in js
    assert html.count('id="run-btn"') == 1


def test_same_files_are_not_run_twice(cpa):
    """Action item 7: unchanged files return the same run, flagged as reused; status says it's up to date."""
    cpa.post(WS + "/reset", headers=H)
    for f in sorted(PACK.glob("0[12]_*")):
        u = up(cpa, f).json()
        cpa.post(f"{WS}/uploads/{u['id']}/mapping", headers=H, json={"mapping": u["proposed_mapping"]})
    st = next(w for w in cpa.get("/v1/workspaces").json() if w["id"] == "client-a")["status"]
    assert st["up_to_date"] is False and st["current_run"] is None
    first = cpa.post(WS + "/reconciliation-runs", headers=H).json()
    assert not first.get("reused")
    again = cpa.post(WS + "/reconciliation-runs", headers=H).json()
    assert again["id"] == first["id"] and again["reused"] is True
    st = next(w for w in cpa.get("/v1/workspaces").json() if w["id"] == "client-a")["status"]
    assert st["up_to_date"] is True and st["current_run"] == first["id"] and st["runs"] == 1
    u = up(cpa, PACK / "03_open_invoices.csv").json()  # new file -> the run is no longer up to date
    cpa.post(f"{WS}/uploads/{u['id']}/mapping", headers=H, json={"mapping": u["proposed_mapping"]})
    st = next(w for w in cpa.get("/v1/workspaces").json() if w["id"] == "client-a")["status"]
    assert st["up_to_date"] is False
    third = cpa.post(WS + "/reconciliation-runs", headers=H).json()
    assert third["id"] != first["id"] and not third.get("reused")


@pytest.mark.parametrize("kind", ["bank", "ledger", "ar", "ap", "documents", "parties"])
def test_templates_download_and_are_recognised_as_their_type(cpa, kind):
    """Action item 5: a downloadable template per file type; uploading the template itself is recognised."""
    r = cpa.get(f"/v1/templates/{kind}.csv")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"] and "text/csv" in r.headers["content-type"]
    u = cpa.post(WS + "/uploads", headers=H, data={"kind": "auto"}, files={"file": (f"template_{kind}.csv", r.content)})
    assert u.status_code == 200, u.text
    assert u.json()["kind"] == kind


def test_templates_need_sign_in_and_unknown_kind_404(cpa):
    from fastapi.testclient import TestClient
    assert TestClient(appmod.app).get("/v1/templates/bank.csv").status_code == 401
    assert cpa.get("/v1/templates/payroll.csv").status_code == 404


def test_review_card_buttons_and_auto_hiding_success_messages():
    """Review card: no suggested comments / suggested-next-step box; four actions for everyone
    (Return to preparer, Reject, Propose resolution for reviewer, Approve). Success messages auto-hide."""
    js = (ROOT / "ledgersight" / "static" / "avocet.js").read_text(encoding="utf-8")
    assert "suggestChips" not in js and "SUGGESTED_NOTES" not in js and "Suggested next step" not in js
    row = js.split('el("div", { class: "xa-row" },', 1)[1][:900]
    labels = ["Return to preparer", "Reject", "Propose resolution for reviewer", "Approve"]
    assert all(l in row for l in labels) and [row.index(l) for l in labels] == sorted(row.index(l) for l in labels)
    assert "!reviewer ? el(\"button\"" not in row  # Propose is shown to CPA reviewers too
    m = js.split("const msg = (id, text, kind) =>", 1)[1][:400]
    assert 'kind === "good"' in m and "setTimeout" in m and "7000" in m


def test_templates_only_in_the_error_message_and_no_sample_connector(cpa):
    """Sources has no template row; a wrong file gets an error message that links to the templates."""
    js = (ROOT / "ledgersight" / "static" / "avocet.js").read_text(encoding="utf-8")
    html = (ROOT / "ledgersight" / "static" / "index.html").read_text(encoding="utf-8")
    assert "tpl-row" not in html and "tpl-chip" not in js and "Not sure of the format" not in js
    refusal = js.split("function refused(f, reason)", 1)[1].split("\n}\n", 1)[0]
    assert "We couldn't use this file" in refusal and "templateLinks()" in refusal
    conns = {c["id"] for c in cpa.get(WS + "/sources").json()["connectors"]}
    assert "sample" not in conns and {"file-upload", "google-drive"} <= conns


def test_sources_upload_card_has_no_empty_block_under_the_connector():
    """The drop area grows to fill the upload card, so it matches the Source status card without a gap."""
    css = (ROOT / "ledgersight" / "static" / "avocet.css").read_text(encoding="utf-8")
    assert "#p-import .grid.two>.card:first-child{display:flex;flex-direction:column}" in css
    assert "#p-import #dz-target{flex:1 1 auto;" in css


def test_login_page_uses_the_shield_design():
    """10/11 item 1: dark shield illustration, 'Financial intelligence. Built on trust.', three points, Sign in card."""
    html = (ROOT / "ledgersight" / "static" / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "ledgersight" / "static" / "avocet.css").read_text(encoding="utf-8")
    login = html.split('id="login"', 1)[1].split("</section>", 1)[0]
    assert "Financial intelligence." in login and "Built on trust." in login
    assert "Every transaction matched. Every decision governed. Every action evidenced." in login
    assert login.count('<li class="m-in">') == 3 and "<h1 class=\"m-in\">Sign in</h1>" in login
    assert "LedgerSight v1.0.0 · Governed by TrustOps · Read-only" in login
    assert (ROOT / "ledgersight" / "static" / "login-shield.jpg").stat().st_size > 50_000
    assert 'url("/static/login-shield.jpg")' in css


def test_cpa_reviewer_can_propose_but_not_approve_own_proposal():
    """With every Google user a CPA reviewer: reviewer A proposes -> A approving it is DENY (maker-checker);
    reviewer B approves -> ALLOW."""
    a = make_user("reviewer-a@demo.test", "owner_reviewer", ["client-a"])
    b = make_user("reviewer-b@demo.test", "owner_reviewer", ["client-a"])
    a.post(WS + "/reset", headers=H)
    for f in sorted(PACK.glob("0[12]_*")):
        u = up(a, f).json()
        a.post(f"{WS}/uploads/{u['id']}/mapping", headers=H, json={"mapping": u["proposed_mapping"]})
    run = a.post(WS + "/reconciliation-runs", headers=H).json()
    e = next(x for x in run["result"]["exceptions"] if x["bank_id"] == "HB006")
    base = f"{WS}/runs/{run['id']}/exceptions/{e['id']}"
    p = a.post(base + "/proposals", headers=H, json={"note": "vendor bill on file"})
    assert p.status_code == 200 and p.json()["proposed_by"] == "reviewer-a@demo.test"
    own = a.post(base + "/decisions", headers=H, json={"decision": "ACCEPT", "note": ""})
    assert own.status_code == 403 and own.json()["detail"]["reason_code"] == "MAKER_CHECKER_SELF_APPROVAL"
    ok = b.post(base + "/decisions", headers=H, json={"decision": "ACCEPT", "note": "agreed"})
    assert ok.status_code == 200 and ok.json()["status"] == "ACCEPTED" and ok.json()["reviewer"] == "reviewer-b@demo.test"


def test_intake_is_upload_only_device_or_google():
    """Files come from this device (or a saved email) or Google Drive/Sheets/Docs/Slides. No built-in loader."""
    html = (ROOT / "ledgersight" / "static" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "ledgersight" / "static" / "avocet.js").read_text(encoding="utf-8")
    assert "sample-btn" not in html and "Load All Files" not in html and "/samples/load" not in js
    assert 'id="choose-btn"' in html and 'id="dz-drive-btn"' in html
    for kind in ("spreadsheet", "document", "presentation"):  # Sheets -> xlsx, Docs/Slides -> PDF, then content detection
        assert f"application/vnd.google-apps.{kind}" in js
    assert '"AVOCET_SHOW_SAMPLES": "0"' in (ROOT / ".vscode" / "launch.json").read_text(encoding="utf-8")
    assert "AVOCET_SHOW_SAMPLES=0" in (ROOT / "deploy" / "cloudrun.sh").read_text(encoding="utf-8")
    assert json.loads((ROOT / ".vscode" / "launch.json").read_text(encoding="utf-8"))  # still valid JSON


def test_test_fixture_api_matches_uploading_the_pack(cpa):
    """The /samples API (tests only) and a real upload of demo_files/0_client1_october give the same result."""
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
