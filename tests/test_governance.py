"""TrustOps governance, agent workflow, approvals, reports, Ask LedgerSight, sources and file types."""
import csv
import io
import json
import re

import pytest

from test_ledgersight import EXP, FIXTURES, H, appmod, golden_run, make_user  # noqa: F401  (shared helpers)

from ledgersight.config import load
from ledgersight.core import policy, recon

WS = "/v1/workspaces/client-a"


@pytest.fixture(scope="module")
def rev():
    return make_user("reviewer@gov.test", "owner_reviewer", ["client-a", "client-b"])


@pytest.fixture(scope="module")
def prep():
    return make_user("preparer@gov.test", "preparer", ["client-a"])


def exc_for(run, bank_id):
    return next(x for x in run["result"]["exceptions"] if x["bank_id"] == bank_id)


# ---- 1. policy engine: ALLOW / ESCALATE / DENY with policy ids ------------------------
def _req(**kw):
    return {"actor_id": "u", "role": "owner_reviewer", "workspace_id": "w", "data_class": "synthetic",
            "model_route": "deterministic", **kw}


def test_policy_ids_and_three_decisions():
    mem = {"u": {"w"}}
    d = policy.evaluate(_req(tool_action="ledger.post"), mem)
    assert (d.decision, d.policy_id, d.policy_name) == ("DENY", "POL-03", "READ_ONLY")
    assert policy.evaluate(_req(tool_action="workspace.read", workspace_id="x"), mem).policy_id == "POL-01"
    d = policy.evaluate(_req(tool_action="exception.route", role="agent:triage", actor_id="agent:triage",
                             exception_type="MISSING_GL"), {"agent:triage": {"w"}})
    assert (d.decision, d.policy_id, d.required_role) == ("ESCALATE", "POL-05", "owner_reviewer")
    dup = policy.evaluate(_req(tool_action="exception.route", role="agent:triage", actor_id="agent:triage",
                               exception_type="POSSIBLE_DUPLICATE"), {"agent:triage": {"w"}})
    assert dup.policy_id == "POL-04"
    other = policy.evaluate(_req(tool_action="exception.route", role="agent:triage", actor_id="agent:triage",
                                 exception_type="PARTIAL_RECEIPT"), {"agent:triage": {"w"}})
    assert other.policy_id == "POL-09" and other.decision == "ESCALATE"
    assert policy.evaluate(_req(tool_action="workspace.read"), mem).decision == "ALLOW"
    assert d.policy_version == load("policies.json")["version"]


def test_policies_and_thresholds_come_from_config():
    cfg = load("policies.json")
    assert set(policy.POLICIES) == {p["id"] for p in cfg["policies"]}
    assert policy.FORBIDDEN == set(cfg["forbidden_actions"])
    assert set(policy.AGENTS) == {"AG-01", "AG-02", "AG-03", "AG-04", "AG-05", "AG-06"}
    m = load("matching.json")
    assert recon.DATE_TOLERANCE_DAYS == m["date_tolerance_days"] and recon.ENGINE_VERSION == m["engine_version"]


# ---- 2. workflow: six agents, each gated by TrustOps ------------------------------------
def test_run_records_six_gated_agent_steps(rev):
    run = golden_run(rev)
    steps = run["workflow"]
    assert [s["agent"] for s in steps] == ["AG-01", "AG-02", "AG-03", "AG-04", "AG-05", "AG-06"]
    assert all(s["decision"] == "ALLOW" and s["policy_id"] == "POL-02" and s["status"] == "DONE" for s in steps)
    assert len({s["trace_id"] for s in steps}) == 1 and run["trace_id"] == steps[0]["trace_id"]
    assert all(s["step_id"].startswith(run["trace_id"]) and s["duration_ms"] >= 0 for s in steps)
    recon_step = steps[2]
    assert recon_step["rows_in"] == EXP["client-a"]["bank_rows"] + EXP["client-a"]["ledger_rows"]
    assert recon_step["rows_out"] == len(run["result"]["matches"]) + len(run["result"]["exceptions"])
    ev = [e for e in appmod.svc.ws["client-a"].log.events if e.get("run_id") == run["id"] and e["actor_type"] == "agent"]
    assert {e["agent_code"] for e in ev} >= {"AG-01", "AG-02", "AG-03", "AG-04", "AG-05", "AG-06"}
    assert all(e["delegated_by"] == "reviewer@gov.test" for e in ev)


