"""Hosted durability (2026-11-16): the data folder is restored from Cloud Storage at start-up and every change is
synced back, like TrustOps' state in GCS. A fake bucket stands in for Cloud Storage."""
import json
import os

import pytest

from ledgersight import cloud_sync, service
from ledgersight.core.store import Store


class FakeBucket:
    """In-memory bucket with object generations and if_generation_match, like Cloud Storage."""

    def __init__(self):
        self.objects: dict[str, tuple[bytes, int]] = {}
        self.counter = 0

    def _put(self, name, data):
        self.counter += 1
        self.objects[name] = (data, self.counter)
        return self.counter

    def list(self, prefix):
        return {n: g for n, (_, g) in self.objects.items() if n.startswith(prefix)}

    def download(self, name, path):
        path.write_bytes(self.objects[name][0])

    def upload_file(self, name, path, if_generation=None):
        if if_generation is not None and self.objects.get(name, (None, 0))[1] != if_generation:
            raise cloud_sync.Conflict(name)
        return self._put(name, path.read_bytes())

    def get_bytes(self, name):
        return self.objects[name][0] if name in self.objects else None

    def put_bytes(self, name, data):
        self._put(name, data)

    def generation(self, name):
        return self.objects.get(name, (None, 0))[1]

    def delete(self, name):
        self.objects.pop(name, None)


URI = "gs://test-bucket/ledgersight"


def boot(tmp, bucket, name):
    """One 'container': restore an (empty) data folder, open the database, attach the sync."""
    s = cloud_sync.CloudSync(tmp, URI, backend=bucket, instance=name)
    s.restore()
    st = Store(tmp / cloud_sync.DB_NAME)
    st.on_change = s.mark_dirty
    s.store = st          # attach without the background thread: the tests flush explicitly
    return s, st


def test_restart_restores_database_and_files(tmp_path):
    bucket = FakeBucket()
    a, sa = boot(tmp_path / "a", bucket, "rev-1")
    sa.put("users", [{"user_id": "u1"}])
    (tmp_path / "a" / "firm-01" / "client-a").mkdir(parents=True)
    (tmp_path / "a" / "firm-01" / "client-a" / "UP-1.csv").write_text("date,amount\n")
    (tmp_path / "a" / "signing-key.pem").write_text("KEY")
    assert a.sync_now()
    assert "ledgersight/db/ledgersight.db" in bucket.objects
    assert "ledgersight/files/firm-01/client-a/UP-1.csv" in bucket.objects

    b, sb = boot(tmp_path / "b", bucket, "rev-2")          # a new revision on an empty disk
    assert b.status["restored"] == 3
    assert sb.get("users") == [{"user_id": "u1"}]
    assert (tmp_path / "b" / "firm-01" / "client-a" / "UP-1.csv").read_text() == "date,amount\n"
    assert (tmp_path / "b" / "signing-key.pem").read_text() == "KEY"


def test_deleted_files_leave_the_bucket(tmp_path):
    bucket = FakeBucket()
    a, sa = boot(tmp_path / "a", bucket, "rev-1")
    f = tmp_path / "a" / "firm-01" / "client-a" / "UP-1.csv"
    f.parent.mkdir(parents=True)
    f.write_text("x")
    a.sync_now()
    f.unlink()
    sa.put("k", 1)
    a.sync_now()
    assert not any("UP-1.csv" in n for n in bucket.objects)


def test_older_revision_stops_writing_once_a_newer_one_owns_the_data(tmp_path):
    bucket = FakeBucket()
    old, so = boot(tmp_path / "old", bucket, "rev-1")
    so.put("k", "old")
    old.sync_now()
    new, sn = boot(tmp_path / "new", bucket, "rev-2")      # rollout: the new revision restores and takes over
    gen = bucket.generation("ledgersight/db/ledgersight.db")
    so.put("k", "late write from the old revision")
    assert old.sync_now() is False and "rev-2" in old.status["stopped"]
    assert bucket.generation("ledgersight/db/ledgersight.db") == gen      # nothing overwritten
    assert not old.healthy()
    sn.put("k", "new")
    assert new.sync_now() and new.healthy()


def test_owner_keeps_its_copy_when_the_database_object_changed_underneath(tmp_path):
    bucket = FakeBucket()
    a, sa = boot(tmp_path / "a", bucket, "rev-1")
    sa.put("k", 1)
    a.sync_now()
    bucket._put("ledgersight/db/ledgersight.db", b"written by someone else")   # e.g. an old revision mid-rollout
    sa.put("k", 2)
    assert a.sync_now()
    c, sc = boot(tmp_path / "c", bucket, "rev-3")
    assert sc.get("k") == 2


def test_existing_local_database_is_kept_not_overwritten(tmp_path):
    bucket = FakeBucket()
    a, sa = boot(tmp_path / "a", bucket, "rev-1")
    sa.put("k", "bucket copy")
    a.sync_now()
    local = Store(tmp_path / "laptop" / cloud_sync.DB_NAME)
    local.put("k", "local copy")
    b, sb = boot(tmp_path / "laptop", bucket, "rev-2")
    assert sb.get("k") == "local copy" and b.status["restored"] == 0


def test_bad_uri_is_refused(tmp_path):
    with pytest.raises(ValueError):
        cloud_sync.CloudSync(tmp_path, "s3://x/y", backend=FakeBucket())


def test_service_restarts_with_its_books_and_a_verifying_audit_trail(tmp_path, monkeypatch):
    """The whole service: books, evidence chain and signing key survive a 'redeploy' onto an empty disk."""
    bucket = FakeBucket()
    monkeypatch.setattr(cloud_sync, "GcsBackend", lambda name: bucket)
    monkeypatch.setenv("LEDGERSIGHT_STATE_GCS_URI", URI)
    monkeypatch.delenv("AVOCET_SIGNING_KEY_PEM", raising=False)
    monkeypatch.setattr(cloud_sync, "SWEEP", 3600)

    monkeypatch.setattr(service, "DATA_DIR", tmp_path / "one")
    s1 = service.Service()
    wid = next(iter(s1.ws))
    w = s1.ws[wid]
    w.log.append(tenant_id=service.TENANT, workspace_id=wid, actor_type="human", actor_id="tester",
                 action="test.event", policy_decision="ALLOW:OK")
    s1.save(wid)
    s1.sync.flush()
    s1.sync.stop()
    assert s1.ready()["durable_storage"] is True

    monkeypatch.setattr(service, "DATA_DIR", tmp_path / "two")          # new container, empty disk
    s2 = service.Service()
    try:
        assert any(e.get("action") == "test.event" for e in s2.ws[wid].log.events)
        r = s2.ready()
        assert r["ready"] and r["checks"]["audit_chain_matches_key"] is True
        assert r["durable_storage"] is True and r["cloud_storage"]["restored_objects"] >= 2
        assert json.loads(bucket.get_bytes("ledgersight/owner.json"))["owner"] == s2.sync.instance
    finally:
        s2.sync.stop()
        cloud_sync._active = None


def test_without_the_setting_nothing_changes(tmp_path, monkeypatch):
    monkeypatch.delenv("LEDGERSIGHT_STATE_GCS_URI", raising=False)
    monkeypatch.setattr(cloud_sync, "_active", None)
    monkeypatch.setattr(service, "DATA_DIR", tmp_path)
    s = service.Service()
    assert s.sync is None and "cloud_storage" not in s.ready()
    assert os.path.exists(tmp_path / "ledgersight.db")
