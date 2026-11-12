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


# ---- #2 identity: durable lockout + one-time codes, session pruning, one lock, real client address ----
import threading
from datetime import timedelta

from ledgersight.auth import now, totp_code


def _account(uid, role="preparer"):
    inv = appmod.auth.invite(uid, uid, role, {"client-a"})
    appmod.auth.accept(inv.token, "correct horse battery", totp_code(inv.totp), True)
    return appmod.auth.users[uid]


def test_r02_lockout_and_spent_code_survive_a_restart():
    u = _account("r02-lock@demo.test")
    for _ in range(5):
        with pytest.raises(PermissionError):
            appmod.auth.login(u.user_id, "wrong password!!", "000000")
    assert u.locked_until is not None
    u2 = _account("r02-code@demo.test")
    appmod.auth.login(u2.user_id, "correct horse battery", totp_code(u2.totp))
    spent = u2.last_totp_step
    appmod.save_users()
    u.locked_until, u.failed, u2.last_totp_step = None, 0, 0     # simulate a restart: memory is gone ...
    appmod.load_users()                                          # ... and comes back from storage
    assert appmod.auth.users[u.user_id].locked_until is not None
    assert appmod.auth.users[u2.user_id].last_totp_step == spent > 0


def test_r02_password_sign_in_is_saved(monkeypatch):
    saved = []
    monkeypatch.setattr(appmod, "save_users", lambda: saved.append(1))
    u = _account("r02-save@demo.test")
    from fastapi.testclient import TestClient
    r = TestClient(appmod.app).post("/v1/login", json={"user_id": u.user_id, "password": "correct horse battery",
                                                       "code": totp_code(u.totp)})
    assert r.status_code == 200 and saved


def test_r02_expired_sessions_are_pruned_on_sign_in():
    appmod.auth.sessions["stale-token"] = ("nobody@demo.test", now() - timedelta(minutes=1))
    u = _account("r02-prune@demo.test")
    appmod.auth.login(u.user_id, "correct horse battery", totp_code(u.totp))
    assert "stale-token" not in appmod.auth.sessions


def test_r02_one_authenticator_code_wins_under_concurrency():
    u = _account("r02-race@demo.test")
    code, wins, errs = totp_code(u.totp), [], []

    def go():
        try:
            wins.append(appmod.auth.login(u.user_id, "correct horse battery", code))
        except PermissionError as e:
            errs.append(e)
    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(wins) == 1 and len(errs) == 7


