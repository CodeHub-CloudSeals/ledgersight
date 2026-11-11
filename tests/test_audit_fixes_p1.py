"""Audit batch C (P1): money path and governance regressions (finding ids in test names)."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_ledgersight import H, appmod, golden_run, make_user  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
PACK = ROOT / "demo_files" / "0_client1_october"
WS = "/v1/workspaces/client-a"


@pytest.fixture(scope="module")
def owner():
    return make_user("p1-owner@demo.test", "owner_reviewer", ["client-a"])


@pytest.fixture(scope="module")
def prep():
    return make_user("p1-prep@demo.test", "preparer", ["client-a"])


def upload_confirm(c, name, blob, wid="client-a"):
    r = c.post(f"/v1/workspaces/{wid}/uploads", headers=H, data={"kind": "auto"}, files={"file": (name, blob)})
    assert r.status_code == 200, r.text
    u = r.json()
    m = c.post(f"/v1/workspaces/{wid}/uploads/{u['id']}/mapping", headers=H, json={"mapping": u["proposed_mapping"]})
    assert m.status_code == 200, m.text
    return m.json()


def pack_run(c, bank_blob=None):
    c.post(WS + "/reset", headers=H)
    upload_confirm(c, "01_bank_statement_October.csv", bank_blob or (PACK / "01_bank_statement_October.csv").read_bytes())
    for n in ("02_general_ledger_October.xlsx", "03_open_invoices.csv", "04_vendor_bills.csv", "05_supporting_documents.csv"):
        upload_confirm(c, n, (PACK / n).read_bytes())
    r = c.post(WS + "/reconciliation-runs", headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def test_func01_rejected_bank_row_is_a_visible_case(owner):
    bad = (PACK / "01_bank_statement_October.csv").read_text().replace("HB006,2026-10-09", "HB006,2026-13-09").encode()
    run = pack_run(owner, bad)
    rej = [e for e in run["result"]["exceptions"] if e["type"] == "REJECTED_SOURCE_ROW"]
    assert len(rej) == 1 and rej[0]["amount_cents"] == -31500 and rej[0]["status"] == "OPEN"
    assert "NOT in the bank total" in rej[0]["why"] and rej[0]["trustops"]["decision"] == "ESCALATE"
    assert run["result"]["rejected_source_rows"] == 1
    assert "could not be imported" in run["workflow"][0]["summary"]
    fin = owner.get(f"{WS}/runs/{run['id']}/reports/finance.json").text
    assert "could not be imported" in fin


def test_func04_foreign_currency_rows_kept_out_of_base_totals(owner):
    golden = pack_run(owner)["result"]
    eur = (PACK / "01_bank_statement_October.csv").read_text().rstrip("\n") + "\nHB099,2026-10-30,EU CUSTOMER,EU-1,100.00,EUR\n"
    run = pack_run(owner, eur.encode())["result"]
    assert run["bank_net_cents"] == golden["bank_net_cents"] and run["base_currency"] == "USD"
    fx = [e for e in run["exceptions"] if e["type"] == "CURRENCY_MISMATCH"]
    assert len(fx) == 1 and fx[0]["bank_id"] == "HB099" and fx[0]["client_status"] == "FX_REVIEW"
    assert fx[0]["trustops"]["policy_id"] == "POL-12"
    assert run["unexplained_variance_cents"] == golden["unexplained_variance_cents"]


def test_func02_run_snapshots_its_inputs(owner):
    run = pack_run(owner)
    assert run["inputs"]["bank"]["rows"] == 14 and run["inputs"]["ar"]["filename"] == "03_open_invoices.csv"
    pack = owner.get(f"{WS}/runs/{run['id']}/review-pack.json").json()
    assert pack["inputs"]["bank"]["sha256"] == run["inputs"]["bank"]["sha256"]
    # change the live AR batch behind the run's back: the old run's outputs must not move
    appmod.svc.ws["client-a"].batches["ar"]["rows"] = []
    appmod.svc.ws["client-a"].batches["ar"]["sha256"] = "changed"
    pack2 = owner.get(f"{WS}/runs/{run['id']}/review-pack.json").json()
    assert pack2["inputs"]["ar"] == pack["inputs"]["ar"]
    fin = owner.get(f"{WS}/runs/{run['id']}/reports/finance.json").text
    assert run["inputs"]["ar"]["sha256"] in fin and "changed" not in fin


def test_sec03_preparer_cannot_clear_after_decisions(owner, prep):
    run = pack_run(owner)
    exc = next(e for e in run["result"]["exceptions"] if e["status"] == "OPEN")
    assert owner.post(f"{WS}/runs/{run['id']}/exceptions/{exc['id']}/decisions", headers=H,
                      json={"decision": "REJECT", "note": "checked"}).status_code == 200
    r = prep.post(WS + "/sources/bank/clear", headers=H)
    assert r.status_code == 403 and r.json()["detail"]["reason_code"] == "DECISIONS_EXIST"
    assert owner.get(f"{WS}/runs/{run['id']}").status_code == 200
    assert owner.post(WS + "/sources/bank/clear", headers=H).status_code == 200


def test_sec04_lockout_is_temporary_generic_and_unlockable(owner):
    make_user("lock-me@demo.test", "preparer", ["client-a"])
    c = TestClient(appmod.app)
    msgs = {c.post("/v1/login", json={"user_id": "lock-me@demo.test", "password": "wrong-password-x", "code": "000000"}).json()["detail"]
            for _ in range(6)}
    unknown = c.post("/v1/login", json={"user_id": "nobody@demo.test", "password": "x", "code": "0"}).json()["detail"]
    assert len(msgs) == 1 and unknown in msgs
    u = appmod.auth.users["lock-me@demo.test"]
    assert u.locked_until is not None
    assert owner.post("/v1/users/lock-me@demo.test/unlock", headers=H).status_code == 200
    assert u.failed == 0 and u.locked_until is None
    appmod._LOGIN_FAILS.clear()


def test_sec04_failed_sign_ins_rate_limited_per_address():
    c = TestClient(appmod.app)
    appmod._LOGIN_FAILS.clear()
    codes = [c.post("/v1/login", json={"user_id": "nobody@demo.test", "password": "x", "code": "0"}).status_code for _ in range(22)]
    assert codes[0] == 401 and codes[-1] == 429
    appmod._LOGIN_FAILS.clear()


def test_sec05_cannot_revoke_outside_scope():
    a = make_user("scope-a@demo.test", "owner_reviewer", ["client-a"])
    make_user("scope-b@demo.test", "owner_reviewer", ["client-b"])
    assert a.post("/v1/users/scope-b@demo.test/revoke", headers=H).status_code == 404
    assert a.post("/v1/users/no-such-person@demo.test/revoke", headers=H).status_code == 404
    assert not appmod.auth.users["scope-b@demo.test"].revoked


@pytest.mark.parametrize("q", ["Show Client 9 exceptions", "Show Client 2 exceptions", "deposits for Nonexistent Marine Co",
                               "what about client-b", "Secret Holdings Ltd unmatched items"])
def test_sec06_client_like_names_refused_identically(owner, q):
    golden_run(owner)
    a = owner.post(WS + "/ask", headers=H, json={"question": q}).json()
    assert a["status"] == "REFUSED" and a["trustops"]["reason_code"] == "WORKSPACE_SCOPE"


def test_sec06_own_book_questions_still_answered(owner):
    golden_run(owner)
    for q in ("show unmatched deposits", "why was HB004 flagged", "show Client 1 exceptions"):
        a = owner.post(WS + "/ask", headers=H, json={"question": q}).json()
        assert a["status"] != "REFUSED", (q, a)


def test_sec07_readiness_fails_when_key_does_not_match_history(owner):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    golden_run(owner)
    svc = appmod.svc
    real = svc._key
    try:
        svc._key, svc._chain_ok = Ed25519PrivateKey.generate(), None
        r = svc.ready()
        assert r["ready"] is False and r["checks"]["audit_chain_matches_key"] is False
    finally:
        svc._key, svc._chain_ok = real, None
    assert svc.ready()["checks"]["audit_chain_matches_key"] is True


def test_sec09_trust_lists_only_own_clients(owner):
    assert owner.get("/v1/trust").json()["workspaces"] == ["client-a"]


def test_sec10_invite_limits(owner):
    r = owner.post("/v1/invites", headers=H, json={"user_id": "long@demo.test", "workspaces": ["client-a"], "account_days": 100000})
    assert r.status_code == 422
    t1 = appmod.auth.invite("reinvite@demo.test", "r", "preparer", {"client-a"}).token
    appmod.auth.invite("reinvite@demo.test", "r", "preparer", {"client-a"})
    with pytest.raises(PermissionError):
        appmod.auth.get_invite(t1)
    appmod.auth.invite("other-scope@demo.test", "o", "preparer", {"client-b"})
    r = owner.post("/v1/invites", headers=H, json={"user_id": "other-scope@demo.test", "workspaces": ["client-a"], "role": "owner_reviewer"})
    assert r.status_code == 403


def test_sec11_bootstrap_keeps_revocation(monkeypatch):
    from ledgersight.auth import AuthStore
    monkeypatch.setenv("AVOCET_BOOTSTRAP_USER", "boot@demo.test")
    monkeypatch.setenv("AVOCET_BOOTSTRAP_PASSWORD", "correct horse battery staple")
    monkeypatch.setenv("AVOCET_BOOTSTRAP_TOTP", "JBSWY3DPEHPK3PXP")
    a = AuthStore()
    a.bootstrap({"client-a"})
    a.revoke("boot@demo.test")
    a.bootstrap({"client-a"})
    assert a.users["boot@demo.test"].revoked


def test_sec14_google_reviewer_allow_list(monkeypatch):
    from ledgersight.auth import AuthStore
    monkeypatch.setenv("AVOCET_GOOGLE_DEFAULT_ROLE", "owner_reviewer")
    monkeypatch.setenv("AVOCET_GOOGLE_REVIEWERS", "boss@firm.test")
    a = AuthStore()
    a.google_login("boss@firm.test", "Boss", {"client-a"})
    a.google_login("staff@firm.test", "Staff", {"client-a"})
    assert a.users["boss@firm.test"].role == "owner_reviewer" and a.users["staff@firm.test"].role == "preparer"
