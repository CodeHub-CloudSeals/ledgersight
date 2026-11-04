"""Durable storage (SQLite, standard library).

- workspaces: the current state of each client book (uploads, imported rows, runs, decisions)
- events:     the signed evidence chains, append-only; a cleared book's chain is archived, never deleted
- kv:         small documents such as the user directory

Uploaded files themselves stay on disk next to the database (DATA_DIR/<tenant>/<workspace>/).
One process owns the database; a lock serialises writes from the server's worker threads.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (id TEXT PRIMARY KEY, state TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (log TEXT NOT NULL, seq INTEGER NOT NULL, event TEXT NOT NULL, PRIMARY KEY (log, seq));
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
SCHEMA_VERSION = "1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(SCHEMA)
        self._db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))

    # ---- workspaces ----
    def save_workspace(self, wid: str, state: dict) -> None:
        body = json.dumps(state, default=str, separators=(",", ":"))
        with self._lock:
            self._db.execute("INSERT INTO workspaces (id, state, updated_at) VALUES (?, ?, ?) "
                             "ON CONFLICT(id) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at",
                             (wid, body, _now()))

    def load_workspace(self, wid: str) -> dict | None:
        row = self._db.execute("SELECT state FROM workspaces WHERE id=?", (wid,)).fetchone()
        return json.loads(row[0]) if row else None

    # ---- evidence chains (append-only) ----
    def append_events(self, log: str, start: int, events: list[dict]) -> None:
        if not events:
            return
        with self._lock:
            self._db.execute("BEGIN")
            try:
                self._db.executemany("INSERT INTO events (log, seq, event) VALUES (?, ?, ?)",
                                     [(log, start + i, json.dumps(e, separators=(",", ":"))) for i, e in enumerate(events)])
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def load_events(self, log: str) -> list[dict]:
        return [json.loads(r[0]) for r in self._db.execute("SELECT event FROM events WHERE log=? ORDER BY seq", (log,))]

    def archive_events(self, log: str) -> str:
        """Keep a cleared book's chain under an archive name; it is never deleted."""
        name = f"{log}@archived-{_now()}"
        with self._lock:
            self._db.execute("UPDATE events SET log=? WHERE log=?", (name, log))
        return name

    def archived_logs(self, log: str) -> list[str]:
        return [r[0] for r in self._db.execute("SELECT DISTINCT log FROM events WHERE log LIKE ? ORDER BY log", (log + "@archived-%",))]

    # ---- small documents ----
    def put(self, key: str, value) -> None:
        with self._lock:
            self._db.execute("INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                             (key, json.dumps(value, default=str), _now()))

    def get(self, key: str, default=None):
        row = self._db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def healthy(self) -> bool:
        try:
            self._db.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
