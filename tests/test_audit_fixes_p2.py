"""Audit batch D (P2): platform regressions (finding ids in test names)."""
import sqlite3
import threading
from pathlib import Path

import pytest

from test_ledgersight import H, appmod, golden_run, make_user  # noqa: F401
from ledgersight.core.store import Store, SCHEMA_VERSION

ROOT = Path(__file__).resolve().parent.parent
WS = "/v1/workspaces/client-a"


@pytest.fixture(scope="module")
def owner():
    return make_user("p2-owner@demo.test", "owner_reviewer", ["client-a"])


def test_prod02_reads_do_not_rewrite_the_book(owner, monkeypatch):
    golden_run(owner)
    calls = []
    real = appmod.svc.store.save_workspace
    monkeypatch.setattr(appmod.svc.store, "save_workspace", lambda wid, st: (calls.append(wid), real(wid, st)))
    for path in ("/agents", "/sources", ""):
        assert owner.get(WS + path).status_code == 200
    assert calls == []  # GETs only append their audit events
    owner.post(WS + "/profile", headers=H, json={"name": "Client 1", "period": "October 2026"})
    assert calls == ["client-a"]


def test_prod04_concurrent_saves_keep_the_chain_whole(owner):
    golden_run(owner)
    svc, w = appmod.svc, appmod.svc.ws["client-a"]
    errors = []

    def worker():
        try:
            for _ in range(20):
                w.log.append(tenant_id="t", workspace_id="client-a", actor_type="human", actor_id="x",
                             action="workspace.read", policy_decision="ALLOW:OK")
                svc.save("client-a", False)
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    ts = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    svc.save("client-a", False)
    assert errors == []
    stored = svc.store.load_events("client-a")
    assert len(stored) == len(w.log.events) and stored[-1]["event_hash"] == w.log.events[-1]["event_hash"]


def test_prod04_failed_persistence_is_not_a_silent_200(owner, monkeypatch):
    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(appmod.svc.store, "save_workspace", boom)
    r = owner.post(WS + "/profile", headers=H, json={"name": "Client 1", "period": "October 2026"})
    assert r.status_code == 503


def test_prod05_evidence_paged_with_totals_and_cached_verification(owner):
    golden_run(owner)
    full = owner.get(WS + "/evidence").json()
    page = owner.get(WS + "/evidence?limit=10").json()
    assert len(page["events"]) == 10 and page["total"] >= len(full["events"])
    assert page["decision_counts"]["ALLOW"] > 0 and page["verification"]["state"] == "PASS"
    assert owner.get(WS + "/evidence?limit=0").status_code == 422


def test_sec12_oversized_request_refused_before_reading(owner):
    r = owner.post(WS + "/uploads", headers={**H, "content-length": str(60 * 1024 * 1024)}, content=b"x")
    assert r.status_code == 413


def test_prod01_storage_wording_on_cloud_run(owner, monkeypatch):
    monkeypatch.setenv("K_SERVICE", "ledgersight")
    monkeypatch.delenv("AVOCET_STORAGE_DURABLE", raising=False)
    t = owner.get("/v1/trust").json()
    assert "temporary disk" in t["storage"] and "redeployed" in t["storage"]
    assert appmod.svc.ready()["durable_storage"] is False
    monkeypatch.setenv("AVOCET_STORAGE_DURABLE", "1")
    assert "durable storage" in owner.get("/v1/trust").json()["storage"]


def test_prod06_schema_version_checked_and_backup(tmp_path):
    db = tmp_path / "x.db"
    s = Store(db)
    s.put("k", {"a": 1})
    out = s.backup(tmp_path / "bk" / "copy.db")
    assert Store(out).get("k") == {"a": 1}
    con = sqlite3.connect(str(db))
    con.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
    con.commit()
    con.close()
    with pytest.raises(RuntimeError):
        Store(db)
    assert SCHEMA_VERSION == "1"


def test_prod09_static_compressed_and_revalidated(owner):
    r = owner.get("/static/avocet.js", headers={"accept-encoding": "gzip"})
    assert r.headers.get("content-encoding") == "gzip" and r.headers["cache-control"] == "no-cache"
    assert owner.get(WS).headers["cache-control"] == "no-store"


def test_prod08_runtime_requirements_pinned_without_test_tools():
    req = (ROOT / "requirements.txt").read_text()
    lines = [x for x in req.splitlines() if x and not x.startswith("#")]
    assert all("==" in x for x in lines) and not any(x.startswith(("pytest", "httpx")) for x in lines)
    assert "pytest" in (ROOT / "requirements-dev.txt").read_text()


def test_prod05_recent_activity_survives_paging(owner):
    """Regression found during the fix pass: with only the newest N events on screen, real activity
    (uploads, runs, decisions) must still be reported even when many page views came after it."""
    golden_run(owner)
    for _ in range(30):
        owner.get(WS)  # routine page views
    page = owner.get(WS + "/evidence?limit=10").json()
    acts = [e["action"] for e in page["recent_activity"]]
    assert acts and not any(a in ("workspace.read", "export.read", "query.ask") for a in acts)
    assert all(e["action"] in ("workspace.read", "export.read") for e in page["events"][-5:])
