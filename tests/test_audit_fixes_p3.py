"""Audit batch E (P3): clarity and hardening regressions."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_ledgersight import H, appmod, make_user  # noqa: F401
from ledgersight.auth import totp_code

ROOT = Path(__file__).resolve().parent.parent
PACK = ROOT / "demo_files" / "0_client1_october"
WS = "/v1/workspaces/client-a"


@pytest.fixture(scope="module")
def owner():
    return make_user("p3-owner@demo.test", "owner_reviewer", ["client-a"])


def upload_confirm(c, name, blob):
    u = c.post(WS + "/uploads", headers=H, data={"kind": "auto"}, files={"file": (name, blob)}).json()
    return c.post(f"{WS}/uploads/{u['id']}/mapping", headers=H, json={"mapping": u["proposed_mapping"]}).json()


def test_func07_unchanged_cases_keep_their_decision_and_status_flags_stale(owner):
    owner.post(WS + "/reset", headers=H)
    for n in ("01_bank_statement_October.csv", "02_general_ledger_October.xlsx", "03_open_invoices.csv", "04_vendor_bills.csv"):
        upload_confirm(owner, n, (PACK / n).read_bytes())
    run1 = owner.post(WS + "/reconciliation-runs", headers=H).json()
    dup = next(e for e in run1["result"]["exceptions"] if e["type"] == "POSSIBLE_DUPLICATE")
    owner.post(f"{WS}/runs/{run1['id']}/exceptions/{dup['id']}/decisions", headers=H, json={"decision": "REJECT", "note": "not a duplicate"})
    upload_confirm(owner, "05_supporting_documents.csv", (PACK / "05_supporting_documents.csv").read_bytes())
    st = next(w for w in owner.get("/v1/workspaces").json() if w["id"] == "client-a")["status"]
    assert st["stale"] is True  # files changed since the last run
    run2 = owner.post(WS + "/reconciliation-runs", headers=H).json()
    assert run2["id"] != run1["id"]
    kept = next(e for e in run2["result"]["exceptions"] if e["type"] == "POSSIBLE_DUPLICATE")
    assert kept["status"] == "REJECTED" and kept["carried_from"] == run1["id"] and kept["review_note"] == "not a duplicate"
    assert any(e["action"] == "exception.decision_carried" for e in appmod.svc.ws["client-a"].log.events)
    st = next(w for w in owner.get("/v1/workspaces").json() if w["id"] == "client-a")["status"]
    assert st["stale"] is False


def test_sec15_authenticator_code_works_once():
    inv = appmod.auth.invite("replay@demo.test", "r", "preparer", {"client-a"})
    appmod.auth.accept(inv.token, "correct horse battery", totp_code(inv.totp), True)
    c = TestClient(appmod.app)
    code = totp_code(inv.totp)
    body = {"user_id": "replay@demo.test", "password": "correct horse battery", "code": code}
    if not appmod.auth.users["replay@demo.test"].totp:
        pytest.skip("MFA off in this environment")
    assert c.post("/v1/login", json=body).status_code == 200
    assert c.post("/v1/login", json=body).status_code == 401  # same code again: refused
    appmod._LOGIN_FAILS.clear()


def test_sec15_csp_and_headers(owner):
    r = owner.get("/v1/me")
    csp = r.headers["content-security-policy"]
    assert "base-uri 'self'" in csp and "form-action 'self'" in csp and "object-src 'none'" in csp
