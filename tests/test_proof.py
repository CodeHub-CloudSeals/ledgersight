"""Proof attached to a review item (2026-11-12): both the staff preparer and the CPA reviewer can add a receipt,
bill, statement extract or email to support the decision. Stored unopened, fingerprinted, signed; only before the
decision; downloads match their fingerprint; the other person is notified; kept when a re-run keeps the case.
"""
import hashlib

from test_ledgersight import FIXTURES, H, appmod, golden_run, make_user  # noqa: F401

import pytest

WS = "/v1/workspaces/client-a"
PDF = b"%PDF-1.4\n% synthetic receipt for the demo\n1 0 obj <<>> endobj\ntrailer <<>>\n%%EOF\n"


@pytest.fixture(scope="module")
def cpa():
    return make_user("proof-cpa@demo.test", "owner_reviewer", ["client-a"])


@pytest.fixture(scope="module")
def prep():
    return make_user("proof-prep@demo.test", "preparer", ["client-a"])


def _attach(c, run_id, exc_id, name, blob, note=""):
    return c.post(f"{WS}/runs/{run_id}/exceptions/{exc_id}/attachments", headers=H, data={"note": note}, files={"file": (name, blob)})


def test_preparer_and_reviewer_can_attach_proof_and_it_is_signed(cpa, prep):
    run = golden_run(cpa)
    x = run["result"]["exceptions"][0]
    log = appmod.svc.ws["client-a"].log
    r = _attach(prep, run["id"], x["id"], "receipt_OD-7741.pdf", PDF, "Receipt from the vendor")
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["id"].startswith("PF-") and a["sha256"] == hashlib.sha256(PDF).hexdigest() and a["by"] == "proof-prep@demo.test"
    assert a["note"] == "Receipt from the vendor" and a["size"] == len(PDF)
    ev = log.events[-1]
    assert ev["action"] == "exception.attachment_added" and ev["approval_id"] == a["id"] and ev["source_batch_sha256"] == a["sha256"]
    assert ev["actor_id"] == "proof-prep@demo.test" and ev["policy_decision"].startswith("ALLOW")
    # the CPA reviewer can attach too
    r2 = _attach(cpa, run["id"], x["id"], "bank_extract.csv", b"date,amount\n2026-10-09,1000.00\n")
    assert r2.status_code == 200, r2.text
    # both are on the case, in order, and both people see them
    case = next(e for e in cpa.get(f"{WS}/runs/{run['id']}").json()["result"]["exceptions"] if e["id"] == x["id"])
    assert [p["filename"] for p in case["attachments"]] == ["receipt_OD-7741.pdf", "bank_extract.csv"]
    # download returns the exact bytes, as a download (never rendered in the page)
    d = prep.get(f"{WS}/runs/{run['id']}/exceptions/{x['id']}/attachments/{a['id']}")
    assert d.status_code == 200 and d.content == PDF and d.headers["content-type"] == "application/pdf"
    assert d.headers["content-disposition"].startswith("attachment;") and d.headers["x-content-type-options"] == "nosniff"
    # the other person is notified, with the file name
    n = cpa.get(WS + "/notifications").json()
    assert any(i["kind"] == "ATTACHED" and i["ref"] == a["id"] and i["note"] == "receipt_OD-7741.pdf" for i in n["items"])
    p = prep.get(WS + "/notifications").json()
    assert any(i["kind"] == "ATTACHED" and i["by"] == "proof-cpa@demo.test" for i in p["items"])
    assert not any(i["kind"] == "ATTACHED" and i["by"] == "proof-prep@demo.test" for i in p["items"])   # not your own


def test_bad_or_late_proof_is_refused(cpa, prep):
    run = golden_run(cpa)
    x, y = run["result"]["exceptions"][0], run["result"]["exceptions"][1]
    url = lambda e: f"{WS}/runs/{run['id']}/exceptions/{e['id']}/attachments"
    assert _attach(prep, run["id"], x["id"], "macro.xlsm", b"PK\x03\x04junk").status_code == 422       # type not accepted
    r = _attach(prep, run["id"], x["id"], "fake.pdf", b"MZ\x90\x00 not a pdf")                         # disguised file
    assert r.status_code == 422 and "not a real PDF" in r.text
    assert _attach(prep, run["id"], x["id"], "empty.pdf", b"").status_code == 422
    assert _attach(prep, run["id"], x["id"], "big.pdf", b"%PDF" + b"0" * (10 * 1024 * 1024)).status_code in (413, 422)
    assert _attach(prep, run["id"], x["id"], "r.pdf", PDF).status_code == 200
    assert _attach(prep, run["id"], x["id"], "same_again.pdf", PDF).status_code == 422                   # exact duplicate
    assert _attach(prep, run["id"], "EX-999", "r.pdf", PDF).status_code == 404
    # after the decision the record is final
    assert cpa.post(f"{WS}/runs/{run['id']}/exceptions/{y['id']}/decisions", headers=H, json={"decision": "ACCEPT", "note": "ok"}).status_code == 200
    r = _attach(prep, run["id"], y["id"], "late.pdf", PDF)
    assert r.status_code == 422 and "before the decision" in r.text
    # another client's people cannot attach or download (concealed)
    other = make_user("proof-other@demo.test", "owner_reviewer", ["client-b"])
    assert other.post(url(x), headers=H, files={"file": ("r.pdf", PDF)}).status_code == 404
    att = next(e for e in cpa.get(f"{WS}/runs/{run['id']}").json()["result"]["exceptions"] if e["id"] == x["id"])["attachments"][0]
    assert other.get(f"{url(x)}/{att['id']}").status_code == 404


def test_proof_is_kept_when_a_rerun_keeps_the_case(cpa, prep):
    run = golden_run(cpa)
    x = run["result"]["exceptions"][0]
    assert _attach(prep, run["id"], x["id"], "receipt.pdf", PDF).status_code == 200
    # change an input that does not touch this case (a revised documents table), then reconcile again
    csv = (FIXTURES / "client-a_documents.csv").read_text() + "DOC-H-09,lease,LEASE-NOV,VEND-LANDLORD,1200.00,lease_nov.pdf\n"
    up =cpa.post(WS + "/uploads", headers=H, data={"kind": "documents"}, files={"file": ("docs_v2.csv", csv.encode())}).json()
    assert cpa.post(f"{WS}/uploads/{up['id']}/mapping", headers=H, json={"mapping": up["proposed_mapping"]}).status_code == 200
    run2 = cpa.post(WS + "/reconciliation-runs", headers=H).json()
    assert run2["id"] != run["id"]
    same = next(e for e in run2["result"]["exceptions"] if e.get("case_id") == x.get("case_id") and e["type"] == x["type"])
    assert [a["filename"] for a in same.get("attachments") or []] == ["receipt.pdf"] and same.get("carried_from") == run["id"]
    # the carried proof still downloads from the new run
    a = same["attachments"][0]
    assert prep.get(f"{WS}/runs/{run2['id']}/exceptions/{same['id']}/attachments/{a['id']}").content == PDF


def test_policy_lets_both_roles_attach():
    from ledgersight.core import policy
    assert "exception.attach" in policy.ROLE_ACTIONS["owner_reviewer"] and "exception.attach" in policy.ROLE_ACTIONS["preparer"]
