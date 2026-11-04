"""Production storage: a restart keeps every client book, decision and signed audit record."""
from test_ledgersight import H, appmod, golden_run, make_user  # noqa: F401

from ledgersight import service
from ledgersight.core import evidence

WS = "/v1/workspaces/client-a"


def _restart():
    """A brand-new Service reading the same data directory, as after a server restart."""
    return service.Service()


def test_restart_keeps_runs_decisions_and_evidence():
    c = make_user("persist@test", "owner_reviewer", ["client-a", "client-b"])
    run = golden_run(c)
    exc = next(x for x in run["result"]["exceptions"] if x["bank_id"] == "HB005")
    c.post(f"{WS}/runs/{run['id']}/exceptions/{exc['id']}/decisions", headers=H, json={"decision": "ACCEPT", "note": "ok"})
    c.post(WS + "/profile", headers=H, json={"name": "Renamed Client"})
    before = appmod.svc.ws["client-a"]
    n_events = len(before.log.events)

    after = _restart().ws["client-a"]
    assert after.meta["name"] == "Renamed Client"
    r = after.runs[run["id"]]
    assert next(x for x in r["result"]["exceptions"] if x["id"] == exc["id"])["status"] == "ACCEPTED"
    assert len(after.log.events) == n_events
    assert evidence.verify(after.log.events, appmod.svc.public_key_pem())["state"] == "PASS"
    assert set(after.blobs) == set(before.blobs)  # uploaded files reloaded from disk
    c.post(WS + "/profile", headers=H, json={"name": "Client 1"})


def test_restart_keeps_signing_key_and_matching_still_works():
    c = make_user("persist2@test", "owner_reviewer", ["client-a"])
    run = golden_run(c)
    svc2 = _restart()
    assert svc2.public_key_pem() == appmod.svc.public_key_pem() and "key file" in svc2.key_source
    u = appmod.auth.users["persist2@test"]
    again = svc2.run(u, "client-a")  # same inputs after restart -> same run (dates restored correctly)
    assert again["id"] == run["id"]
    svc2.clear_source(u, "client-a", "ar")
    rerun = svc2.run(u, "client-a")  # a genuinely new run on reloaded data
    assert rerun["id"] != run["id"] and len(rerun["result"]["matches"]) == 9


def test_clearing_a_client_archives_its_audit_trail():
    c = make_user("persist3@test", "owner_reviewer", ["client-a"])
    golden_run(c)
    old = len(appmod.svc.ws["client-a"].log.events)
    assert c.post(WS + "/reset", headers=H).status_code == 200
    archives = appmod.svc.store.archived_logs("client-a")
    assert archives and len(appmod.svc.store.load_events(archives[-1])) >= old
    assert evidence.verify(appmod.svc.store.load_events(archives[-1]), appmod.svc.public_key_pem())["state"] == "PASS"
    assert any("archived_as:" in r for r in appmod.svc.ws["client-a"].log.events[0]["source_line_refs"])


def test_user_directory_is_saved():
    make_user("persist4@test", "preparer", ["client-a"])
    appmod.save_users()
    saved = {u["user_id"]: u for u in appmod.svc.store.get("users")}
    assert saved["persist4@test"]["role"] == "preparer" and saved["persist4@test"]["pw_hash"]
    appmod.auth.users.pop("persist4@test")
    appmod.load_users()
    assert appmod.auth.users["persist4@test"].workspaces == {"client-a"}


def test_readiness_reports_storage_and_signer():
    from fastapi.testclient import TestClient
    r = TestClient(appmod.app).get("/readyz").json()
    assert r["ready"] is True and r["checks"]["storage"] is True and r["persistence"] == "sqlite"
    assert TestClient(appmod.app).get("/healthz").json()["version"] == appmod.APP_VERSION
    assert TestClient(appmod.app).get("/healthz").headers["x-request-id"]
