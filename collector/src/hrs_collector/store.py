"""SQLite store for the stateful (local) collector.

Nothing is deleted: a changed publication gets a new version row, duplicates are
linked through canonical_id, a vacancy seen through several sources keeps every
origin in vacancy_sources.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .models import CursorState, Run, Source, SourceIn, Vacancy, VacancyData
from .util import iso, parse_dt, stable_id, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
  candidate_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  spec_json TEXT NOT NULL,
  title TEXT, category TEXT, origin TEXT NOT NULL,
  enabled INTEGER NOT NULL,
  access_status TEXT NOT NULL DEFAULT 'unknown',
  current_title TEXT, stable_chat_id TEXT,
  checked_at TEXT, last_success_at TEXT, next_retry_at TEXT,
  fail_count INTEGER NOT NULL DEFAULT 0,
  last_error_code TEXT, last_error_message TEXT,
  cursor_json TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (candidate_id, source_id)
);
CREATE TABLE IF NOT EXISTS vacancies (
  vacancy_id TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  item_key TEXT NOT NULL,
  platform TEXT NOT NULL,
  data_json TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  snippet_hash TEXT,
  completeness TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  title_key TEXT,
  is_vacancy TEXT NOT NULL,
  search_text TEXT NOT NULL,
  published_at TEXT,
  version INTEGER NOT NULL,
  status TEXT NOT NULL,
  first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, changed_at TEXT NOT NULL,
  canonical_id TEXT NOT NULL,
  duplicate_status TEXT NOT NULL,
  UNIQUE (candidate_id, item_key)
);
CREATE INDEX IF NOT EXISTS vacancies_order ON vacancies (candidate_id, published_at DESC, vacancy_id DESC);
CREATE INDEX IF NOT EXISTS vacancies_fp ON vacancies (candidate_id, fingerprint);
CREATE INDEX IF NOT EXISTS vacancies_title ON vacancies (candidate_id, title_key);
CREATE TABLE IF NOT EXISTS vacancy_versions (
  vacancy_id TEXT NOT NULL, version INTEGER NOT NULL,
  content_hash TEXT NOT NULL, data_json TEXT NOT NULL, observed_at TEXT NOT NULL,
  PRIMARY KEY (vacancy_id, version)
);
CREATE TABLE IF NOT EXISTS vacancy_sources (
  vacancy_id TEXT NOT NULL, source_id TEXT NOT NULL,
  first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
  PRIMARY KEY (vacancy_id, source_id)
);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL,
  status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
  report_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leases (
  candidate_id TEXT NOT NULL, source_id TEXT NOT NULL,
  run_id TEXT NOT NULL, expires_at TEXT NOT NULL,
  PRIMARY KEY (candidate_id, source_id)
);
"""


