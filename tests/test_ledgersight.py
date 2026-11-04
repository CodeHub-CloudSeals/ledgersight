"""Avocet sandbox acceptance tests, mapped to plan section 13 (G-Dxx) and section 14."""
import json
import os
from datetime import timedelta

os.environ["AVOCET_SECURE_COOKIE"] = "0"
os.environ.setdefault("AVOCET_DATA_DIR", "/tmp/avocet-test-data")

import pytest
from fastapi.testclient import TestClient

from ledgersight import app as appmod
from ledgersight.auth import now, totp_code
from ledgersight.service import FIXTURES

H = {"x-ledgersight": "1"}
EXP = json.loads((FIXTURES / "expectations.json").read_text())


def make_user(user_id, role, ws, pw="correct horse battery"):
    inv = appmod.auth.invite(user_id, user_id, role, set(ws))
    appmod.auth.accept(inv.token, pw, totp_code(inv.totp), True)
    c = TestClient(appmod.app)
    r = c.post("/v1/login", json={"user_id": user_id, "password": pw, "code": totp_code(inv.totp)})
    assert r.status_code == 200, r.text
    return c


@pytest.fixture(scope="module")
def sam():
    return make_user("samantha@avocet.test", "owner_reviewer", ["harborline", "bayview"])


@pytest.fixture(scope="module")
def alex():
    return make_user("alex@avocet.test", "preparer", ["harborline"])


def golden_run(c, wid="harborline"):
    assert c.post(f"/v1/workspaces/{wid}/reset", headers=H).status_code == 200
    assert c.post(f"/v1/workspaces/{wid}/samples/load", headers=H).status_code == 200
    r = c.post(f"/v1/workspaces/{wid}/reconciliation-runs", headers=H)
    assert r.status_code == 200, r.text
    return r.json()


# G-D01
def test_unauthenticated_rejected():
    assert TestClient(appmod.app).get("/v1/workspaces").status_code == 401


def test_wrong_mfa_code_rejected():
    inv = appmod.auth.invite("mfa@avocet.test", "m", "preparer", {"harborline"})
    appmod.auth.accept(inv.token, "correct horse battery", totp_code(inv.totp), True)
    r = TestClient(appmod.app).post("/v1/login", json={"user_id": "mfa@avocet.test", "password": "correct horse battery", "code": "000000"})
    assert r.status_code == 401


def test_invite_single_use():
    inv = appmod.auth.invite("once@avocet.test", "o", "preparer", {"harborline"})
    appmod.auth.accept(inv.token, "correct horse battery", totp_code(inv.totp), True)
    assert TestClient(appmod.app).get(f"/v1/invites/{inv.token}").status_code == 410


def test_expired_and_revoked_accounts_lose_access():
    c = make_user("temp@avocet.test", "preparer", ["harborline"])
    assert c.get("/v1/me").status_code == 200
    appmod.auth.users["temp@avocet.test"].expires_at = now() - timedelta(seconds=1)
    assert c.get("/v1/me").status_code == 401
    c2 = make_user("temp2@avocet.test", "preparer", ["harborline"])
    appmod.auth.revoke("temp2@avocet.test")
    assert c2.get("/v1/me").status_code == 401


def test_csrf_header_required(sam):
    assert sam.post("/v1/workspaces/harborline/reconciliation-runs").status_code == 403


# G-D03 / G-D04 / G-D10: fresh live run from cleared workspace equals business truth
def test_golden_run_from_cleared_workspace(sam):
    run = golden_run(sam)
    e = EXP["harborline"]
    res = run["result"]
    assert {m["bank_id"]: m["ledger_id"] for m in res["matches"]} == e["matches"]
    assert {(x["bank_id"], x["type"]) for x in res["exceptions"]} == set(e["exceptions"].items())
    assert res["variance_cents"] == e["variance_cents"] and res["unexplained_variance_cents"] == 0
    # repeat after reset gives identical business result
    again = golden_run(sam)["result"]
    assert again["matches"] == res["matches"]


def test_duplicate_batch_not_imported_twice(sam):
    golden_run(sam)
    blob = (FIXTURES / "harborline_bank.csv").read_bytes()
    up = sam.post("/v1/workspaces/harborline/uploads", headers=H, data={"kind": "bank"},
                  files={"file": ("harborline_bank.csv", blob, "text/csv")}).json()
    assert up["duplicate_of"]
    r = sam.post(f"/v1/workspaces/harborline/uploads/{up['id']}/mapping", headers=H,
                 json={"mapping": up["proposed_mapping"]}).json()
    assert r["state"] == "DUPLICATE_BATCH"


