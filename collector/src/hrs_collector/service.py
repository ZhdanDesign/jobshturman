"""Collector core used by the CLI, the HTTP API and the MCP server alike.

Two layers:
- stateless operations (op_probe / op_collect / op_fetch): the caller keeps the
  cursor and the results. This is what the web app uses.
- stateful collector (sources, runs, vacancies in SQLite): the local version.
"""

from __future__ import annotations

import csv
import os
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import credentials as creds_mod
from .adapters import make_adapter
from .engine import collect_batch, merge_full
from .errors import SourceError
from .http import Http
from .models import (
    BatchResult,
    CollectParams,
    Credentials,
    CursorState,
    FetchResult,
    ProbeResult,
    Run,
    RunRequest,
    RunSourceReport,
    Source,
    SourceIn,
    SourceSpec,
    Vacancy,
)
from .store import Store
from .util import as_utc, utcnow

NO_ACCESS = {"not_authenticated", "private_invite_required", "web_preview_unavailable", "unavailable",
             "captcha_required", "two_factor_required", "not_configured", "unsupported_environment"}
ACCESS_BY_ERROR = {
    "not_authenticated": "not_authenticated",
    "not_configured": "read_failed",
    "private_invite_required": "private_invite_required",
    "web_preview_unavailable": "read_failed",
    "unavailable": "unavailable",
}


def default_data_dir() -> Path:
    return Path(os.environ.get("HRS_COLLECTOR_DATA", Path.home() / ".hrs-collector")).expanduser()