def _title_key(v: VacancyData) -> str | None:
    if not v.title or not v.company:
        return None
    return " ".join(f"{v.title}|{v.company}".casefold().split())


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        with self._write_lock, self.connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------ sources

    def upsert_source(self, candidate_id: str, src: SourceIn, *, keep_user_state: bool = True) -> Source:
        now = iso(utcnow())
        with self.tx() as db:
            row = db.execute("SELECT enabled FROM sources WHERE candidate_id=? AND source_id=?",
                             (candidate_id, src.source_id)).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO sources (candidate_id, source_id, spec_json, title, category, origin, enabled,"
                    " access_status, cursor_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (candidate_id, src.source_id, src.spec.model_dump_json(), src.title, src.category, src.origin,
                     int(src.enabled), "unknown" if src.enabled else "disabled", CursorState().model_dump_json(), now, now),
                )
            else:
                enabled = row["enabled"] if keep_user_state else int(src.enabled)
                db.execute(
                    "UPDATE sources SET spec_json=?, title=?, category=?, origin=?, enabled=?, updated_at=?"
                    " WHERE candidate_id=? AND source_id=?",
                    (src.spec.model_dump_json(), src.title, src.category, src.origin, enabled, now,
                     candidate_id, src.source_id),
                )
        return self.get_source(candidate_id, src.source_id)  # type: ignore[return-value]

    def set_enabled(self, candidate_id: str, source_id: str, enabled: bool) -> Source | None:
        with self.tx() as db:
            db.execute(
                "UPDATE sources SET enabled=?, access_status=CASE WHEN ?=0 THEN 'disabled' "
                "WHEN access_status='disabled' THEN 'unknown' ELSE access_status END, updated_at=?"
                " WHERE candidate_id=? AND source_id=?",
                (int(enabled), int(enabled), iso(utcnow()), candidate_id, source_id),
            )
        return self.get_source(candidate_id, source_id)

    def get_source(self, candidate_id: str, source_id: str) -> Source | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM sources WHERE candidate_id=? AND source_id=?",
                             (candidate_id, source_id)).fetchone()
        return _source(row) if row else None

    def list_sources(self, candidate_id: str) -> list[Source]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM sources WHERE candidate_id=? ORDER BY source_id", (candidate_id,)).fetchall()
        return [_source(r) for r in rows]

    def record_source_result(self, candidate_id: str, source_id: str, *, ok: bool, access_status: str,
                             error_code: str | None, error_message: str | None, retry_after: int | None,
                             can_retry: bool, meta: dict) -> None:
        now = utcnow()
        with self.tx() as db:
            row = db.execute("SELECT fail_count FROM sources WHERE candidate_id=? AND source_id=?",
                             (candidate_id, source_id)).fetchone()
            fails = 0 if ok else (row["fail_count"] if row else 0) + 1
            if ok:
                next_retry = None
            elif can_retry:
                backoff = max(retry_after or 0, min(15 * 60 * 2 ** (fails - 1), 24 * 3600))
                next_retry = iso(now + timedelta(seconds=backoff))
            else:
                next_retry = iso(now + timedelta(hours=24))
            db.execute(
                "UPDATE sources SET access_status=?, checked_at=?, last_success_at=COALESCE(?, last_success_at),"
                " next_retry_at=?, fail_count=?, last_error_code=?, last_error_message=?,"
                " current_title=COALESCE(?, current_title), stable_chat_id=COALESCE(?, stable_chat_id), updated_at=?"
                " WHERE candidate_id=? AND source_id=?",
                (access_status, iso(now), iso(now) if ok else None, next_retry, fails,
                 None if ok else error_code, None if ok else error_message,
                 meta.get("current_title"), meta.get("stable_chat_id"), iso(now), candidate_id, source_id),
            )

    # ------------------------------------------------------------ leases

    def acquire_lease(self, candidate_id: str, source_id: str, run_id: str, ttl_seconds: int = 1800) -> bool:
        now = utcnow()
        with self.tx() as db:
            row = db.execute("SELECT run_id, expires_at FROM leases WHERE candidate_id=? AND source_id=?",
                             (candidate_id, source_id)).fetchone()
            if row and row["run_id"] != run_id and parse_dt(row["expires_at"]) > now:  # type: ignore[operator]
                return False
            db.execute("INSERT OR REPLACE INTO leases VALUES (?,?,?,?)",
                       (candidate_id, source_id, run_id, iso(now + timedelta(seconds=ttl_seconds))))
            return True

    def release_lease(self, candidate_id: str, source_id: str, run_id: str) -> None:
        with self.tx() as db:
            db.execute("DELETE FROM leases WHERE candidate_id=? AND source_id=? AND run_id=?",
                       (candidate_id, source_id, run_id))

    # ------------------------------------------------------------ page commit

    def commit_page(self, candidate_id: str, source_id: str, run_id: str, items: list[VacancyData],
                    cursor: CursorState) -> dict[str, int]:
        """Persist one page and the cursor that becomes valid with it, atomically.

        Refuses (lease_lost) if another run took over the source meanwhile.
        """
        now = utcnow()
        counts = {"new": 0, "updated": 0, "unchanged": 0}
        with self.tx() as db:
            lease = db.execute("SELECT run_id FROM leases WHERE candidate_id=? AND source_id=?",
                               (candidate_id, source_id)).fetchone()
            if lease is None or lease["run_id"] != run_id:
                from .errors import SourceError
                raise SourceError("lease_lost", f"{source_id}: lease held by another run", can_retry=True)
            for item in items:
                counts[self._upsert_vacancy(db, candidate_id, source_id, item, now)] += 1
            db.execute("UPDATE sources SET cursor_json=?, updated_at=? WHERE candidate_id=? AND source_id=?",
                       (cursor.model_dump_json(), iso(now), candidate_id, source_id))
        return counts

    def upsert_vacancy(self, candidate_id: str, source_id: str, item: VacancyData) -> str:
        with self.tx() as db:
            return self._upsert_vacancy(db, candidate_id, source_id, item, utcnow())

    def _upsert_vacancy(self, db: sqlite3.Connection, candidate_id: str, source_id: str,
                        item: VacancyData, now: datetime) -> str:
        vid = stable_id("vac", candidate_id, item.item_key)
        t = iso(now)
        db.execute(
            "INSERT INTO vacancy_sources VALUES (?,?,?,?) ON CONFLICT(vacancy_id, source_id) DO UPDATE SET last_seen_at=excluded.last_seen_at",
            (vid, source_id, t, t),
        )
        row = db.execute("SELECT * FROM vacancies WHERE vacancy_id=?", (vid,)).fetchone()
        snippet_hash = item.content_hash if item.completeness == "snippet" else None
        if row is None:
            self._write_vacancy(db, vid, candidate_id, source_id, item, version=1, first_seen=t, now=t,
                                snippet_hash=snippet_hash, insert=True)
            return "new"
        if item.content_hash == row["content_hash"] or (snippet_hash and snippet_hash == row["snippet_hash"]):
            db.execute("UPDATE vacancies SET last_seen_at=? WHERE vacancy_id=?", (t, vid))
            return "unchanged"
        stored = VacancyData.model_validate_json(row["data_json"])
        if item.completeness == "snippet" and stored.completeness == "full":
            # Snippet changed under a full record: overlay snippet fields, flag for refetch.
            data = stored.model_dump()
            for key in ("title", "company", "location", "salary", "employment", "work_format", "published_at"):
                value = getattr(item, key)
                if value not in (None, []):
                    data[key] = value.model_dump() if hasattr(value, "model_dump") else value
            data["completeness"] = "snippet"
            data["content_hash"] = item.content_hash
            item = VacancyData.model_validate(data)
        elif item.completeness == "full":
            from .engine import merge_full
            item = merge_full(stored, item)
            snippet_hash = row["snippet_hash"]
        self._write_vacancy(db, vid, candidate_id, row["source_id"], item, version=row["version"] + 1,
                            first_seen=row["first_seen_at"], now=t, snippet_hash=snippet_hash, insert=False)
        return "updated"

    def _write_vacancy(self, db, vid, candidate_id, source_id, item: VacancyData, *, version, first_seen, now,
                       snippet_hash, insert) -> None:
        title_key = _title_key(item)
        search_text = " ".join(x for x in (item.title, item.company, item.location, item.text) if x).casefold()
        canonical, dup = self._dedup(db, candidate_id, vid, item.fingerprint, title_key)
        values = {
            "vacancy_id": vid, "candidate_id": candidate_id, "source_id": source_id, "item_key": item.item_key,
            "platform": item.platform, "data_json": item.model_dump_json(), "content_hash": item.content_hash,
            "snippet_hash": snippet_hash, "completeness": item.completeness, "fingerprint": item.fingerprint,
            "title_key": title_key, "is_vacancy": item.is_vacancy, "search_text": search_text,
            "published_at": iso(item.published_at), "version": version, "status": "normalized",
            "first_seen_at": first_seen, "last_seen_at": now, "changed_at": now, "canonical_id": canonical, "duplicate_status": dup,
        }
        cols = ", ".join(values)
        if insert:
            db.execute(f"INSERT INTO vacancies ({cols}) VALUES ({', '.join('?' * len(values))})", tuple(values.values()))
        else:
            sets = ", ".join(f"{k}=?" for k in values if k not in ("vacancy_id", "first_seen_at"))
            db.execute(f"UPDATE vacancies SET {sets} WHERE vacancy_id=?",
                       tuple(v for k, v in values.items() if k not in ("vacancy_id", "first_seen_at")) + (vid,))
        db.execute("INSERT OR REPLACE INTO vacancy_versions VALUES (?,?,?,?,?)",
                   (vid, version, item.content_hash, item.model_dump_json(), now))

    @staticmethod
    def _dedup(db, candidate_id: str, vid: str, fp: str, title_key: str | None) -> tuple[str, str]:
        """Same wording = duplicate of the earliest record; same title+company with
        different wording = suspected duplicate. Neither deletes anything."""
        # Rows are written one at a time under the write lock, so "already stored" means "seen earlier".
        row = db.execute(
            "SELECT canonical_id FROM vacancies WHERE candidate_id=? AND fingerprint=? AND vacancy_id<>?"
            " ORDER BY first_seen_at, vacancy_id LIMIT 1",
            (candidate_id, fp, vid),
        ).fetchone()
        if row and row["canonical_id"] != vid:
            return row["canonical_id"], "duplicate"
        if row:
            return vid, "unique"
        if title_key:
            row = db.execute(
                "SELECT 1 FROM vacancies WHERE candidate_id=? AND title_key=? AND fingerprint<>? AND vacancy_id<>? LIMIT 1",
                (candidate_id, title_key, fp, vid),
            ).fetchone()
            if row:
                return vid, "suspected_duplicate"
        return vid, "unique"

    # ------------------------------------------------------------ vacancy queries

    def get_vacancy(self, candidate_id: str, vacancy_id: str) -> Vacancy | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM vacancies WHERE candidate_id=? AND vacancy_id=?",
                             (candidate_id, vacancy_id)).fetchone()
        return _vacancy(row) if row else None

    def vacancy_details(self, candidate_id: str, vacancy_id: str) -> dict | None:
        vac = self.get_vacancy(candidate_id, vacancy_id)
        if vac is None:
            return None
        with self.connect() as db:
            origins = [dict(r) for r in db.execute(
                "SELECT source_id, first_seen_at, last_seen_at FROM vacancy_sources WHERE vacancy_id=? ORDER BY source_id",
                (vacancy_id,))]
            versions = [dict(r) for r in db.execute(
                "SELECT version, content_hash, observed_at FROM vacancy_versions WHERE vacancy_id=? ORDER BY version",
                (vacancy_id,))]
            title_key = db.execute("SELECT title_key FROM vacancies WHERE vacancy_id=?", (vacancy_id,)).fetchone()[0]
            related = [dict(r) for r in db.execute(
                "SELECT vacancy_id, duplicate_status, CASE WHEN fingerprint=? THEN 'same_text' ELSE 'same_title_company' END AS relation"
                " FROM vacancies WHERE candidate_id=? AND vacancy_id<>? AND (fingerprint=? OR (title_key IS NOT NULL AND title_key=?))"
                " ORDER BY vacancy_id",
                (vac.fingerprint, candidate_id, vacancy_id, vac.fingerprint, title_key))]
        return {"vacancy": vac.model_dump(mode="json"), "origins": origins, "versions": versions, "related": related}

    def search_vacancies(self, candidate_id: str, *, platform: str | None = None, source_id: str | None = None,
                         q: str | None = None, since: datetime | None = None, is_vacancy: str | None = None,
                         include_duplicates: bool = False, limit: int = 50,
                         page_token: str | None = None) -> tuple[list[Vacancy], str | None]:
        where = ["v.candidate_id=?"]
        args: list = [candidate_id]
        if platform:
            where.append("v.platform=?")
            args.append(platform)
        if source_id:
            where.append("EXISTS (SELECT 1 FROM vacancy_sources s WHERE s.vacancy_id=v.vacancy_id AND s.source_id=?)")
            args.append(source_id)
        for term in (q or "").casefold().split():
            where.append("instr(v.search_text, ?) > 0")
            args.append(term)
        if since:
            where.append("v.published_at >= ?")
            args.append(iso(since))
        if is_vacancy:
            where.append("v.is_vacancy=?")
            args.append(is_vacancy)
        if not include_duplicates:
            where.append("v.duplicate_status<>'duplicate'")
        if page_token:
            pub, vid = base64.urlsafe_b64decode(page_token.encode()).decode().split("|", 1)
            where.append("(COALESCE(v.published_at,''), v.vacancy_id) < (?, ?)")
            args.extend([pub, vid])
        limit = max(1, min(limit, 500))
        sql = (f"SELECT v.* FROM vacancies v WHERE {' AND '.join(where)}"
               " ORDER BY COALESCE(v.published_at,'') DESC, v.vacancy_id DESC LIMIT ?")
        with self.connect() as db:
            rows = db.execute(sql, (*args, limit + 1)).fetchall()
        items = [_vacancy(r) for r in rows[:limit]]
        token = None
        if len(rows) > limit:
            last = rows[limit - 1]
            token = base64.urlsafe_b64encode(f"{last['published_at'] or ''}|{last['vacancy_id']}".encode()).decode()
        return items, token

    def counts(self, candidate_id: str) -> dict:
        with self.connect() as db:
            rows = db.execute("SELECT platform, duplicate_status, COUNT(*) n FROM vacancies WHERE candidate_id=?"
                              " GROUP BY platform, duplicate_status", (candidate_id,)).fetchall()
        out: dict = {}
        for r in rows:
            out.setdefault(r["platform"], {})[r["duplicate_status"]] = r["n"]
        return out

    # ------------------------------------------------------------ runs

    def save_run(self, run: Run) -> None:
        with self.tx() as db:
            db.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?)",
                       (run.run_id, run.candidate_id, run.status, iso(run.started_at), iso(run.finished_at),
                        run.model_dump_json()))

    def get_run(self, run_id: str) -> Run | None:
        with self.connect() as db:
            row = db.execute("SELECT report_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return Run.model_validate_json(row[0]) if row else None

    def list_runs(self, candidate_id: str, limit: int = 20) -> list[Run]:
        with self.connect() as db:
            rows = db.execute("SELECT report_json FROM runs WHERE candidate_id=? ORDER BY started_at DESC, run_id DESC LIMIT ?",
                              (candidate_id, limit)).fetchall()
        return [Run.model_validate_json(r[0]) for r in rows]


def _source(row: sqlite3.Row) -> Source:
    return Source(
        candidate_id=row["candidate_id"], source_id=row["source_id"],
        spec=json.loads(row["spec_json"]), title=row["title"], category=row["category"], origin=row["origin"],
        enabled=bool(row["enabled"]), access_status=row["access_status"], current_title=row["current_title"],
        stable_chat_id=row["stable_chat_id"], checked_at=parse_dt(row["checked_at"]),
        last_success_at=parse_dt(row["last_success_at"]), next_retry_at=parse_dt(row["next_retry_at"]),
        fail_count=row["fail_count"], last_error_code=row["last_error_code"],
        last_error_message=row["last_error_message"], cursor=CursorState.model_validate_json(row["cursor_json"]),
    )


def _vacancy(row: sqlite3.Row) -> Vacancy:
    data = json.loads(row["data_json"])
    data.update(
        vacancy_id=row["vacancy_id"], candidate_id=row["candidate_id"], source_id=row["source_id"],
        version=row["version"], status=row["status"], first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"], changed_at=row["changed_at"], canonical_id=row["canonical_id"],
        duplicate_status=row["duplicate_status"],
    )
    return Vacancy.model_validate(data)

