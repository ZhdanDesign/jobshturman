"""Durable workspace store.

Every object belongs to one candidate_id and carries a version for optimistic
locking. Every mutation appends an event in the same transaction. The web
branch implements the same Tx interface on its own database and adds tenant_id.
"""
import contextlib
import json
import sqlite3
from pathlib import Path

from .common import SCHEMA_VERSION, DomainError, canonical, new_id, now

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS objects (
    kind TEXT NOT NULL,
    object_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    natural_key TEXT,
    version INTEGER NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (kind, object_id));
CREATE UNIQUE INDEX IF NOT EXISTS objects_natural_key
    ON objects(kind, candidate_id, natural_key) WHERE natural_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS objects_candidate ON objects(candidate_id, kind);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    candidate_id TEXT NOT NULL,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    object_kind TEXT,
    object_id TEXT,
    actor TEXT NOT NULL,
    details TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_candidate ON events(candidate_id, seq);
"""


class Store:
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()

    def init(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = self._connect()
        try:
            con.executescript(SCHEMA)
            con.execute("INSERT OR IGNORE INTO meta VALUES('schema_version', ?)", (SCHEMA_VERSION,))
        finally:
            con.close()
        self.path.chmod(0o600)
        return self

    def _connect(self):
        con = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA synchronous=FULL")
        return con

    @contextlib.contextmanager
    def transaction(self, actor):
        if not self.path.is_file():
            raise DomainError("not_configured", "Workspace is not initialized")
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            try:
                yield Tx(con, actor)
                con.commit()
            except BaseException:
                con.rollback()
                raise
        finally:
            con.close()

    def read(self):
        """Read-only view; writes inside it are rolled back."""
        return self.transaction("reader")


class Tx:
    def __init__(self, con, actor):
        self.con = con
        self.actor = actor

    @staticmethod
    def _row(row):
        if row is None:
            return None
        data = json.loads(row["data"])
        data.update(object_id=row["object_id"], candidate_id=row["candidate_id"],
                    version=row["version"], created_at=row["created_at"], updated_at=row["updated_at"])
        return data

    def get(self, kind, candidate_id, object_id):
        row = self.con.execute(
            "SELECT * FROM objects WHERE kind=? AND object_id=? AND candidate_id=?",
            (kind, object_id, candidate_id)).fetchone()
        if row is None:
            raise DomainError("not_found", f"Unknown {kind}")
        return self._row(row)

    def find(self, kind, candidate_id, natural_key):
        return self._row(self.con.execute(
            "SELECT * FROM objects WHERE kind=? AND candidate_id=? AND natural_key=?",
            (kind, candidate_id, natural_key)).fetchone())

    def list(self, kind, candidate_id):
        rows = self.con.execute(
            "SELECT * FROM objects WHERE kind=? AND candidate_id=? ORDER BY created_at, object_id",
            (kind, candidate_id))
        return [self._row(r) for r in rows]

    def insert(self, kind, candidate_id, data, prefix, natural_key=None):
        object_id = new_id(prefix)
        stamp = now()
        self.con.execute(
            "INSERT INTO objects VALUES(?,?,?,?,1,?,?,?)",
            (kind, object_id, candidate_id, natural_key, canonical(self._payload(data)), stamp, stamp))
        return self.get(kind, candidate_id, object_id)

    def update(self, kind, candidate_id, object_id, data, expected_version):
        cur = self.con.execute(
            "UPDATE objects SET data=?, version=version+1, updated_at=? "
            "WHERE kind=? AND object_id=? AND candidate_id=? AND version=?",
            (canonical(self._payload(data)), now(), kind, object_id, candidate_id, expected_version))
        if cur.rowcount != 1:
            self.get(kind, candidate_id, object_id)  # raises not_found for a foreign or missing id
            raise DomainError("state_version_conflict", f"{kind} changed; reread and retry")
        return self.get(kind, candidate_id, object_id)

    def event(self, candidate_id, event_kind, obj=None, **details):
        event_id = new_id("evt")
        self.con.execute(
            "INSERT INTO events(event_id,candidate_id,at,kind,object_kind,object_id,actor,details) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (event_id, candidate_id, now(), event_kind, obj and obj[0], obj and obj[1], self.actor, canonical(details)))
        return event_id

    def events(self, candidate_id):
        rows = self.con.execute("SELECT * FROM events WHERE candidate_id=? ORDER BY seq", (candidate_id,))
        return [dict(r, details=json.loads(r["details"])) for r in rows]

    @staticmethod
    def _payload(data):
        meta = {"object_id", "candidate_id", "version", "created_at", "updated_at"}
        return {k: v for k, v in data.items() if k not in meta}