class Collector:
    def __init__(self, data_dir: Path | None = None, *, store: Store | None = None, http_factory=Http) -> None:
        self.data_dir = data_dir or default_data_dir()
        self.store = store or Store(self.data_dir / "collector.sqlite3")
        self.http_factory = http_factory
        self._runs: dict[str, threading.Thread] = {}

    def _creds(self, override: Credentials | None) -> Credentials:
        return creds_mod.merge(creds_mod.load_local(self.data_dir), override)

    # ------------------------------------------------------------ stateless operations

    def op_probe(self, spec: SourceSpec, credentials: Credentials | None = None) -> ProbeResult:
        http = self.http_factory()
        adapter = make_adapter(spec.transport, http, self._creds(credentials))
        try:
            return adapter.probe(spec)
        finally:
            adapter.close()
            http.close()

    def op_collect(self, spec: SourceSpec, cursor: CursorState | None, params: CollectParams,
                   credentials: Credentials | None = None) -> BatchResult:
        http = self.http_factory()
        adapter = make_adapter(spec.transport, http, self._creds(credentials))
        try:
            return collect_batch(adapter, spec, cursor, params)
        finally:
            adapter.close()
            http.close()

    def op_fetch(self, spec: SourceSpec, external_id: str, credentials: Credentials | None = None) -> FetchResult:
        http = self.http_factory()
        adapter = make_adapter(spec.transport, http, self._creds(credentials))
        try:
            item = adapter.fetch_full(spec, external_id)
            return FetchResult(platform=spec.platform, transport=spec.transport, adapter_version=adapter.version,
                               observed_at=utcnow(), status="ok", item=item)
        except SourceError as exc:
            return FetchResult(platform=spec.platform, transport=spec.transport, adapter_version=adapter.version,
                               observed_at=utcnow(), status="error", error=exc.info())
        finally:
            adapter.close()
            http.close()

    # ------------------------------------------------------------ sources

    def add_source(self, candidate_id: str, src: SourceIn) -> Source:
        return self.store.upsert_source(candidate_id, src, keep_user_state=False)

    def list_sources(self, candidate_id: str) -> list[Source]:
        return self.store.list_sources(candidate_id)

    def set_enabled(self, candidate_id: str, source_id: str, enabled: bool) -> Source | None:
        return self.store.set_enabled(candidate_id, source_id, enabled)

    def import_telegram_registry(self, candidate_id: str, csv_path: Path) -> dict:
        """Import the canonical 210-channel CSV. Re-import keeps the user's enabled flag and cursors."""
        created = updated = 0
        with open(csv_path, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                username = (row.get("current_username") or row.get("username") or "").strip().lstrip("@")
                title = (row.get("current_name") or row.get("title") or "").strip()
                if username:
                    spec = SourceSpec(platform="telegram", transport="telegram_web", kind="channel",
                                      config={"username": username, "url": row.get("url") or f"https://t.me/{username}"})
                else:
                    spec = SourceSpec(platform="telegram", transport="telegram_mtproto", kind="channel",
                                      config={"title": title})
                exists = self.store.get_source(candidate_id, row["source_id"]) is not None
                self.store.upsert_source(candidate_id, SourceIn(
                    source_id=row["source_id"], spec=spec, title=title, origin="registry_csv",
                    enabled=(row.get("included_in_search") or "").strip().lower() in ("да", "yes", "true", "1"),
                ))
                updated += exists
                created += not exists
        return {"created": created, "updated": updated, "total": created + updated}

    def probe_source(self, candidate_id: str, source_id: str, credentials: Credentials | None = None) -> ProbeResult:
        src = self._require_source(candidate_id, source_id)
        result = self.op_probe(src.spec, credentials)
        err = result.error
        self.store.record_source_result(
            candidate_id, source_id, ok=result.status == "ok", access_status=result.access_status if src.enabled else "disabled",
            error_code=err.code if err else None, error_message=err.message if err else None,
            retry_after=err.retry_after_seconds if err else None, can_retry=err.can_retry if err else True,
            meta=result.source_meta,
        )
        return result

    def _require_source(self, candidate_id: str, source_id: str) -> Source:
        src = self.store.get_source(candidate_id, source_id)
        if src is None:
            raise KeyError(f"source {source_id!r} not found for candidate {candidate_id!r}")
        return src

    # ------------------------------------------------------------ runs

    def start_run(self, req: RunRequest) -> Run:
        """Start a run in a background thread and return its initial report."""
        run = self._new_run(req)
        thread = threading.Thread(target=self._execute, args=(run, req), daemon=True, name=run.run_id)
        self._runs[run.run_id] = thread
        thread.start()
        return run

    def run(self, req: RunRequest) -> Run:
        run = self._new_run(req)
        return self._execute(run, req)

    def get_run(self, run_id: str) -> Run | None:
        return self.store.get_run(run_id)

    def _new_run(self, req: RunRequest) -> Run:
        now = utcnow()
        sources = self._select(req)
        run = Run(run_id=f"run_{uuid.uuid4().hex[:20]}", candidate_id=req.candidate_id,
                  status="running", started_at=now, params=req.params,
                  sources=[RunSourceReport(source_id=s.source_id, status="pending") for s in sources])
        self.store.save_run(run)
        return run

    def _select(self, req: RunRequest) -> list[Source]:
        out = []
        for src in self.store.list_sources(req.candidate_id):
            if req.source_ids is not None and src.source_id not in req.source_ids:
                continue
            if req.platforms and src.spec.platform not in req.platforms:
                continue
            out.append(src)
        return out

    def _execute(self, run: Run, req: RunRequest) -> Run:
        creds = self._creds(req.credentials)
        by_platform: dict[str, list[Source]] = {}
        for src in self._select(req):
            by_platform.setdefault(src.spec.platform, []).append(src)
        reports: dict[str, RunSourceReport] = {r.source_id: r for r in run.sources}
        lock = threading.Lock()

        def work(sources: list[Source]) -> None:
            http = self.http_factory()
            try:
                for src in sources:
                    rep = self._collect_source(run, src, req, creds, http)
                    with lock:
                        reports[src.source_id] = rep
                        run.sources = [reports[r.source_id] for r in run.sources]
                        self.store.save_run(run)
            finally:
                http.close()

        # One worker per platform: sources of one host are read sequentially (politeness),
        # different platforms in parallel.
        with ThreadPoolExecutor(max_workers=max(1, len(by_platform))) as pool:
            for future in [pool.submit(work, group) for group in by_platform.values()]:
                future.result()

        run.sources = [reports[r.source_id] for r in run.sources]
        statuses = [r.status for r in run.sources]
        run.totals = {k: sum(getattr(r, k) for r in run.sources) for k in ("pages", "items_seen", "items_new", "items_updated")}
        for st in ("ok", "no_new", "partial", "no_access", "error", "skipped"):
            run.totals[f"sources_{st}"] = statuses.count(st)
        if not statuses or all(s in ("ok", "no_new", "skipped") for s in statuses):
            run.status = "ok"
        elif any(s in ("ok", "no_new", "partial") for s in statuses):
            run.status = "partial"
        else:
            run.status = "error"
        run.finished_at = utcnow()
        self.store.save_run(run)
        return run

    def _collect_source(self, run: Run, src: Source, req: RunRequest, creds: Credentials, http: Http) -> RunSourceReport:
        now = utcnow()
        if not src.enabled:
            return RunSourceReport(source_id=src.source_id, status="skipped")
        if src.next_retry_at and as_utc(src.next_retry_at) > now and not req.force:  # type: ignore[operator]
            return RunSourceReport(source_id=src.source_id, status="skipped",
                                   error=None if not src.last_error_code else _backoff_info(src))
        if not self.store.acquire_lease(run.candidate_id, src.source_id, run.run_id):
            return RunSourceReport(source_id=src.source_id, status="skipped",
                                   error=SourceError("lease_lost", "another run is collecting this source", can_retry=True).info())
        counts = {"new": 0, "updated": 0, "unchanged": 0}

        def on_page(items, cursor) -> None:
            page_counts = self.store.commit_page(run.candidate_id, src.source_id, run.run_id, items, cursor)
            for k, v in page_counts.items():
                counts[k] += v

        adapter = None
        try:
            adapter = make_adapter(src.spec.transport, http, creds)
            result = collect_batch(adapter, src.spec, src.cursor, req.params, on_page=on_page)
        except SourceError as exc:
            result = None
            error = exc
        except Exception as exc:  # adapter bug: keep the run going, record it
            result = None
            error = SourceError("internal_error", f"{type(exc).__name__}: {exc}", can_retry=True)
            traceback.print_exc()
        finally:
            if adapter is not None:
                adapter.close()
            self.store.release_lease(run.candidate_id, src.source_id, run.run_id)

        if result is not None:
            error = SourceError(result.error.code, result.error.message, stage=result.error.stage,
                                can_retry=result.error.can_retry, retry_after=result.error.retry_after_seconds) if result.error else None
        ok = error is None
        access = "accessible" if ok or (result is not None and result.items) else ACCESS_BY_ERROR.get(error.code, "read_failed")  # type: ignore[union-attr]
        self.store.record_source_result(
            run.candidate_id, src.source_id, ok=ok, access_status=access,
            error_code=None if ok else error.code, error_message=None if ok else error.message,  # type: ignore[union-attr]
            retry_after=None if ok else error.retry_after, can_retry=True if ok else error.can_retry,  # type: ignore[union-attr]
            meta=result.source_meta if result else {},
        )
        if ok:
            status = result.status  # type: ignore[union-attr]
            if status in ("ok", "no_new"):
                status = "ok" if counts["new"] or counts["updated"] else "no_new"
        else:
            status = "no_access" if error.code in NO_ACCESS else "error"  # type: ignore[union-attr]
            if result is not None and result.coverage.pages and status == "error":
                status = "partial"
        return RunSourceReport(
            source_id=src.source_id, status=status,
            pages=result.coverage.pages if result else 0,
            items_seen=result.coverage.items_seen if result else 0,
            items_new=counts["new"], items_updated=counts["updated"],
            coverage=result.coverage if result else None,
            error=None if ok else error.info(),  # type: ignore[union-attr]
        )

    # ------------------------------------------------------------ vacancies

    def fetch_full_vacancy(self, candidate_id: str, vacancy_id: str, credentials: Credentials | None = None) -> Vacancy:
        vac = self.store.get_vacancy(candidate_id, vacancy_id)
        if vac is None:
            raise KeyError(f"vacancy {vacancy_id!r} not found")
        src = self._require_source(candidate_id, vac.source_id)
        result = self.op_fetch(src.spec, vac.external_id, credentials)
        if result.status != "ok" or result.item is None:
            raise SourceError(result.error.code, result.error.message, stage="fetch_full",  # type: ignore[union-attr]
                              can_retry=result.error.can_retry)  # type: ignore[union-attr]
        current = Vacancy.model_validate(vac.model_dump())
        merged = merge_full(current, result.item)
        self.store.upsert_vacancy(candidate_id, vac.source_id, merged)
        return self.store.get_vacancy(candidate_id, vacancy_id)  # type: ignore[return-value]


def _backoff_info(src: Source):
    from .models import ErrorInfo
    return ErrorInfo(code=src.last_error_code, message=f"in backoff until {src.next_retry_at}: {src.last_error_message}",  # type: ignore[arg-type]
                     stage="schedule", can_retry=True)