def test_triage_escalates_each_exception_with_its_policy(rev):
    run = golden_run(rev)
    t = {x["bank_id"]: x["trustops"] for x in run["result"]["exceptions"]}
    assert t["HB006"]["decision"] == "ESCALATE" and t["HB006"]["policy_id"] == "POL-05"
    assert t["HB004"]["policy_id"] == "POL-04"
    assert t["HB005"]["policy_id"] == "POL-09" and t["HB005"]["required_role_label"] == "CPA reviewer"
    esc = [e for e in appmod.svc.ws["client-a"].log.events if e["action"] == "exception.route" and e["run_id"] == run["id"]]
    assert len(esc) == len(run["result"]["exceptions"]) and all(e["policy_decision"].startswith("ESCALATE") for e in esc)


def test_vendor_bill_supports_missing_gl_without_changing_results(rev):
    run = golden_run(rev)
    hb6 = exc_for(run, "HB006")
    assert hb6["type"] == "MISSING_GL" and "GP-5531" in hb6["evidence_refs"] and "posts nothing" in hb6["suggestion"]
    assert run["result"]["unexplained_variance_cents"] == 0


# ---- 3. agents cannot do what they are not granted ---------------------------------------
@pytest.mark.parametrize("agent,action,code,pid", [
    ("AG-03", "ledger.post", "ACTION_OUTSIDE_PRODUCT_BOUNDARY", "POL-03"),
    ("AG-03", "bank.payment", "ACTION_OUTSIDE_PRODUCT_BOUNDARY", "POL-03"),
    ("AG-06", "exception.decide", "AGENT_CANNOT_APPROVE", "POL-06"),
    ("AG-01", "run.compute", "ROLE_NOT_AUTHORISED", "POL-02"),
])
def test_agent_attempts_denied_and_logged_as_agent(rev, agent, action, code, pid):
    r = rev.post(WS + "/actions/attempt", headers=H, json={"action": action, "as_agent": agent})
    assert r.status_code == 403
    body = r.json()
    assert (body["decision"], body["reason_code"], body["policy_id"], body["executed"]) == ("DENY", code, pid, False)
    ev = appmod.svc.ws["client-a"].log.events[-1]
    assert ev["actor_type"] == "agent" and ev["agent_code"] == agent and ev["policy_decision"] == f"DENY:{code}"
    assert ev["policy_id"] == pid


def test_agent_cannot_act_outside_delegating_users_scope(prep):
    r = prep.post("/v1/workspaces/client-b/actions/attempt", headers=H, json={"action": "workspace.read", "as_agent": "AG-03"})
    assert r.status_code == 404


# ---- 4. approvals: maker-checker, RETURN, required role -----------------------------------
def test_preparer_self_approval_is_maker_checker_then_return_and_accept(rev, prep):
    run = golden_run(rev)
    exc = exc_for(run, "HB006")
    base = f"{WS}/runs/{run['id']}/exceptions/{exc['id']}"
    assert prep.post(base + "/proposals", headers=H, json={"note": "vendor bill on file"}).status_code == 200
    r = prep.post(base + "/decisions", headers=H, json={"decision": "ACCEPT"})
    assert r.status_code == 403 and r.json()["detail"]["reason_code"] == "MAKER_CHECKER_SELF_APPROVAL"
    assert r.json()["detail"]["policy_id"] == "POL-06"
    r = rev.post(base + "/decisions", headers=H, json={"decision": "RETURN", "note": "attach the bill"})
    assert r.status_code == 200 and r.json()["status"] == "RETURNED" and "Nothing posted" in r.json()["effect"]
    assert rev.get(WS).json()["status"]["open_exceptions"] == len(run["result"]["exceptions"])  # returned is still open
    assert prep.post(base + "/proposals", headers=H, json={"note": "bill attached"}).json()["status"] == "OPEN"
    r = rev.post(base + "/decisions", headers=H, json={"decision": "ACCEPT", "note": "ok"})
    assert r.status_code == 200 and r.json()["status"] == "ACCEPTED" and r.json()["approval_policy"] == "POL-06"
    assert [h["decision"] for h in r.json()["history"]] == ["RETURN", "ACCEPT"]
    ev = next(e for e in reversed(appmod.svc.ws["client-a"].log.events) if e["action"] == "exception.accept")
    assert ev["policy_decision"] == "ALLOW:MAKER_CHECKER_PASSED" and ev["policy_id"] == "POL-06"