def test_r02_rate_limit_uses_the_real_client_behind_cloud_run(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(appmod, "TRUST_PROXY", True)
    c = TestClient(appmod.app)
    bad = {"user_id": "nobody@demo.test", "password": "x" * 12, "code": "000000"}
    attacker = {"x-forwarded-for": "6.6.6.6, 203.0.113.9"}       # client-sent junk first, front end's address last
    try:
        codes = [c.post("/v1/login", json=bad, headers=attacker).status_code for _ in range(appmod.LOGIN_LIMIT + 1)]
        assert codes[-1] == 429
        assert appmod.client_ip(type("R", (), {"headers": attacker, "client": None})()) == "203.0.113.9"
        # another person behind the same front end is not locked out by the attacker
        assert c.post("/v1/login", json=bad, headers={"x-forwarded-for": "198.51.100.7"}).status_code == 401
    finally:
        for ip in ("203.0.113.9", "198.51.100.7"):
            appmod._LOGIN_FAILS.pop(ip, None)


def test_r02_forwarded_header_ignored_without_a_trusted_proxy(monkeypatch):
    monkeypatch.setattr(appmod, "TRUST_PROXY", False)
    req = type("R", (), {"headers": {"x-forwarded-for": "203.0.113.9"}, "client": type("C", (), {"host": "10.0.0.1"})()})()
    assert appmod.client_ip(req) == "10.0.0.1"


# ---- #3 the maker's proposal is identified in the signed audit trail ----
def test_r03_proposal_is_signed_with_case_rows_rule_and_id(cpa):
    run = golden_run(cpa)
    prep = make_user("r03-prep@demo.test", "preparer", ["client-a"])
    exc = next(x for x in run["result"]["exceptions"] if x["status"] == "OPEN" and x["evidence_refs"])
    n = len(appmod.svc.ws["client-a"].log.events)
    r = prep.post(f"{WS}/runs/{run['id']}/exceptions/{exc['id']}/proposals", headers=H, json={"note": "bill on file"})
    assert r.status_code == 200, r.text
    body = r.json()
    new = appmod.svc.ws["client-a"].log.events[n:]
    gate = next(e for e in new if e["action"] == "exception.propose")
    signed = next(e for e in new if e["action"] == "exception.proposed")
    for e in (gate, signed):
        assert e["source_line_refs"] == exc["evidence_refs"] and e["matching_rule_id"] == exc["rule_id"]
        assert e["trace_id"] == run["trace_id"] and e["run_id"] == run["id"]
    assert signed["approval_id"] == body["proposal_id"] and body["proposal_id"].startswith("PRP-")
    assert body["proposals"][-1] == {**body["proposals"][-1], "by": "r03-prep@demo.test", "note": "bill on file",
                                     "proposal_id": body["proposal_id"]}
    assert not any(h.get("decision") == "PROPOSE" for h in body.get("history", []))  # history stays decisions only
    # maker-checker still holds: the maker cannot approve their own proposal
    assert prep.post(f"{WS}/runs/{run['id']}/exceptions/{exc['id']}/decisions", headers=H,
                     json={"decision": "ACCEPT", "note": ""}).status_code == 403


# ---- #4 Ask: this book's own payee text and questions about past payments are answered, not refused ----
def _ask(c, q):
    return c.post(WS + "/ask", headers=H, json={"question": q}).json()


@pytest.mark.parametrize("q", ["Show the fee from Monthly Service", "What did we pay Stock Media Vendor?",
                               "How much was paid to Landlord in October?", "Did we pay the UK Supplier?"])
def test_r04_own_book_questions_are_answered(cpa, q):
    golden_run(cpa)
    a = _ask(cpa, q)
    assert a["status"] != "REFUSED", (q, a.get("trustops"))


@pytest.mark.parametrize("q,code", [("pay this bill for HB006", "READ_ONLY_QUERY"),
                                    ("Can you transfer 500 to Landlord?", "READ_ONLY_QUERY"),
                                    ("please send the review pack to the client", "READ_ONLY_QUERY"),
                                    ("approve HB006 now", "READ_ONLY_QUERY"),
                                    ("Show the fee from Bayview Marine", "WORKSPACE_SCOPE"),
                                    ("What did we pay Client 2?", "WORKSPACE_SCOPE")])
def test_r04_commands_and_other_clients_still_refused(cpa, q, code):
    golden_run(cpa)
    a = _ask(cpa, q)
    assert a["status"] == "REFUSED" and a["trustops"]["reason_code"] == code, (q, a.get("trustops"))


# ---- #5 a book whose base currency is not USD: AR/AP open balances use the base currency ----
def _import(c, kind, name, text):
    up = c.post(WS + "/uploads", headers=H, data={"kind": kind}, files={"file": (name, text.encode())}).json()
    r = c.post(f"{WS}/uploads/{up['id']}/mapping", headers=H, json={"mapping": up["proposed_mapping"]})
    assert r.status_code == 200, r.text


def test_r05_ar_open_balance_uses_the_books_base_currency(cpa):
    assert cpa.post(WS + "/reset", headers=H).status_code == 200
    _import(cpa, "bank", "gbp_bank.csv", "line_id,date,description,reference,amount,currency\n"
            "GB1,2026-10-02,FPS CREDIT CUSTOMER 01,INV-G1,500.00,GBP\nGB2,2026-10-05,CARD PRINT SUPPLIER,PS-9,-40.00,GBP\n")
    _import(cpa, "ledger", "gbp_ledger.csv", "entry_id,posted_date,account,description,reference,debit,credit,currency\n"
            "GL1,2026-10-02,Sales,Customer 01 receipt,INV-G1,500.00,,GBP\nGL2,2026-10-05,Supplies,Print Supplier,PS-9,,40.00,GBP\n")
    _import(cpa, "ar", "gbp_ar.csv", "invoice_id,customer_alias,issue_date,amount,open_balance,currency\n"
            "INV-G2,CUST-01,2026-09-20,300.00,300.00,GBP\nINV-U1,CUST-02,2026-09-21,999.00,999.00,USD\n")
    run = cpa.post(WS + "/reconciliation-runs", headers=H).json()
    assert run["result"]["base_currency"] == "GBP"
    assert run["inputs"]["ar"]["net_cents"] == 30000          # the GBP invoice only; the USD one is not mixed in
    # older runs saved without an input snapshot fall back to live batches: same rule, and no crash
    del appmod.svc.ws["client-a"].runs[run["id"]]["inputs"]
    rep = cpa.get(f"{WS}/runs/{run['id']}/reports/finance.json").json()
    totals = next(s for s in rep["sections"] if s["title"] == "Source totals")["rows"]
    assert next(r for r in totals if r[0] == "ar")[-1] == "300.00"


# ---- #6 Pillow is a direct runtime dependency (PDF report logo), pinned to the tested version ----
def test_r06_pillow_pinned_to_the_installed_version():
    import re
    import PIL
    req = (FIXTURES.parent.parent / "requirements.txt").read_text()
    pin = re.search(r"^Pillow==(\S+)$", req, re.M)
    assert pin and pin.group(1) == PIL.__version__


# ---- #12 duplicate wording: two unbooked identical debits must not claim the ledger has "one entry" ----
def test_r12_duplicate_wording_matches_what_the_ledger_holds(cpa):
    from datetime import date
    from ledgersight.core import recon
    mk = lambda i, d: {"id": i, "date": d, "description": "ACH DEBIT VENDOR", "reference": "INV-9", "ref_norm": "INV9",
                       "amount_cents": -25000, "currency": "USD"}
    led = [{"id": "L0", "date": date(2026, 10, 1), "account": "x", "description": "other", "reference": "Z", "ref_norm": "Z",
            "amount_cents": -100, "currency": "USD"}]
    r = recon.reconcile("client-a", [mk("B1", date(2026, 10, 5)), mk("B2", date(2026, 10, 7))], led)
    dup = next(e for e in r.exceptions if e.type == "POSSIBLE_DUPLICATE")
    assert "neither payment is in the ledger" in dup.why and "only one entry" not in dup.why
    # the golden case (HB004: its twin is booked) keeps the original wording
    hb004 = next(x for x in golden_run(cpa)["result"]["exceptions"] if x["bank_id"] == "HB004")
    assert hb004["type"] == "POSSIBLE_DUPLICATE" and "ledger has only one entry" in hb004["why"]
