"""Any document type can be uploaded: it is held unopened until JSON conversion is built.

The table importer still refuses unsupported files (see test_office_documents_refused); the
separate held-files intake stores them without ever parsing, rendering or running them."""
import pytest

from test_ledgersight import H, appmod, make_user  # noqa: F401
from ledgersight import service
from ledgersight.core import evidence

WS = "/v1/workspaces/client-a"


@pytest.fixture(scope="module")
def rev():
    return make_user("held-reviewer@test", "owner_reviewer", ["client-a"])


@pytest.mark.parametrize("name,blob", [
    ("letter.docx", b"PK\x03\x04word"),
    ("deck.pptx", b"PK\x03\x04slides"),
    ("macro.xlsm", b"PK\x03\x04vbaProject.bin"),
    ("archive.zip", b"PK\x03\x04zip"),
    ("notes", b"plain text with no extension"),
])
def test_any_document_type_is_held_not_opened(rev, name, blob):
    r = rev.post(WS + "/held-files", headers=H, files={"file": (name, blob)})
    assert r.status_code == 200, r.text
    rec = r.json()
    assert rec["state"] == "HELD_FOR_CONVERSION" and "JSON" in rec["note"]
    assert rec["size"] == len(blob) and len(rec["sha256"]) == 64
    stored = service.DATA_DIR / service.TENANT / "client-a" / f"{rec['id']}.held"
    assert stored.read_bytes() == blob  # kept byte-for-byte under a neutral name, never executed
    w = rev.get(WS, headers=H).json()
    assert any(h["id"] == rec["id"] for h in w["held"])


def test_held_file_is_checked_by_trustops_and_signed(rev):
    n = len(appmod.svc.ws["client-a"].log.events)
    rec = rev.post(WS + "/held-files", headers=H, files={"file": ("memo.docx", b"PK\x03\x04memo")}).json()
    ev = appmod.svc.ws["client-a"].log.events[n:]
    assert any(e["action"] == "upload.create" and e["source_batch_sha256"] == rec["sha256"]
               and "held:.docx" in e["source_line_refs"] and e["policy_decision"].startswith("ALLOW") for e in ev)
    assert evidence.verify(appmod.svc.ws["client-a"].log.events, appmod.svc.public_key_pem())["state"] == "PASS"


def test_held_files_do_not_feed_reconciliation(rev):
    before = rev.get(WS, headers=H).json()["status"]
    rev.post(WS + "/held-files", headers=H, files={"file": ("bank.docx", b"PK\x03\x04x")})
    after = rev.get(WS, headers=H).json()["status"]
    assert after["bank"] == before["bank"] and after["uploads"] == before["uploads"]


def test_duplicate_held_file_is_flagged(rev):
    a = rev.post(WS + "/held-files", headers=H, files={"file": ("same.pptx", b"PK\x03\x04same")}).json()
    b = rev.post(WS + "/held-files", headers=H, files={"file": ("same-again.pptx", b"PK\x03\x04same")}).json()
    assert b["duplicate_of"] == a["id"]


def test_empty_and_oversized_files_refused(rev):
    assert rev.post(WS + "/held-files", headers=H, files={"file": ("empty.docx", b"")}).status_code == 422
    big = b"x" * (10 * 1024 * 1024 + 1)
    assert rev.post(WS + "/held-files", headers=H, files={"file": ("big.docx", big)}).status_code == 422


def test_filename_path_is_stripped(rev):
    rec = rev.post(WS + "/held-files", headers=H, files={"file": ("..\\..\\evil\\name.docx", b"PK\x03\x04p")}).json()
    assert rec["filename"] == "name.docx"


def test_held_files_need_scope(rev):
    other = make_user("held-outsider@test", "preparer", ["client-b"])
    r = other.post(WS + "/held-files", headers=H, files={"file": ("x.docx", b"PK\x03\x04x")})
    assert r.status_code == 404  # scope concealment: does not reveal that client-a exists


def test_held_files_survive_restart(rev):
    rec = rev.post(WS + "/held-files", headers=H, files={"file": ("keep.docx", b"PK\x03\x04keep")}).json()
    assert rec["id"] in service.Service().ws["client-a"].held
