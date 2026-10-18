"""HTTP API. Same core as the MCP server and the CLI.

Auth: if HRS_COLLECTOR_TOKEN is set, every /v1 request needs
`Authorization: Bearer <token>`. The server refuses to bind a non-loopback
address without a token (see cli.serve).
"""

from __future__ import annotations

import hmac
import os
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

from . import CONTRACT_VERSION, __version__
from .adapters import ADAPTERS
from .errors import SourceError
from .models import (
    BatchResult,
    CollectParams,
    Credentials,
    CursorState,
    FetchResult,
    ProbeResult,
    Run,
    RunRequest,
    Source,
    SourceIn,
    SourceSpec,
    VacancyPage,
    Vacancy,
)
from .service import Collector


class ProbeOp(BaseModel):
    spec: SourceSpec
    credentials: Credentials | None = None


class CollectOp(BaseModel):
    spec: SourceSpec
    cursor: CursorState | None = None
    params: CollectParams = CollectParams()
    credentials: Credentials | None = None


class FetchOp(BaseModel):
    spec: SourceSpec
    external_id: str
    credentials: Credentials | None = None


class EnabledPatch(BaseModel):
    enabled: bool


class ImportRegistry(BaseModel):
    csv_path: str


class CredentialsBody(BaseModel):
    credentials: Credentials | None = None


def create_app(collector: Collector | None = None) -> FastAPI:
    core = collector or Collector()
    app = FastAPI(title="HR Shturman collector", version=__version__,
                  description=f"Contract {CONTRACT_VERSION}. See collector/CONTRACT.md.")

    def auth(authorization: str | None = Header(None)) -> None:
        token = os.environ.get("HRS_COLLECTOR_TOKEN")
        if not token:
            return
        given = (authorization or "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(given.encode(), token.encode()):
            raise HTTPException(401, "invalid or missing bearer token")

    @app.exception_handler(SourceError)
    async def _source_error(_, exc: SourceError):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=502 if exc.can_retry else 422, content={"error": exc.info().model_dump()})

    @app.exception_handler(KeyError)
    async def _not_found(_, exc: KeyError):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404, content={"error": {"code": "not_found", "message": str(exc.args[0])}})

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__, "contract": CONTRACT_VERSION, "transports": sorted(ADAPTERS)}

    v1 = [Depends(auth)]

    # ---- stateless operations (caller keeps cursor and results)

    @app.post("/v1/ops/probe", dependencies=v1)
    def op_probe(body: ProbeOp) -> ProbeResult:
        return core.op_probe(body.spec, body.credentials)

    @app.post("/v1/ops/collect", dependencies=v1)
    def op_collect(body: CollectOp) -> BatchResult:
        return core.op_collect(body.spec, body.cursor, body.params, body.credentials)

    @app.post("/v1/ops/fetch", dependencies=v1)
    def op_fetch(body: FetchOp) -> FetchResult:
        return core.op_fetch(body.spec, body.external_id, body.credentials)

    # ---- stateful collector

    @app.get("/v1/sources", dependencies=v1)
    def list_sources(candidate_id: str = "default") -> list[Source]:
        return core.list_sources(candidate_id)

    @app.post("/v1/sources", dependencies=v1)
    def add_source(body: SourceIn, candidate_id: str = "default") -> Source:
        return core.add_source(candidate_id, body)

    @app.patch("/v1/sources/{source_id}", dependencies=v1)
    def patch_source(source_id: str, body: EnabledPatch, candidate_id: str = "default") -> Source:
        src = core.set_enabled(candidate_id, source_id, body.enabled)
        if src is None:
            raise KeyError(f"source {source_id!r} not found")
        return src

    @app.post("/v1/sources/import-telegram-registry", dependencies=v1)
    def import_registry(body: ImportRegistry, candidate_id: str = "default") -> dict:
        return core.import_telegram_registry(candidate_id, Path(body.csv_path))

    @app.post("/v1/sources/{source_id}/probe", dependencies=v1)
    def probe_source(source_id: str, body: CredentialsBody | None = None, candidate_id: str = "default") -> ProbeResult:
        return core.probe_source(candidate_id, source_id, body.credentials if body else None)

    @app.post("/v1/runs", dependencies=v1)
    def start_run(body: RunRequest, wait: bool = False) -> Run:
        return core.run(body) if wait else core.start_run(body)

    @app.get("/v1/runs/{run_id}", dependencies=v1)
    def get_run(run_id: str) -> Run:
        run = core.get_run(run_id)
        if run is None:
            raise KeyError(f"run {run_id!r} not found")
        return run

    @app.get("/v1/runs", dependencies=v1)
    def list_runs(candidate_id: str = "default", limit: int = Query(20, ge=1, le=200)) -> list[Run]:
        return core.store.list_runs(candidate_id, limit)

    @app.get("/v1/vacancies", dependencies=v1)
    def search(candidate_id: str = "default", platform: str | None = None, source_id: str | None = None,
               q: str | None = None, since: datetime | None = None, is_vacancy: str | None = None,
               include_duplicates: bool = False, limit: int = Query(50, ge=1, le=500),
               page_token: str | None = None) -> VacancyPage:
        items, token = core.store.search_vacancies(candidate_id, platform=platform, source_id=source_id, q=q,
                                                   since=since, is_vacancy=is_vacancy,
                                                   include_duplicates=include_duplicates, limit=limit,
                                                   page_token=page_token)
        return VacancyPage(items=items, next_page_token=token)

    @app.get("/v1/vacancies/{vacancy_id}", dependencies=v1)
    def get_vacancy(vacancy_id: str, candidate_id: str = "default") -> dict:
        details = core.store.vacancy_details(candidate_id, vacancy_id)
        if details is None:
            raise KeyError(f"vacancy {vacancy_id!r} not found")
        return details

    @app.post("/v1/vacancies/{vacancy_id}/fetch-full", dependencies=v1)
    def fetch_full(vacancy_id: str, body: CredentialsBody | None = None, candidate_id: str = "default") -> Vacancy:
        return core.fetch_full_vacancy(candidate_id, vacancy_id, body.credentials if body else None)

    @app.get("/v1/stats", dependencies=v1)
    def stats(candidate_id: str = "default") -> dict:
        sources = core.list_sources(candidate_id)
        by_access: dict[str, int] = {}
        for s in sources:
            by_access[s.access_status] = by_access.get(s.access_status, 0) + 1
        return {"sources": len(sources), "sources_by_access": by_access, "vacancies": core.store.counts(candidate_id)}

    return app