def test_preparer_who_is_not_the_maker_gets_role_reason(rev, prep):
    run = golden_run(rev)
    base = f"{WS}/runs/{run['id']}/exceptions/{exc_for(run, 'HB004')['id']}"
    r = prep.post(base + "/decisions", headers=H, json={"decision": "ACCEPT"})
    assert r.status_code == 403 and r.json()["detail"]["reason_code"] == "ROLE_NOT_AUTHORISED"


def test_unknown_decision_rejected(rev):
    run = golden_run(rev)
    base = f"{WS}/runs/{run['id']}/exceptions/{exc_for(run, 'HB004')['id']}"
    assert rev.post(base + "/decisions", headers=H, json={"decision": "POST"}).status_code == 422


# ---- 5. two reports, same run id, four formats --------------------------------------------
@pytest.mark.parametrize("kind", ["finance", "governance"])
@pytest.mark.parametrize("fmt", ["json", "csv", "xlsx", "pdf"])
def test_reports_in_every_format(rev, kind, fmt):
    run = golden_run(rev)
    r = rev.get(f"{WS}/runs/{run['id']}/reports/{kind}.{fmt}")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith({"json": "application/json", "csv": "text/csv", "pdf": "application/pdf",
                                                 "xlsx": "application/vnd.openxmlformats"}[fmt])
    if fmt == "json":
        assert r.json()["run_id"] == run["id"]
    elif fmt == "csv":
        assert run["id"] in r.content.decode("utf-8-sig")
    elif fmt == "xlsx":
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(r.content))
        assert any(run["id"] == c.value for row in wb["Summary"].iter_rows() for c in row)
    else:
        import pdfplumber
        assert r.content.startswith(b"%PDF")
        with pdfplumber.open(io.BytesIO(r.content)) as pdf:
            assert run["id"] in "".join(p.extract_text() or "" for p in pdf.pages)


def test_finance_and_governance_reports_share_run_and_show_decisions(rev):
    run = golden_run(rev)
    rev.post(WS + "/actions/attempt", headers=H, json={"action": "ledger.post", "as_agent": "AG-03"})
    fin = rev.get(f"{WS}/runs/{run['id']}/reports/finance.json").json()
    gov = rev.get(f"{WS}/runs/{run['id']}/reports/governance.json").json()
    assert fin["run_id"] == gov["run_id"] == run["id"]
    s = dict(gov["summary"])
    assert s["ESCALATE decisions"] >= len(run["result"]["exceptions"]) and s["DENY decisions"] >= 1
    assert s["Evidence verification"].startswith("PASS")
    titles = {x["title"]: x for x in gov["sections"]}
    assert len(titles["Agent steps in this run"]["rows"]) == 6
    assert any(r[1] == "agent:recon" and r[3] == "ledger.post" for r in titles["Denied actions"]["rows"])
    assert {"POL-03", "POL-05", "POL-04"} <= {r[0] for r in titles["Policies applied"]["rows"]}
    fs = dict(fin["summary"])
    assert fs["Outstanding on partial receipts (USD)"] == "300.00" and fs["Exceptions raised"] == len(EXP["client-a"]["exceptions"])


def test_report_csv_guards_against_formula_injection():
    from ledgersight.core.reports import to_csv
    rep = {"title": "t", "summary": [["a", "=HYPERLINK(\"x\")"], ["b", "-12.50"]],
           "sections": [{"title": "s", "columns": ["c"], "rows": [["+cmd"]]}]}
    rows = list(csv.reader(io.StringIO(to_csv(rep).decode("utf-8-sig"))))
    assert rows[1][1].startswith("'=") and rows[2][1] == "-12.50" and rows[-1][0] == "'+cmd"


