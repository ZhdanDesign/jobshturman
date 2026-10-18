from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..errors import SourceError
from ..http import Http
from ..models import (
    CapabilityLevels,
    Credentials,
    ProbeResult,
    Salary,
    SourceSpec,
    VacancyData,
)
from ..util import extract_contacts, fingerprint, guess_is_vacancy, sha256, utcnow


@dataclass
class Page:
    """One page of a source, newest-first navigation.

    next_token loads the next OLDER page; None means the beginning of history
    (or the end of search results) was reached.
    """

    items: list[VacancyData]
    next_token: str | None
    source_meta: dict[str, Any] = field(default_factory=dict)


class SourceAdapter(ABC):
    platform: str
    transport: str
    version: str
    supported_operations: list[str] = ["probe_capabilities", "collect_batch", "fetch_full_item"]
    limits: list[str] = []

    def __init__(self, http: Http, credentials: Credentials | None = None) -> None:
        self.http = http
        self.credentials = credentials

    # False when the source cannot sort by date (LinkedIn guest): the engine then
    # stops only at the end of results and passes `since` so the adapter can
    # filter by time on the platform side.
    date_ordered: bool = True

    @abstractmethod
    def read_page(self, spec: SourceSpec, token: str | None, since: datetime | None = None) -> Page: ...

    @abstractmethod
    def fetch_full(self, spec: SourceSpec, external_id: str) -> VacancyData: ...

    def probe(self, spec: SourceSpec) -> ProbeResult:
        """Default probe: read the newest page and report what happened."""
        try:
            self.check_config(spec)
        except SourceError as exc:
            return self._probe_result("error", CapabilityLevels(configured=False), "unknown", error=exc)
        try:
            page = self.read_page(spec, None)
        except SourceError as exc:
            reachable = exc.code not in {"service_unreachable"}
            access = {
                "not_authenticated": "not_authenticated",
                "private_invite_required": "private_invite_required",
                "web_preview_unavailable": "read_failed",
                "unavailable": "unavailable",
            }.get(exc.code, "read_failed")
            return self._probe_result(
                "error",
                CapabilityLevels(configured=True, service_reachable=reachable, scenario_verified=False),
                access,
                error=exc,
            )
        return self._probe_result(
            "ok",
            CapabilityLevels(
                configured=True,
                service_reachable=True,
                account_authorized=self.account_authorized(),
                scenario_verified=True,
            ),
            "accessible",
            evidence={"items_on_first_page": len(page.items), "has_older_page": page.next_token is not None},
            source_meta=page.source_meta,
        )

    def check_config(self, spec: SourceSpec) -> None:
        if spec.platform != self.platform or spec.transport != self.transport:
            raise SourceError("not_configured", f"spec is {spec.platform}/{spec.transport}, adapter is {self.platform}/{self.transport}")

    def account_authorized(self) -> bool | None:
        return None  # anonymous transports have no account

    def close(self) -> None:
        pass

    def _probe_result(self, status, levels, access, *, error: SourceError | None = None,
                      evidence: dict | None = None, source_meta: dict | None = None) -> ProbeResult:
        return ProbeResult(
            platform=self.platform,  # type: ignore[arg-type]
            transport=self.transport,  # type: ignore[arg-type]
            adapter_version=self.version,
            observed_at=utcnow(),
            status=status,
            levels=levels,
            access_status=access,
            supported_operations=self.supported_operations,
            limits=self.limits,
            evidence=evidence or {},
            source_meta=source_meta or {},
            error=error.info() if error else None,
        )


def make_vacancy(
    *,
    platform: str,
    item_key: str,
    external_id: str,
    text: str,
    url: str | None = None,
    title: str | None = None,
    company: str | None = None,
    location: str | None = None,
    salary: Salary | None = None,
    employment: str | None = None,
    work_format: list[str] | None = None,
    published_at: datetime | None = None,
    edited_at: datetime | None = None,
    completeness: str = "snippet",
    links: list[str] | None = None,
    is_vacancy: str | None = None,
    raw: dict[str, Any] | None = None,
) -> VacancyData:
    basis = "\n".join(x for x in (title, company, text) if x)
    payload = {
        "title": title, "company": company, "location": location, "text": text,
        "salary": salary.model_dump() if salary else None, "employment": employment,
        "edited_at": edited_at.isoformat() if edited_at else None,
    }
    return VacancyData(
        item_key=item_key,
        platform=platform,  # type: ignore[arg-type]
        external_id=external_id,
        url=url,
        title=title,
        company=company,
        location=location,
        salary=salary,
        employment=employment,
        work_format=work_format or [],
        published_at=published_at,
        edited_at=edited_at,
        text=text,
        contacts=extract_contacts(text, links),
        is_vacancy=is_vacancy or guess_is_vacancy(basis),  # type: ignore[arg-type]
        completeness=completeness,  # type: ignore[arg-type]
        content_hash=sha256(repr(sorted(payload.items()))),
        fingerprint=fingerprint(basis),
        raw=raw or {},
    )