def test_alt_header_upload_maps(sam):
    blob = (FIXTURES / "harborline_bank_alt_headers.csv").read_bytes()
    up = sam.post("/v1/workspaces/harborline/uploads", headers=H, data={"kind": "bank"},
                  files={"file": ("bank_export.csv", blob, "text/csv")}).json()
    assert up["proposed_mapping"]["amount"] == "Amount (USD)"


@pytest.mark.parametrize("name,blob", [("x.xlsm", b"PK\x03\x04"), ("x.csv", b"PK\x03\x04zz"), ("x.pdf", b"%PDF-1.4")])
def test_unsafe_uploads_rejected(sam, name, blob):
    r = sam.post("/v1/workspaces/harborline/uploads", headers=H, data={"kind": "bank"}, files={"file": (name, blob)})
    assert r.status_code == 422


# G-D02: cross-client denial, concealed as 404
def test_cross_client_concealed(alex, sam):
    run = golden_run(sam, "bayview")
    for path in ["/v1/workspaces/bayview", f"/v1/workspaces/bayview/runs/{run['id']}",
                 f"/v1/workspaces/bayview/runs/{run['id']}/export.csv", "/v1/workspaces/bayview/evidence",
                 "/v1/workspaces/bayview/samples/bayview_bank.csv"]:
        assert alex.get(path).status_code == 404, path
    assert [w["id"] for w in alex.get("/v1/workspaces").json()] == ["harborline"]
    assert alex.get("/v1/workspaces/harborline/samples/bayview_bank.csv").status_code == 404


# G-D05: named reviewer decides; preparer cannot; maker cannot approve own proposal; no side effects
def test_review_controls(sam, alex):
    run = golden_run(sam)
    exc = next(x for x in run["result"]["exceptions"] if x["bank_id"] == "HB005")
    base = f"/v1/workspaces/harborline/runs/{run['id']}/exceptions/{exc['id']}"
    assert alex.post(base + "/decisions", headers=H, json={"decision": "ACCEPT"}).status_code == 403
    r = sam.post(base + "/decisions", headers=H, json={"decision": "ACCEPT", "note": "partial ok"})
    assert r.status_code == 200 and r.json()["reviewer"] == "samantha@avocet.test"
    assert "Nothing posted" in r.json()["effect"]
    assert sam.post(base + "/decisions", headers=H, json={"decision": "REJECT"}).status_code == 422


def test_self_approval_denied():
    c = make_user("dual@avocet.test", "owner_reviewer", ["harborline"])
    run = golden_run(c)
    exc = run["result"]["exceptions"][0]
    base = f"/v1/workspaces/harborline/runs/{run['id']}/exceptions/{exc['id']}"
    appmod.svc.ws["harborline"].runs[run["id"]]["result"]["exceptions"][0]["proposed_by"] = "dual@avocet.test"
    r = c.post(base + "/decisions", headers=H, json={"decision": "ACCEPT"})
    assert r.status_code == 403 and r.json()["detail"]["reason_code"] == "MAKER_CHECKER_SELF_APPROVAL"


@pytest.mark.parametrize("body,code", [
    ({"action": "ledger.post"}, "ACTION_OUTSIDE_PRODUCT_BOUNDARY"),
    ({"action": "bank.payment"}, "ACTION_OUTSIDE_PRODUCT_BOUNDARY"),
    ({"action": "email.send"}, "ACTION_OUTSIDE_PRODUCT_BOUNDARY"),
    ({"action": "run.create", "model_route": "public-llm"}, "MODEL_ROUTE_NOT_ALLOWLISTED"),
    ({"action": "upload.create", "data_class": "tax_return"}, "DATA_CLASS_NOT_APPROVED"),
])
def test_gate_denies_and_logs(sam, body, code):
    n = len(appmod.svc.ws["harborline"].log.events)
    r = sam.post("/v1/workspaces/harborline/actions/attempt", headers=H, json=body)
    assert r.status_code == 403 and r.json()["reason_code"] == code and r.json()["executed"] is False
    ev = appmod.svc.ws["harborline"].log.events
    assert len(ev) == n + 1 and ev[-1]["policy_decision"] == f"DENY:{code}"


# G-D06: signed evidence PASS, tamper FAIL on a copy, original intact
def test_evidence_and_tamper(sam):
    golden_run(sam)
    ev = sam.get("/v1/workspaces/harborline/evidence").json()
    assert ev["verification"]["state"] == "PASS"
    t = sam.post("/v1/workspaces/harborline/evidence/tamper-check", headers=H).json()
    assert t["verification"]["state"] == "FAIL" and t["original_chain"]["state"] == "PASS"