def test_report_other_client_concealed(rev, prep):
    run = golden_run(rev, "client-b")
    assert prep.get(f"/v1/workspaces/client-b/runs/{run['id']}/reports/finance.pdf").status_code == 404


# ---- 6. Ask LedgerSight -------------------------------------------------------------------
def ask(c, q, ws="client-a"):
    r = c.post(f"/v1/workspaces/{ws}/ask", headers=H, json={"question": q})
    assert r.status_code == 200, r.text
    return r.json()


def test_ask_unmatched_deposits_with_citation(rev):
    golden_run(rev)
    a = ask(rev, "Show unmatched deposits for Client 1")
    assert a["status"] == "ANSWERED" and [r[1] for r in a["rows"]] == ["HB010"]
    assert any(c["ref"] == "HB010" and "client-a_bank.csv, row" in c["source"] for c in a["citations"])
    assert a["trustops"]["decision"] == "ALLOW"


def test_ask_why_flagged_outstanding_and_who_approved(rev):
    run = golden_run(rev)
    why = ask(rev, "Why was HB004 flagged?")
    assert "possible duplicate" in why["answer"] and "POL-04" in why["answer"]
    out = ask(rev, "What is outstanding on INV-1005?")
    assert "$300.00" in out["answer"]
    assert "no approval recorded" in ask(rev, "Who approved HB005?")["answer"]
    exc = exc_for(run, "HB005")
    rev.post(f"{WS}/runs/{run['id']}/exceptions/{exc['id']}/decisions", headers=H, json={"decision": "ACCEPT", "note": "ok"})
    who = ask(rev, "Who approved HB005?")
    assert "reviewer@gov.test" in who["answer"] and exc["id"] in who["answer"]


def test_ask_date_filter_and_unknown_id(rev):
    golden_run(rev)
    a = ask(rev, "Show exceptions after 2026-10-09")
    assert {r[1] for r in a["rows"]} == {"HB009", "HB010", "HB013", "HB014"}  # HB013 CA tax review, HB014 UK FX review
    assert ask(rev, "Why was HB099 flagged?")["status"] == "NO_DATA"


@pytest.mark.parametrize("q", ["Show Bayview Marine's deposits", "Show Acme Corp's deposits", "list deposits for all clients"])
def test_ask_other_clients_refused_identically(rev, q):
    golden_run(rev)
    a = ask(rev, q)
    assert a["status"] == "REFUSED" and a["trustops"]["reason_code"] == "WORKSPACE_SCOPE"
    assert a["answer"] == ask(rev, "Show Acme Corp's deposits")["answer"]
    assert "bayview" not in json.dumps(a).lower() and "client-b" not in json.dumps(a).lower()
    ev = appmod.svc.ws["client-a"].log.events[-1]
    assert ev["actor_id"] == "agent:query" and ev["policy_decision"] == "DENY:WORKSPACE_SCOPE"


@pytest.mark.parametrize("q,code", [("select * from bank where 1=1", "SQL_NOT_ALLOWED"),
                                    ("approve HB006 now", "READ_ONLY_QUERY"), ("post the journal for HB006", "READ_ONLY_QUERY")])
def test_ask_sql_and_write_refused(rev, q, code):
    golden_run(rev)
    a = ask(rev, q)
    assert a["status"] == "REFUSED" and a["trustops"]["reason_code"] == code and a["trustops"]["policy_id"] == "POL-10"
    assert a["rows"] == [] and a["citations"] == []


def test_ask_cannot_reach_other_client_book(prep):
    assert prep.post("/v1/workspaces/client-b/ask", headers=H, json={"question": "summary"}).status_code == 404


