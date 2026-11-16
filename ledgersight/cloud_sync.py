"""Durable hosted storage in Google Cloud Storage (2026-11-16), the same approach as TrustOps.

On Cloud Run the data folder (AVOCET_DATA_DIR) is temporary: it is wiped on every deploy and whenever Google
restarts the instance. With LEDGERSIGHT_STATE_GCS_URI=gs://<bucket>/<prefix> set:

- start-up: an empty data folder is restored from the bucket (database snapshot + uploaded files), then this
  instance writes an owner record, so an older revision still running during a rollout stops writing;
- after every change (the store calls `mark_dirty`), a background thread waits a moment for more changes, then
  uploads a consistent database snapshot (SQLite backup API) and mirrors the uploaded files (new and deleted);
- the database object is written with a generation check (optimistic locking, like TrustOps' state.json), so two
  writers can never silently overwrite each other;
- on shutdown (Cloud Run sends SIGTERM) the last changes are flushed before the process exits.

The bucket should be private, versioned and in the declared region. Without the setting nothing changes.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path

DB_NAME = "ledgersight.db"
SKIP = {DB_NAME, DB_NAME + "-wal", DB_NAME + "-shm", DB_NAME + "-journal"}
DEBOUNCE = float(os.environ.get("LEDGERSIGHT_SYNC_DEBOUNCE", "1.0"))
SWEEP = float(os.environ.get("LEDGERSIGHT_SYNC_SWEEP", "30"))


def uri() -> str:
    return os.environ.get("LEDGERSIGHT_STATE_GCS_URI", "").strip()


class Conflict(Exception):
    """The database object changed since this instance last wrote it."""


class GcsBackend:
    """The few Cloud Storage operations the sync needs (google-cloud-storage)."""

    def __init__(self, bucket: str):
        from google.cloud import storage  # imported only when hosted sync is configured
        self.bucket = storage.Client().bucket(bucket)

    def list(self, prefix: str) -> dict[str, int]:
        return {b.name: b.generation for b in self.bucket.list_blobs(prefix=prefix)}

    def download(self, name: str, path: Path) -> None:
        self.bucket.blob(name).download_to_filename(str(path))

    def upload_file(self, name: str, path: Path, if_generation: int | None = None) -> int:
        from google.api_core.exceptions import PreconditionFailed
        blob = self.bucket.blob(name)
        try:
            blob.upload_from_filename(str(path), **({} if if_generation is None else {"if_generation_match": if_generation}))
        except PreconditionFailed:
            raise Conflict(name) from None
        return blob.generation

    def get_bytes(self, name: str) -> bytes | None:
        blob = self.bucket.blob(name)
        return blob.download_as_bytes() if blob.exists() else None

    def put_bytes(self, name: str, data: bytes) -> None:
        self.bucket.blob(name).upload_from_string(data, content_type="application/json")

    def generation(self, name: str) -> int:
        blob = self.bucket.get_blob(name)
        return blob.generation if blob else 0

    def delete(self, name: str) -> None:
        try:
            self.bucket.blob(name).delete()
        except Exception:
            pass


class CloudSync:
    def __init__(self, data_dir: Path, gcs_uri: str, backend=None, instance: str | None = None):
        if not gcs_uri.startswith("gs://"):
            raise ValueError("LEDGERSIGHT_STATE_GCS_URI must look like gs://bucket/prefix")
        bucket, _, prefix = gcs_uri[5:].partition("/")
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
        self.backend = backend or GcsBackend(bucket)
        self.data_dir = Path(data_dir)
        self.instance = instance or f"{os.environ.get('K_REVISION', 'local')}:{uuid.uuid4().hex[:8]}"
        self.db_gen = 0            # generation of the database object this instance last read or wrote
        self.db_digest = None      # SHA-256 of the last snapshot uploaded (skip unchanged uploads)
        self.files: dict[str, tuple[int, int]] = {}   # relative path -> (size, mtime_ns) known to be in the bucket
        self.store = None
        self.status = {"enabled": True, "restored": None, "last_sync": None, "last_error": None, "stopped": None}
        self._dirty = threading.Event()
        self._lock = threading.Lock()
        self._stop = False
        self._thread: threading.Thread | None = None

    # ---- names ----
    def _db_obj(self) -> str:
        return self.prefix + "db/" + DB_NAME

    def _file_obj(self, rel: str) -> str:
        return self.prefix + "files/" + rel

    def _owner_obj(self) -> str:
        return self.prefix + "owner.json"

    # ---- start-up ----
    def restore(self) -> dict:
        """Fill an empty data folder from the bucket, then take ownership. A folder that already has a database
        (a laptop, or a restart that kept the disk) is kept and becomes the copy that is synced."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        objs = self.backend.list(self.prefix)
        restored = 0
        local_db = self.data_dir / DB_NAME
        if not local_db.exists() and self._db_obj() in objs:
            self.backend.download(self._db_obj(), local_db)
            restored += 1
            for name in objs:
                if name.startswith(self.prefix + "files/"):
                    rel = name[len(self.prefix + "files/"):]
                    dest = self.data_dir / rel
                    if ".." in Path(rel).parts:
                        continue
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    self.backend.download(name, dest)
                    if rel == "signing-key.pem":
                        os.chmod(dest, 0o600)
                    restored += 1
        self.db_gen = objs.get(self._db_obj(), 0)
        # What is in the bucket now counts as uploaded, so it is not sent again and deletions are mirrored.
        for name in objs:
            if name.startswith(self.prefix + "files/"):
                rel = name[len(self.prefix + "files/"):]
                p = self.data_dir / rel
                if p.exists():
                    st = p.stat()
                    self.files[rel] = (st.st_size, st.st_mtime_ns)
                else:
                    self.files[rel] = (-1, -1)   # in the bucket but not here: deleted at the next sync
        self.backend.put_bytes(self._owner_obj(), json.dumps({"owner": self.instance, "at": time.time()}).encode())
        self.status["restored"] = restored
        print(f"[ledgersight] storage: {restored} object(s) restored from {self.prefix or '/'} (db generation {self.db_gen}); "
              f"owner {self.instance}", flush=True)
        return self.status

    def attach(self, store) -> None:
        """Called once the database is open: changes are synced from now on, and once at start."""
        self.store = store
        self._thread = threading.Thread(target=self._loop, name="ledgersight-cloud-sync", daemon=True)
        self._thread.start()
        atexit.register(self.flush)
        self.mark_dirty()

    # ---- changes ----
    def mark_dirty(self) -> None:
        self._dirty.set()

    def _loop(self) -> None:
        while not self._stop:
            fired = self._dirty.wait(timeout=SWEEP)
            if self._stop:
                return
            if fired:
                time.sleep(DEBOUNCE)          # let a burst of writes finish, then upload once
            self._dirty.clear()
            try:
                self.sync_now()
            except Exception as e:            # keep running; the next change retries
                self.status["last_error"] = f"{type(e).__name__}: {e}"[:300]
                print(f"[ledgersight] storage sync failed: {self.status['last_error']}", flush=True)

    def flush(self) -> None:
        """Upload any pending change now (shutdown, tests)."""
        try:
            self.sync_now()
        except Exception as e:
            self.status["last_error"] = f"{type(e).__name__}: {e}"[:300]
            print(f"[ledgersight] storage flush failed: {self.status['last_error']}", flush=True)

    def _owner(self) -> str | None:
        raw = self.backend.get_bytes(self._owner_obj())
        try:
            return json.loads(raw or b"{}").get("owner")
        except ValueError:
            return None

    def sync_now(self) -> bool:
        if self.store is None or self.status["stopped"]:
            return False
        with self._lock:
            owner = self._owner()
            if owner and owner != self.instance:
                # A newer revision has started and restored the data: this instance must not write any more.
                self.status["stopped"] = f"newer instance {owner} owns the data"
                print(f"[ledgersight] storage: {self.status['stopped']}; this instance stops writing", flush=True)
                return False
            self._sync_files()
            with tempfile.TemporaryDirectory() as tmp:
                snap = self.store.backup(Path(tmp) / DB_NAME)
                digest = hashlib.sha256(snap.read_bytes()).hexdigest()
                if digest == self.db_digest and self.db_gen:
                    # Nothing changed since the last upload (the periodic check): no new object version.
                    self.status.update(last_sync=time.time(), last_error=None)
                    return True
                try:
                    self.db_gen = self.backend.upload_file(self._db_obj(), snap, if_generation=self.db_gen)
                except Conflict:
                    # The owner record says this instance owns the data, so the other write came from an older
                    # revision during a rollout. Keep this instance's data, and say so in the log.
                    current = self.backend.generation(self._db_obj())
                    print(f"[ledgersight] storage: database changed by another instance (generation {self.db_gen} -> "
                          f"{current}); this owner's copy is kept", flush=True)
                    self.db_gen = self.backend.upload_file(self._db_obj(), snap, if_generation=current)
                self.db_digest = digest
            self.status.update(last_sync=time.time(), last_error=None)
            return True

    def _sync_files(self) -> None:
        seen = set()
        for p in self.data_dir.rglob("*"):
            if not p.is_file() or p.name in SKIP or p.parent == self.data_dir and p.name.startswith(DB_NAME):
                continue
            rel = p.relative_to(self.data_dir).as_posix()
            seen.add(rel)
            st = p.stat()
            if self.files.get(rel) != (st.st_size, st.st_mtime_ns):
                self.backend.upload_file(self._file_obj(rel), p)
                self.files[rel] = (st.st_size, st.st_mtime_ns)
        for rel in [r for r in self.files if r not in seen]:   # cleared books and replaced files go from the bucket too
            self.backend.delete(self._file_obj(rel))
            del self.files[rel]

    def healthy(self) -> bool:
        return not self.status["stopped"] and self.status["last_error"] is None

    def stop(self) -> None:
        self._stop = True
        self._dirty.set()


_active: CloudSync | None = None


def start(data_dir: Path) -> CloudSync | None:
    """Restore the data folder from the bucket when LEDGERSIGHT_STATE_GCS_URI is set (call before opening the db)."""
    global _active
    if not uri():
        return None
    _active = CloudSync(data_dir, uri())
    _active.restore()
    return _active


def active() -> CloudSync | None:
    return _active


def mark_dirty() -> None:
    if _active is not None:
        _active.mark_dirty()