# G-D09: export carries source line ids; pack verifies offline
def test_exports(sam, tmp_path):
    run = golden_run(sam)
    csv_ = sam.get(f"/v1/workspaces/harborline/runs/{run['id']}/export.csv").text
    for line in ("HB001", "GL-H-101", "HB004", "POSSIBLE_DUPLICATE", "HB005"):
        assert line in csv_
    pack = sam.get(f"/v1/workspaces/harborline/runs/{run['id']}/review-pack.json").json()
    p = tmp_path / "pack.json"
    p.write_text(json.dumps(pack))
    import subprocess, sys
    out = subprocess.run([sys.executable, "-m", "ledgersight.verify_pack", str(p)], capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.startswith("PASS")


def test_trust_view_is_honest(sam):
    t = sam.get("/v1/trust").json()
    assert t["model_routes_in_use"] == ["deterministic"]
    assert "Not an independently verified" in t["region_note"]


# G-D07: text-PDF intake with cited fields; unsafe content flagged; scans refused safely
def _pdf(c, name):
    return c.post("/v1/workspaces/harborline/documents", headers=H,
                  files={"file": (name, (FIXTURES / name).read_bytes(), "application/pdf")})


def test_pdf_receipt_clears_missing_receipt(sam):
    run1 = golden_run(sam)
    assert any(x["bank_id"] == "HB009" and x["type"] == "MISSING_RECEIPT" for x in run1["result"]["exceptions"])
    up = _pdf(sam, "harborline_receipt_OD-7741.pdf").json()
    f = {k: v["value"] for k, v in up["fields"].items()}
    assert f["reference"] == "OD-7741" and f["amount"] == "245.00" and up["fields"]["amount"]["source_text"].startswith("Total")
    assert sam.post(f"/v1/workspaces/harborline/documents/{up['id']}/confirm", headers=H, json={"fields": f, "doc_type": "receipt"}).status_code == 200
    run2 = sam.post("/v1/workspaces/harborline/reconciliation-runs", headers=H).json()
    assert run2["id"] != run1["id"]
    assert not any(x["bank_id"] == "HB009" for x in run2["result"]["exceptions"])


def test_pdf_injection_flagged_not_followed(sam):
    n = len(appmod.svc.ws["harborline"].log.events)
    up = _pdf(sam, "harborline_invoice_suspicious.pdf").json()
    assert len(up["unsafe_content"]) == 2
    new = appmod.svc.ws["harborline"].log.events[n:]
    assert any("unsafe_content_flagged" in e["action"] for e in new)
    assert not any(e["action"] in ("bank.payment", "ledger.post") for e in new)


def test_scanned_pdf_refused(sam):
    r = _pdf(sam, "harborline_scanned_receipt.pdf")
    assert r.status_code == 422 and "scanned" in r.json()["detail"]


def test_trace_shows_source_to_decision_with_evidence(sam):
    run = golden_run(sam)
    exc = next(x for x in run["result"]["exceptions"] if x["bank_id"] == "HB005")
    sam.post(f"/v1/workspaces/harborline/runs/{run['id']}/exceptions/{exc['id']}/decisions", headers=H,
             json={"decision": "ACCEPT", "note": "short pay"})
    t = sam.get("/v1/workspaces/harborline/trace/HB005").json()
    stages = [s["stage"] for s in t["steps"]]
    assert stages[:2] == ["Source", "Checked & imported"] and "Matched" in stages and "Decided" in stages
    assert t["steps"][0]["proof"].endswith(t["line"]["source_sha256"])
    assert any(e["action"] == "exception.accept" for e in t["events"]) and t["chain"]["state"] == "PASS"
    assert {r["id"] for r in t["related"]} >= {"GL-H-104", "INV-1005"}
    idx = sam.get("/v1/workspaces/harborline/trace").json()
    assert len(idx["lines"]) == 12


def test_trace_other_client_concealed(alex, sam):
    golden_run(sam, "bayview")
    assert alex.get("/v1/workspaces/bayview/trace/BV001").status_code == 404


def test_counterparty_view_links_all_records_with_evidence(sam):
    golden_run(sam)
    d = sam.get("/v1/workspaces/harborline/parties/P-PELICAN").json()
    ids = {l["line_id"]: l for l in d["links"]}
    assert {"HB005", "GL-H-104", "INV-1005"} <= set(ids)
    assert ids["HB005"]["state"] == "LINKED" and any("INV-1005" in s for s in ids["HB005"]["signals"])
    assert d["residual_cents"] == 30000
    idx = sam.get("/v1/workspaces/harborline/parties").json()
    assert [l["line_id"] for l in idx["unlinked"]] == ["HB010"]  # mobile deposit: never guessed


def test_counterparty_confirmation_is_signed(sam):
    golden_run(sam)
    r = sam.post("/v1/workspaces/harborline/parties/P-CORAL/links/HB010", headers=H,
                 json={"decision": "CONFIRM", "note": "client emailed deposit slip"})
    assert r.status_code == 200
    d = sam.get("/v1/workspaces/harborline/parties/P-CORAL").json()
    assert next(l for l in d["links"] if l["line_id"] == "HB010")["state"] == "CONFIRMED"
    ev = next(e for e in reversed(appmod.svc.ws["harborline"].log.events) if e["action"] == "party.confirm")
    assert ev["actor_id"] == "samantha@avocet.test" and ev["source_line_refs"] == ["HB010", "P-CORAL"]
    assert sam.get("/v1/workspaces/harborline/evidence").json()["verification"]["state"] == "PASS"


def test_counterparty_one_row_per_transaction(sam):
    golden_run(sam)
    d = sam.get("/v1/workspaces/harborline/parties/P-PELICAN").json()
    assert len(d["transactions"]) == 1
    t = d["transactions"][0]
    assert t["line_id"] == "HB005" and {x["line_id"] for x in t["attached"]} == {"GL-H-104", "INV-1005"}


def test_clear_one_source_withdraws_runs_and_is_recorded(sam):
    golden_run(sam)
    r = sam.post("/v1/workspaces/harborline/sources/bank/clear", headers=H).json()
    assert r["uploads_removed"] >= 1 and r["runs_withdrawn"] >= 1
    w = sam.get("/v1/workspaces/harborline").json()
    assert not w["status"]["bank"] and w["status"]["ledger"] and w["runs"] == []
    assert any(e["action"] == "source.cleared:bank" for e in appmod.svc.ws["harborline"].log.events)
    blob = (FIXTURES / "harborline_bank.csv").read_bytes()
    up = sam.post("/v1/workspaces/harborline/uploads", headers=H, data={"kind": "bank"},
                  files={"file": ("harborline_bank.csv", blob, "text/csv")}).json()
    assert up["duplicate_of"] is None  # can be imported again after clearing


def test_bank_download_qbo_and_tsv_ledger_reconcile_like_csv(sam):
    sam.post("/v1/workspaces/harborline/reset", headers=H)
    for kind, name in (("bank", "harborline_bank.qbo"), ("ledger", "harborline_ledger.tsv")):
        up = sam.post("/v1/workspaces/harborline/uploads", headers=H, data={"kind": kind},
                      files={"file": (name, (FIXTURES / name).read_bytes())}).json()
        assert up["proposed_mapping"]["currency"], up
        r = sam.post(f"/v1/workspaces/harborline/uploads/{up['id']}/mapping", headers=H, json={"mapping": up["proposed_mapping"]}).json()
        assert r["health"]["rejected"] == []
    run = sam.post("/v1/workspaces/harborline/reconciliation-runs", headers=H).json()
    assert run["result"]["variance_cents"] == EXP["harborline"]["variance_cents"]
    assert len(run["result"]["matches"]) == 9


@pytest.mark.parametrize("name", ["deck.pptx", "letter.docx"])
def test_office_documents_refused(sam, name):
    r = sam.post("/v1/workspaces/harborline/uploads", headers=H, data={"kind": "bank"}, files={"file": (name, b"PK\x03\x04x")})
    assert r.status_code == 422


def test_email_attachments_extracted_and_logged(sam):
    blob = (FIXTURES / "harborline_email_from_client.eml").read_bytes()
    r = sam.post("/v1/workspaces/harborline/email", headers=H, files={"file": ("mail.eml", blob)}).json()
    assert {a["name"] for a in r["attachments"]} == {"harborline_bank.csv", "harborline_receipt_OD-7741.pdf"}
    assert "October bank statement" in r["subject"]
    assert any(e["action"].startswith("email.opened") for e in appmod.svc.ws["harborline"].log.events)


def test_not_an_email_refused(sam):
    r = sam.post("/v1/workspaces/harborline/email", headers=H, files={"file": ("x.eml", b"hello")})
    assert r.status_code == 422


def test_drive_off_unless_configured(sam):
    assert sam.get("/v1/integrations").json()["google_drive"]["enabled"] is False
