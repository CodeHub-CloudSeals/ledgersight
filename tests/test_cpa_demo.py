"""Avocet CPA demo changes (engineering delivery note, 7 Nov 2026): C02-C09.

State-tax readiness (CA-TAX-01), UK supplier FX (UK-FX-01), six visible run stages with linkage, source
envelope, TrustOps proof cards, bounded client/date queries, cross-client refusal and the CPA review pack.
Every result is computed from the uploaded synthetic fixtures; nothing is hard-coded.
"""
import io
import json
from datetime import date

import pytest

from test_ledgersight import EXP, H, appmod, golden_run, make_user  # noqa: F401
from ledgersight.core import recon

WS = "/v1/workspaces/client-a"
CASES = EXP["client-a"]["review_cases"]


@pytest.fixture(scope="module")
def cpa():
    return make_user("cpa@demo.test", "owner_reviewer", ["client-a", "client-b"])


@pytest.fixture(scope="module")
def staff():
    return make_user("staff@demo.test", "preparer", ["client-a"])


def by_case(run):
    return {e["case_id"]: e for e in run["result"]["exceptions"]}


def ask(c, q):
    r = c.post(WS + "/ask", headers=H, json={"question": q})
    assert r.status_code == 200, r.text
    return r.json()


# ---- C03 state-tax review ------------------------------------------------------------------
def test_ca_tax_case_flags_cpa_review_without_asserting_tax(cpa):
    run = golden_run(cpa)
    e = by_case(run)["CA-TAX-01"]
    want = CASES["CA-TAX-01"]
    assert (e["type"], e["client_status"], e["amount_cents"]) == (want["type"], want["client_status"], want["amount_cents"])
    assert e["missing_inputs"] == want["missing_inputs"]
    assert e["facts"]["ship_from_state"] == "FL" and e["facts"]["ship_to_state"] == "CA" and e["facts"]["invoice"] == want["invoice"]
    t = e["trustops"]
    assert (t["decision"], t["policy_id"], t["required_role_label"]) == ("ESCALATE", want["policy_id"], "CPA reviewer")
    assert "no tax liability" in e["why"] and "No tax is computed, filed or paid" in e["suggestion"]
    text = json.dumps(e).lower()
    assert "tax owed" not in text and "tax_amount" not in text and "rate" not in e["facts"]
    # the sale itself still reconciles: the tax flag sits on a matched line
    assert {m["bank_id"]: m["ledger_id"] for m in run["result"]["matches"]}["HB013"] == "GL-H-110"


def _inv(**kw):
    base = {"id": "INV-9", "ref_norm": "INV9", "amount_cents": 1000, "open_cents": 1000, "currency": "USD",
            "customer_alias": "C", "issue_date": "2026-10-12", "ship_from_state": "FL", "ship_to_state": "CA",
            "taxability": "", "nexus_status": "", "dest_registration": ""}
    return {**base, **kw}


def test_tax_review_only_when_interstate_and_facts_missing():
    flag = lambda inv: [e for e in recon.reconcile("w", [], [], [inv]).exceptions if e.type == "TAX_REVIEW"]
    assert flag(_inv()) and flag(_inv())[0].case_id == "CA-TAX-01"
    assert not flag(_inv(taxability="taxable", nexus_status="established", dest_registration="registered"))
    assert not flag(_inv(ship_to_state="FL"))          # same state: nothing to review
    assert not flag(_inv(ship_from_state="", ship_to_state=""))  # older AR files without state columns


# ---- C04 international mismatch --------------------------------------------------------------
def test_uk_fx_case_abstains_without_approved_rate(cpa):
    run = golden_run(cpa)
    e = by_case(run)["UK-FX-01"]
    want = CASES["UK-FX-01"]
    assert (e["type"], e["client_status"], e["reason_code"]) == (want["type"], want["client_status"], want["reason_code"])
    assert want["bill"] in e["evidence_refs"] and e["bank_id"] == "HB014"
    assert e["facts"]["bill_currency"] == "GBP" and e["facts"]["paid_currency"] == "USD"
    assert "HB014" not in {m["bank_id"] for m in run["result"]["matches"]}  # no silent auto-match
    assert not any("rate" in k for k in e["facts"]) and "invents no rate" in e["suggestion"]
    assert e["trustops"]["policy_id"] == want["policy_id"] and e["trustops"]["decision"] == "ESCALATE"
    assert run["result"]["unexplained_variance_cents"] == 0


