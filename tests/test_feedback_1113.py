"""Review feedback 2026-11-13 (items 3-9).

3 suggested comments on review items · 4 one generic message for refused Google sign-ins · 5 clearer refusal +
sample templates · 6 Sources only links to Reconciliation · 7 same files are not run twice · 8 no run button on
Agents · 9 success messages fade out.
"""
import csv
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_google_login import claims, fake_verify, google_on  # noqa: F401  (fixture)
from test_ledgersight import H, appmod, golden_run, make_user

from ledgersight.core import ingest

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "ledgersight" / "static" / "avocet.js").read_text(encoding="utf-8")
HTML = (ROOT / "ledgersight" / "static" / "index.html").read_text(encoding="utf-8")
WS = "/v1/workspaces/client-a"


@pytest.fixture(scope="module")
def cpa():
    return make_user("fb1113-cpa@demo.test", "owner_reviewer", ["client-a"])


# 4 ---------------------------------------------------------------------------------------------------------------
def test_refused_google_sign_ins_share_one_plain_message(google_on, monkeypatch):  # noqa: F811
    monkeypatch.setattr(appmod.google_id_token, "verify_oauth2_token", fake_verify(claims(email="someone@gmail.com", hd=None)))
    wrong_domain = TestClient(appmod.app).post("/v1/login/google", json={"credential": "t"})
    monkeypatch.setattr(appmod.google_id_token, "verify_oauth2_token", fake_verify(ValueError("bad token")))
    bad_token = TestClient(appmod.app).post("/v1/login/google", json={"credential": "t"})
    assert wrong_domain.status_code == 403 and bad_token.status_code == 401
    assert wrong_domain.json()["detail"] == bad_token.json()["detail"] == appmod.GOOGLE_REFUSED
    assert "403" not in appmod.GOOGLE_REFUSED and "@" not in appmod.GOOGLE_REFUSED


def test_page_never_shows_a_bare_http_code():
    api = JS.split("async function api(path", 1)[1].split("\n}\n", 1)[0]
    assert "`HTTP ${r.status}`" not in api and "You don't have access to this." in api
    google = JS.split("async function onGoogleCredential", 1)[1].split("\n}\n", 1)[0]
    assert "e.message" not in google.split("e.status === 0 ?", 1)[1].split(":", 1)[1]


# 5 ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", appmod.TEMPLATE_KINDS)
def test_each_template_downloads_and_is_read_back_as_its_own_kind(cpa, kind):
    r = cpa.get(f"/v1/templates/{kind}.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert f'filename="ledgersight-template-{kind}.csv"' in r.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(r.text)))
    assert len(rows) >= 2  # header + synthetic example rows
    detected, _ = ingest.detect_kind(rows[0], rows[1:], hint=None)
    assert detected == kind


def test_templates_need_sign_in_and_known_kinds(cpa):
    assert TestClient(appmod.app).get("/v1/templates/bank.csv").status_code == 401
    assert cpa.get("/v1/templates/secrets.csv").status_code == 404


def test_refusal_names_the_columns_and_points_to_the_templates():
    with pytest.raises(ingest.IngestError) as e:
        ingest.detect_kind(["employee", "role", "shift"], [["A", "B", "C"]], hint=None)
    m = str(e.value)
    assert m.startswith("Not accepted") and "Columns found: employee, role, shift" in m and "sample template" in m
    assert 'put("tpl-links", templateLinks(' in JS and "templateLinks(\"Check the expected columns" in JS and 'id="tpl-links"' in HTML


# 6 / 8 -----------------------------------------------------------------------------------------------------------
def test_run_happens_only_on_the_reconciliation_page():
    offer = JS.split("async function offerRun()", 1)[1].split("\n}\n", 1)[0]
    assert 'go("recon")' in offer and "run-btn" not in offer and "Go to Reconciliation →" in offer
    assert 'id="ag-run"' not in HTML and '$("ag-run")' not in JS and "Run reconciliation now" not in HTML


# 7 ---------------------------------------------------------------------------------------------------------------
def test_running_the_same_files_again_returns_the_same_run_flagged_reused(cpa):
    first = golden_run(cpa)
    assert first["reused"] is False
    again = cpa.post(f"{WS}/reconciliation-runs", headers=H)
    assert again.status_code == 200
    a = again.json()
    assert a["id"] == first["id"] and a["reused"] is True
    stored = appmod.svc.ws["client-a"].runs
    assert len(stored) == 1 and "reused" not in stored[first["id"]]  # one run; the flag is never saved into it


def test_page_turns_run_off_when_the_files_were_already_reconciled():
    ready = JS.split("function renderReadiness()", 1)[1].split("\n}\n", 1)[0]
    assert "alreadyRun()" in ready and '$("run-btn").disabled = !!box || !!done' in ready
    assert "if (run.reused) { S.anim = null;" in JS  # a reused run is not replayed as if new


# 3 / 9 -----------------------------------------------------------------------------------------------------------
def test_review_items_offer_suggested_comments_by_role():
    assert "suggestChips(x, note, reviewer), note," in JS
    fn = JS.split("function suggestChips(", 1)[1].split("\n}\n", 1)[0]
    assert "reviewer ? SUGGEST_CPA" in fn and "note.value" in fn and "api(" not in fn  # adds text only, sends nothing


def test_success_messages_fade_but_errors_stay():
    m = JS.split("const msg = (id, text, kind) => {", 1)[1].split("\n};", 1)[0]
    assert 'kind === "good" || kind === "ok"' in m and "8000" in m