# ---- 7. source labels, file types ----------------------------------------------------------
def test_source_labels_are_honest(rev):
    golden_run(rev)
    s = rev.get(WS + "/sources").json()
    k = {x["kind"]: x for x in s["kinds"]}
    assert k["bank"]["status"] == "SIMULATED" and k["ap"]["status"] == "SIMULATED" and k["bank"]["rows"] == EXP["client-a"]["bank_rows"]
    c = {x["id"]: x["status"] for x in s["connectors"]}
    assert c["quickbooks"] == "PLANNED" and c["xero"] == "PLANNED" and c["google-drive"] == "PLANNED"
    blob = (FIXTURES / "client-a_bank.csv").read_bytes()
    rev.post(WS + "/sources/bank/clear", headers=H)
    up = rev.post(WS + "/uploads", headers=H, data={"kind": "bank"}, files={"file": ("my_bank.csv", blob)}).json()
    rev.post(f"{WS}/uploads/{up['id']}/mapping", headers=H, json={"mapping": up["proposed_mapping"]})
    assert {x["kind"]: x for x in rev.get(WS + "/sources").json()["kinds"]}["bank"]["status"] == "VERIFIED_UPLOAD"


def _csv_to_json(name):
    return json.dumps(list(csv.DictReader(io.StringIO((FIXTURES / name).read_text())))).encode()


def test_json_bank_and_ledger_reconcile_like_csv(rev):
    rev.post(WS + "/reset", headers=H)
    for kind in ("bank", "ledger"):
        up = rev.post(WS + "/uploads", headers=H, data={"kind": kind},
                      files={"file": (f"{kind}.json", _csv_to_json(f"client-a_{kind}.csv"))}).json()
        r = rev.post(f"{WS}/uploads/{up['id']}/mapping", headers=H, json={"mapping": up["proposed_mapping"]}).json()
        assert r["health"]["rejected"] == [], r
    run = rev.post(WS + "/reconciliation-runs", headers=H).json()
    assert run["result"]["variance_cents"] == EXP["client-a"]["variance_cents"]


@pytest.mark.parametrize("blob", [b"{not json", b'{"a": 1}', b"[1, 2]"])
def test_bad_json_rejected(rev, blob):
    r = rev.post(WS + "/uploads", headers=H, data={"kind": "bank"}, files={"file": ("x.json", blob)})
    assert r.status_code == 422


def test_accounts_payable_upload_maps(rev):
    up = rev.post(WS + "/uploads", headers=H, data={"kind": "ap"},
                  files={"file": ("vendor_bills.csv", (FIXTURES / "client-a_ap.csv").read_bytes())}).json()
    assert up["proposed_mapping"]["bill_id"] == "bill_id" and up["proposed_mapping"]["open_balance"] == "open_balance"
    r = rev.post(f"{WS}/uploads/{up['id']}/mapping", headers=H, json={"mapping": up["proposed_mapping"]}).json()
    assert r["state"] in ("VALIDATED", "DUPLICATE_BATCH")


def test_rulebooks_list_policies_read_only(rev):
    rb = rev.get("/v1/rulebooks").json()
    assert [p["id"] for p in rb["policies"]][:3] == ["POL-01", "POL-02", "POL-03"] and len(rb["agents"]) == 6
    assert "Read-only" in rb["editing"]


def test_evidence_chain_still_verifies_after_everything(rev):
    assert rev.get(WS + "/evidence").json()["verification"]["state"] == "PASS"


# ---- 8. nothing client-specific is hardcoded --------------------------------------------
FORMER_NAMES = ["harborline", "bayview", "coral studio", "pelican", "mangrove", "gulfprint", "office depot", "tampa",
                "clouddraw", "vector stock", "bayfront", "harbour charters", "suncoast", "st. petersburg", "bay st"]


def test_no_client_names_or_addresses_in_code_or_sample_data():
    from pathlib import Path
    root = Path(appmod.__file__).resolve().parent
    files = [p for p in root.rglob("*") if p.is_file() and p.suffix in (".py", ".js", ".html", ".json", ".csv", ".tsv", ".qbo", ".eml", ".pdf")
             and "__pycache__" not in p.parts]
    hits = [(p.name, n) for p in files for n in FORMER_NAMES if n in p.read_bytes().decode("latin-1").lower()]
    assert hits == []
    clients = load("clients.json")["clients"]
    assert all(re.fullmatch(r"Client \d+", c["name"]) for c in clients)
    assert set(appmod.svc.ws) <= {c["id"] for c in clients}