def test_fx_with_approved_source_still_not_converted(monkeypatch):
    monkeypatch.setattr(recon, "APPROVED_FX", {("GBP", "USD")})
    b = [{"id": "B1", "date": date(2026, 10, 26), "description": "WIRE", "reference": "UKS-1", "ref_norm": "UKS1",
          "amount_cents": -1000, "currency": "USD"}]
    bills = [{"id": "UKS-1", "ref_norm": "UKS1", "open_cents": 800, "currency": "GBP", "vendor_country": "UK"}]
    res = recon.reconcile("w", b, [], bills=bills)
    assert [(e.type, e.reason_code) for e in res.exceptions] == [("FX_REVIEW", "FX_CONVERSION_NOT_AUTOMATED")]
    assert res.matches == []


# ---- C05 six stages + run linkage ------------------------------------------------------------
def test_run_shows_six_stages_with_linkage(cpa):
    run = golden_run(cpa)
    st = run["stages"]
    assert [s["stage"] for s in st] == ["Intake", "Validate", "Map", "Reconcile", "Exception", "Complete"]
    assert all(s["status"] == "DONE" and s["trace_id"] == run["trace_id"] and s["step_ids"] for s in st)
    assert "confirmed by a person" in st[2]["summary"]
    assert {"CA-TAX-01", "UK-FX-01", "HB005", "HB006"} <= set(st[4]["case_ids"])
    assert "not complete" in st[5]["summary"]  # open cases are never shown as completed
    assert [s["stage"] for s in run["workflow"]][:3] == ["Intake", "Validate · Map", "Reconcile"]
    for e in run["result"]["exceptions"]:
        assert e["run_id"] == run["id"] and e["trace_id"] == run["trace_id"] and e["agent_id"] == "AG-03"
        assert e["rulebook_version"] == run["rulebook_version"] and e["case_id"] and e["source_record_refs"]
    assert len(run["match_linkage"]) == len(run["result"]["matches"])
    assert all(m["source_record_refs"] and m["client_status"] == "MATCHED" for m in run["match_linkage"])


def test_complete_stage_updates_once_every_case_is_decided(cpa):
    run = golden_run(cpa)
    for e in run["result"]["exceptions"]:
        r = cpa.post(f"{WS}/runs/{run['id']}/exceptions/{e['id']}/decisions", headers=H, json={"decision": "ACCEPT", "note": "ok"})
        assert r.status_code == 200, r.text
    st = cpa.get(f"{WS}/runs/{run['id']}").json()["stages"]
    assert st[5]["summary"] == "Reports ready; every case has a CPA decision"


# ---- C02 source envelope -----------------------------------------------------------------------
def test_sources_show_envelope_in_client_context(cpa):
    golden_run(cpa)
    k = {x["kind"]: x for x in cpa.get(WS + "/sources").json()["kinds"]}
    env = k["bank"]["envelope"]
    assert env["client_id"] == "client-a" and env["tenant_id"] and env["access"] == "READ_ONLY"
    assert env["source_file"] == "client-a_bank.csv" and env["row_count"] == EXP["client-a"]["bank_rows"]
    assert len(env["source_digest"]) == 64 and env["upload_time"] and env["currency"] == ["USD"]
    assert env["period"] and env["date_range"] == ["2026-10-01", "2026-10-31"] and env["freshness_seconds"] >= 0
    assert k["ap"]["envelope"]["currency"] == ["GBP", "USD"]
    assert "envelope" not in k["documents"] or k["documents"]["envelope"]["client_id"] == "client-a"


# ---- C07 TrustOps proof ---------------------------------------------------------------------------
def test_trustops_proof_shows_all_four_decisions_from_evidence(cpa, staff):
    run = golden_run(cpa)
    p = cpa.get(WS + "/trustops/proof").json()
    cards = {c["key"]: c for c in p["cards"]}
    assert cards["allow_source_read"]["shown"] and cards["allow_source_read"]["actor_id"] == "agent:intake"
    esc = cards["escalate_review"]
    assert esc["shown"] and esc["policy_decision"].startswith("ESCALATE") and esc["required_role_label"] == "CPA reviewer"
    # preparer approval attempt: real request, real denial
    e = by_case(run)["HB006"]
    r = staff.post(f"{WS}/runs/{run['id']}/exceptions/{e['id']}/decisions", headers=H, json={"decision": "ACCEPT", "note": ""})
    assert r.status_code == 403 and r.json()["detail"]["decision"] == "DENY"
    # cross-client request from a user who only has this client
    a = ask(staff, "Show Client 2 bank transactions")
    assert a["status"] == "REFUSED" and a["trustops"]["reason_code"] == "WORKSPACE_SCOPE"
    p = cpa.get(WS + "/trustops/proof").json()
    cards = {c["key"]: c for c in p["cards"]}
    assert p["all_shown"] and p["chain"]["state"] == "PASS"
    d = cards["deny_preparer_approval"]
    assert d["actor_id"] == "staff@demo.test" and d["actor_role_label"] == "Staff preparer" and d["policy_id"] == "POL-02"
    x = cards["deny_cross_client"]
    assert x["policy_id"] == "POL-01" and x["reason_code"] == "WORKSPACE_SCOPE" and x["delegated_by"] == "staff@demo.test"
    for c in cards.values():
        assert c["policy_version"] and c["time_utc"] and c["event_hash"] and c["signature"] == "PASS" and c["result"]
    assert "client-b" not in json.dumps(p)


