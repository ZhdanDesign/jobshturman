"""MCP server (stdio) over the same Collector core as the HTTP API.

Tools never accept or return platform secrets: the agent works with local
credentials configured by the owner (`hrs-collector telegram-login`, env vars).
Collection results are deterministic reads of the local store; only
run_collection, probe_source and fetch_full_vacancy touch the network.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from .models import CollectParams, RunRequest, SourceIn, SourceSpec
from .service import Collector
from .util import parse_dt


INSTRUCTIONS = (
    "Vacancy collector for HR Shturman. Sources: HeadHunter, Telegram channels, LinkedIn. "
    "Typical flow: list_sources -> run_collection -> search_vacancies -> get_vacancy. "
    "A source status of no_access or partial is not 'zero vacancies': report coverage honestly. "
    "Never treat a collected vacancy as permission to apply."
)


def build_server(collector: Collector | None = None) -> MCPServer:
    mcp = MCPServer("hrs-collector", instructions=INSTRUCTIONS)
    return register_tools(mcp, collector or Collector())


def register_tools(mcp: MCPServer, core: Collector, default_candidate: str = "default") -> MCPServer:
    """Add the collector tools to an existing server (the hrs CLI serves them next to the local app tools)."""

    @mcp.tool()
    def list_sources(candidate_id: str = default_candidate, platform: str | None = None,
                     only_enabled: bool = False) -> list[dict[str, Any]]:
        """List configured sources with access status, last success, backoff and cursor watermark."""
        out = []
        for s in core.list_sources(candidate_id):
            if platform and s.spec.platform != platform:
                continue
            if only_enabled and not s.enabled:
                continue
            out.append({
                "source_id": s.source_id, "platform": s.spec.platform, "transport": s.spec.transport,
                "title": s.current_title or s.title, "config": s.spec.config, "enabled": s.enabled,
                "access_status": s.access_status, "last_success_at": s.last_success_at,
                "next_retry_at": s.next_retry_at, "last_error_code": s.last_error_code,
                "watermark_at": s.cursor.watermark_at, "has_unfinished_range": bool(s.cursor.resume_token),
            })
        return out

    @mcp.tool()
    def add_search_source(source_id: str, platform: str, keywords: str, location: str | None = None,
                          hh_area: str | None = None, candidate_id: str = default_candidate) -> dict[str, Any]:
        """Add an HH (platform='hh') or LinkedIn (platform='linkedin') search query as a source.

        hh_area is an hh.ru area id (1 Moscow, 2 Saint Petersburg, 113 Russia); location is LinkedIn free text.
        """
        if platform == "hh":
            spec = SourceSpec(platform="hh", transport="hh_web", kind="search",
                              config={"text": keywords, **({"area": hh_area} if hh_area else {})})
        elif platform == "linkedin":
            spec = SourceSpec(platform="linkedin", transport="linkedin_guest", kind="search",
                              config={"keywords": keywords, **({"location": location} if location else {})})
        else:
            raise ValueError("platform must be 'hh' or 'linkedin'")
        return core.add_source(candidate_id, SourceIn(source_id=source_id, spec=spec, title=keywords)).model_dump(mode="json")

    @mcp.tool()
    def add_telegram_channel(source_id: str, username: str, candidate_id: str = default_candidate) -> dict[str, Any]:
        """Add a public Telegram channel by @username (read via public web preview)."""
        spec = SourceSpec(platform="telegram", transport="telegram_web", kind="channel",
                          config={"username": username.lstrip("@")})
        return core.add_source(candidate_id, SourceIn(source_id=source_id, spec=spec, title=username)).model_dump(mode="json")

    @mcp.tool()
    def set_source_enabled(source_id: str, enabled: bool, candidate_id: str = default_candidate) -> dict[str, Any]:
        """Enable or disable a source. Only do this on the candidate's explicit instruction."""
        src = core.set_enabled(candidate_id, source_id, enabled)
        if src is None:
            raise ValueError(f"source {source_id} not found")
        return {"source_id": src.source_id, "enabled": src.enabled, "access_status": src.access_status}

    @mcp.tool()
    def probe_source(source_id: str, candidate_id: str = default_candidate) -> dict[str, Any]:
        """Check one source for real: reachable, authorized, readable. Updates its access status."""
        return core.probe_source(candidate_id, source_id).model_dump(mode="json")

    @mcp.tool()
    def run_collection(candidate_id: str = default_candidate, source_ids: list[str] | None = None,
                       platforms: list[str] | None = None, lookback_days: int = 7, page_budget: int = 5,
                       fetch_full: bool = False, force: bool = False) -> dict[str, Any]:
        """Collect new publications and wait for the result.

        Returns per-source status (ok, no_new, partial, no_access, error, skipped) with coverage.
        Reading all 210 Telegram channels takes minutes; pass source_ids or platforms to narrow it.
        """
        req = RunRequest(candidate_id=candidate_id, source_ids=source_ids, platforms=platforms,  # type: ignore[arg-type]
                         params=CollectParams(lookback_days=lookback_days, page_budget=page_budget,
                                              fetch_full=fetch_full), force=force)
        run = core.run(req)
        report = run.model_dump(mode="json")
        report["sources"] = [s for s in report["sources"] if s["status"] != "skipped"] + \
            [{"skipped": [s["source_id"] for s in report["sources"] if s["status"] == "skipped"]}]
        return report

    @mcp.tool()
    def get_run(run_id: str) -> dict[str, Any]:
        """Report of a previous collection run."""
        run = core.get_run(run_id)
        if run is None:
            raise ValueError(f"run {run_id} not found")
        return run.model_dump(mode="json")

    @mcp.tool()
    def search_vacancies(candidate_id: str = default_candidate, q: str | None = None, platform: str | None = None,
                         source_id: str | None = None, since: str | None = None,
                         is_vacancy: str | None = "likely", include_duplicates: bool = False,
                         limit: int = 20, page_token: str | None = None) -> dict[str, Any]:
        """Search collected vacancies, newest first. q matches all words (case-insensitive) in title, company,
        location and text. since is an ISO date. is_vacancy: likely|unlikely|unknown|null for all."""
        items, token = core.store.search_vacancies(
            candidate_id, platform=platform, source_id=source_id, q=q, since=parse_dt(since) if since else None,
            is_vacancy=is_vacancy, include_duplicates=include_duplicates, limit=limit, page_token=page_token)
        return {
            "items": [{
                "vacancy_id": v.vacancy_id, "platform": v.platform, "source_id": v.source_id, "title": v.title,
                "company": v.company, "location": v.location, "salary": v.salary.model_dump() if v.salary else None,
                "published_at": v.published_at, "url": v.url, "completeness": v.completeness,
                "duplicate_status": v.duplicate_status, "preview": v.text[:300],
            } for v in items],
            "next_page_token": token,
        }

    @mcp.tool()
    def get_vacancy(vacancy_id: str, candidate_id: str = default_candidate) -> dict[str, Any]:
        """Full vacancy with all origins (sources it was seen in), version history and related duplicates."""
        details = core.store.vacancy_details(candidate_id, vacancy_id)
        if details is None:
            raise ValueError(f"vacancy {vacancy_id} not found")
        return details

    @mcp.tool()
    def fetch_full_vacancy(vacancy_id: str, candidate_id: str = default_candidate) -> dict[str, Any]:
        """Download the full description for a vacancy that only has a search snippet (HH, LinkedIn)."""
        return core.fetch_full_vacancy(candidate_id, vacancy_id).model_dump(mode="json")

    return mcp


def main() -> None:
    build_server().run()