# ---- 9. live agent monitor ----------------------------------------------------------------
def test_agent_monitor_shows_status_counts_and_run_step(rev):
    run = golden_run(rev)
    d = rev.get(WS + "/agents").json()
    a = {x["code"]: x for x in d["agents"]}
    assert d["run_id"] == run["id"] and set(a) == {"AG-01", "AG-02", "AG-03", "AG-04", "AG-05", "AG-06"}
    assert all(x["status"] == "WORKING" for x in a.values())  # all acted within the last few seconds
    assert a["AG-04"]["counts"]["ESCALATE"] == len(run["result"]["exceptions"])
    assert a["AG-03"]["last_run_step"]["rows_out"] == len(run["result"]["matches"]) + len(run["result"]["exceptions"])
    assert d["feed"] and d["feed"][0]["seq"] > d["feed"][-1]["seq"]  # newest first
    rev.post(WS + "/actions/attempt", headers=H, json={"action": "ledger.post", "as_agent": "AG-03"})
    a3 = next(x for x in rev.get(WS + "/agents").json()["agents"] if x["code"] == "AG-03")
    assert a3["status"] == "BLOCKED" and "POL-03" in a3["status_note"] and a3["counts"]["DENY"] >= 1


def test_agent_monitor_idle_after_window(rev, monkeypatch):
    golden_run(rev)
    monkeypatch.setattr(appmod.svc, "ACTIVE_SECONDS", -1)
    a = {x["code"]: x for x in rev.get(WS + "/agents").json()["agents"]}
    assert a["AG-05"]["status"] == "IDLE"


def test_agent_monitor_polling_does_not_flood_evidence(rev):
    golden_run(rev)
    n = len(appmod.svc.ws["client-a"].log.events)
    for _ in range(5):
        assert rev.get(WS + "/agents").status_code == 200
    assert len(appmod.svc.ws["client-a"].log.events) == n


def test_agent_monitor_other_client_concealed_and_denial_logged(prep):
    n = len(appmod.svc.security_log.events)
    assert prep.get("/v1/workspaces/client-b/agents").status_code == 404
    assert len(appmod.svc.security_log.events) == n + 1  # the refused look is still recorded


def test_client_profile_rename_is_reviewer_only_and_recorded(rev, prep):
    old = dict(appmod.svc.ws["client-a"].meta)
    try:
        r = rev.post(WS + "/profile", headers=H, json={"name": "  Northwind   Books  ", "period": "November 2026"})
        assert r.status_code == 200 and r.json()["name"] == "Northwind Books"
        assert rev.get(WS).json()["name"] == "Northwind Books"
        assert any(e["action"] == "workspace.profile_updated" for e in appmod.svc.ws["client-a"].log.events)
        assert rev.post(WS + "/profile", headers=H, json={"name": ""}).status_code == 422
        assert rev.post(WS + "/profile", headers=H, json={"name": "x" * 81}).status_code == 422
        r = prep.post(WS + "/profile", headers=H, json={"name": "Preparer rename"})
        assert r.status_code == 403 and r.json()["detail"]["reason_code"] == "ROLE_NOT_AUTHORISED"
    finally:
        appmod.svc.ws["client-a"].meta.update(old)


def test_evidence_chain_survives_concurrent_requests():
    """The server handles requests on several threads; simultaneous writes must not break the chain."""
    from concurrent.futures import ThreadPoolExecutor
    from ledgersight.core import evidence as ev_mod
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    key = Ed25519PrivateKey.generate()
    log = ev_mod.EvidenceLog(key, "t")
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(lambda i: log.append(action=f"a{i}", policy_decision="ALLOW:OK"), range(400)))
    pub = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    assert len(log.events) == 400 and ev_mod.verify(log.events, pub)["state"] == "PASS"


def test_parallel_page_loads_keep_audit_trail_valid(rev):
    from concurrent.futures import ThreadPoolExecutor
    golden_run(rev)
    paths = [WS, WS + "/evidence", WS + "/agents", WS + "/sources", WS + "/trace"] * 8
    with ThreadPoolExecutor(max_workers=10) as pool:
        assert all(r.status_code == 200 for r in pool.map(rev.get, paths))
    assert rev.get(WS + "/evidence").json()["verification"]["state"] == "PASS"