def test_trustops_proof_is_client_scoped(staff):
    assert staff.get("/v1/workspaces/client-b/trustops/proof").status_code == 404


# ---- C09 bounded queries ----------------------------------------------------------------------------
def test_scoped_october_query_returns_cases_with_citations(cpa):
    run = golden_run(cpa)
    a = ask(cpa, "Show Client 1 exceptions from 1–31 October 2026")
    assert a["status"] == "ANSWERED" and "between 2026-10-01 and 2026-10-31" in a["answer"]
    assert len(a["rows"]) == len(run["result"]["exceptions"]) and a["citations"]
    assert {r[-1] for r in a["rows"]} >= {"CA-TAX-01", "UK-FX-01"}
    # audit FUNC-11: different kinds of exception are totalled per type, never in one mixed sum
    total = sum(e["amount_cents"] for e in run["result"]["exceptions"])
    assert "By type:" in a["answer"] and f"totalling ${abs(total) / 100:,.2f}" not in a["answer"]
    assert "tax review" in a["answer"].lower()
    narrow = ask(cpa, "Show exceptions from 1-10 October 2026")
    assert {r[1] for r in narrow["rows"]} == {"HB004", "HB005", "HB006"}


def test_who_reviewed_hb006_awaiting_then_named(cpa):
    run = golden_run(cpa)
    a = ask(cpa, "Who reviewed HB006 and why?")
    assert "awaiting approval" in a["answer"] and "R4_MISSING_GL" in a["answer"] and "POL-05" in a["answer"]
    e = by_case(run)["HB006"]
    cpa.post(f"{WS}/runs/{run['id']}/exceptions/{e['id']}/decisions", headers=H, json={"decision": "ACCEPT", "note": "bill on file"})
    a = ask(cpa, "Who reviewed HB006 and why?")
    assert "accepted by cpa@demo.test" in a["answer"] and "bill on file" in a["answer"] and "UTC" in a["answer"]
    assert a["rows"][0][5] == "R4_MISSING_GL" and a["rows"][0][7]


def test_case_ids_are_queryable(cpa):
    golden_run(cpa)
    a = ask(cpa, "Why was CA-TAX-01 flagged?")
    assert a["status"] == "ANSWERED" and "POL-11" in a["answer"] and "CA" in a["answer"]
    assert "APPROVED_FX_RATE_MISSING" not in a["answer"]
    assert "exchange-rate" in ask(cpa, "Why was UK-FX-01 flagged?")["answer"]


@pytest.mark.parametrize("q", ["Show Client 2 bank transactions", "show client-b deposits", "list CLIENT 2 exceptions"])
def test_other_client_by_name_is_refused_without_leaking(staff, cpa, q):
    golden_run(cpa)
    a = ask(staff, q)
    assert a["status"] == "REFUSED" and a["trustops"]["reason_code"] == "WORKSPACE_SCOPE" and a["rows"] == []
    assert a["answer"] == ask(staff, "Show Acme Corp's deposits")["answer"]  # same refusal as a made-up name
    assert "client-b" not in json.dumps(a).lower()


def test_own_client_name_is_not_refused(cpa):
    golden_run(cpa)
    assert ask(cpa, "Show Client 1 open exceptions")["status"] == "ANSWERED"


def test_suggestions_offer_the_bounded_queries(cpa):
    golden_run(cpa)
    s = cpa.get(WS + "/ask/suggestions").json()["suggestions"]
    assert s[0] == "Show Client 1 exceptions from 1–31 October 2026"
    assert "Who reviewed HB006 and why?" in s and "Why was CA-TAX-01 flagged?" in s


# ---- C08 review pack ---------------------------------------------------------------------------------
def test_finance_pack_has_tax_fx_totals_and_next_steps(cpa):
    run = golden_run(cpa)
    fin = cpa.get(f"{WS}/runs/{run['id']}/reports/finance.json").json()
    s = dict(fin["summary"])
    assert s["Tax review items"] == 1 and s["FX review items"] == 1 and s["Partial receipts amount (USD)"] == "900.00"
    assert s["Possible duplicate amount (USD)"] == "89.99" and s["Generated at (UTC)"]
    sec = {x["title"]: x for x in fin["sections"]}
    assert {r[0] for r in sec["Tax and FX review items"]["rows"]} == {"CA-TAX-01", "UK-FX-01"}
    assert {r[0] for r in sec["Totals by status"]["rows"]} >= {"MATCHED", "PARTIAL", "UNMATCHED", "DUPLICATE", "TAX_REVIEW", "FX_REVIEW"}
    assert len(sec["CPA next steps"]["rows"]) == len(run["result"]["exceptions"])
    assert {r[0] for r in sec["Source totals"]["rows"]} >= {"bank", "ledger", "ar", "ap"}
    pdf = cpa.get(f"{WS}/runs/{run['id']}/reports/finance.pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    import pdfplumber
    with pdfplumber.open(io.BytesIO(pdf.content)) as d:
        txt = "\n".join(p.extract_text() or "" for p in d.pages)
    assert "CA-TAX-01" in txt and "UK-FX-01" in txt and "CPA next steps" in txt
    csv_ = cpa.get(f"{WS}/runs/{run['id']}/reports/finance.csv").content.decode("utf-8-sig")
    assert "CA-TAX-01" in csv_ and "tax owed" not in csv_.lower()


def test_audit_trail_exports_as_pdf_with_every_signed_event(cpa):
    golden_run(cpa)
    n = len(appmod.svc.ws["client-a"].log.events)
    js = cpa.get(WS + "/audit-trail.json").json()
    s = dict(js["summary"])
    assert js["title"] == "Audit Trail Report" and s["Events recorded"] >= n and s["Evidence verification"].startswith("PASS")
    rows = {x["title"]: x for x in js["sections"]}["All signed events"]["rows"]
    assert len(rows) == s["Events recorded"] and all(len(r[-1]) == 64 for r in rows)  # every row carries its event hash
    pdf = cpa.get(WS + "/audit-trail.pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    assert "attachment" in pdf.headers["content-disposition"] and ".pdf" in pdf.headers["content-disposition"]
    import pdfplumber
    with pdfplumber.open(io.BytesIO(pdf.content)) as d:
        txt = "\n".join(p.extract_text() or "" for p in d.pages)
    assert "Audit Trail Report" in txt and "Evidence chain" in txt and "PASS" in txt and "exception.route" in txt
    assert cpa.get(WS + "/audit-trail.csv").status_code == 200 and cpa.get(WS + "/audit-trail.xlsx").status_code == 200
    assert cpa.get(WS + "/audit-trail.exe").status_code == 422
    ev = appmod.svc.ws["client-a"].log.events[-1]
    assert ev["action"] == "export.read" and "audit.xlsx" in ev["source_line_refs"]  # the export itself is recorded


def test_audit_trail_export_is_client_scoped(staff):
    assert staff.get("/v1/workspaces/client-b/audit-trail.pdf").status_code == 404


def test_recon_page_shows_agent_workflow_before_run_stages():
    html = (appmod.BASE / "static" / "index.html").read_text(encoding="utf-8")
    recon = html.split('id="p-recon"', 1)[1].split("</section>", 1)[0]
    assert recon.index('id="rc-pipe"') < recon.index('id="stages-card"')


def test_ask_launcher_is_a_floating_button():
    html = (appmod.BASE / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="ask-open" class="ask-fab"' in html and 'class="askbtn"' not in html
    css = (appmod.BASE / "static" / "avocet.css").read_text(encoding="utf-8")
    assert ".ask-fab{position:fixed;right:24px;bottom:24px" in css


def test_rulebook_lists_new_review_policies(cpa):
    pol = {p["id"]: p for p in cpa.get("/v1/rulebooks").json()["policies"]}
    assert pol["POL-11"]["effect"] == "ESCALATE" and "TAX_REVIEW" in pol["POL-11"]["exception_types"]
    assert pol["POL-12"]["effect"] == "ESCALATE" and "FX_REVIEW" in pol["POL-12"]["exception_types"]
