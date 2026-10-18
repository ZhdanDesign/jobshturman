"""Contract models. Everything that crosses the API/MCP boundary is defined here.

See CONTRACT.md for the human-readable description. Field names here are the contract.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr

Platform = Literal["hh", "telegram", "linkedin"]
Transport = Literal["hh_web", "hh_api", "telegram_web", "telegram_mtproto", "linkedin_guest"]
SourceKind = Literal["channel", "search"]

AccessStatus = Literal[
    "unknown",
    "accessible",
    "not_joined",
    "private_invite_required",
    "read_failed",
    "unavailable",
    "not_authenticated",
    "disabled",
]

ErrorCode = Literal[
    "not_configured",
    "service_unreachable",
    "not_authenticated",
    "captcha_required",
    "two_factor_required",
    "rate_limited",
    "unavailable",
    "private_invite_required",
    "web_preview_unavailable",
    "changed_page",
    "invalid_schema",
    "unsupported_environment",
    "incomplete_source",
    "lease_lost",
    "internal_error",
]


# ---------------------------------------------------------------- sources


class SourceSpec(BaseModel):
    """What to read. Stateless: carries no cursor and no secrets."""

    platform: Platform
    transport: Transport
    kind: SourceKind
    config: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "telegram: {username} or {chat_id}; hh: {text, area?, params?}; "
            "linkedin: {keywords, location?, params?}"
        ),
    )


class HHCredentials(BaseModel):
    access_token: SecretStr


class TelegramCredentials(BaseModel):
    api_id: int
    api_hash: SecretStr
    session_string: SecretStr


class Credentials(BaseModel):
    """Per-operation credentials. Used in memory for one call, never stored or logged."""

    hh: HHCredentials | None = None
    telegram: TelegramCredentials | None = None


class CursorState(BaseModel):
    """Opaque-to-caller collection cursor for one source.

    watermark_*: newest point up to which history is contiguously covered.
    resume_*: an unfinished backfill range (set when page_budget ran out).
    """

    version: int = 1
    watermark_at: datetime | None = None
    watermark_id: str | None = None
    resume_token: str | None = None
    resume_stop_at: datetime | None = None
    resume_top_at: datetime | None = None
    resume_top_id: str | None = None
    history_complete: bool = False


class CollectParams(BaseModel):
    lookback_days: int = Field(7, ge=1, le=365, description="First-run history window")
    overlap_hours: int = Field(72, ge=0, le=24 * 30, description="Re-check window behind watermark")
    page_budget: int = Field(10, ge=1, le=500)
    fetch_full: bool = Field(False, description="Fetch full text for items that only have a snippet")
    now: datetime | None = Field(None, description="Override clock (tests, replays)")


# ---------------------------------------------------------------- vacancy


class Salary(BaseModel):
    min: int | None = None
    max: int | None = None
    currency: str | None = None
    gross: bool | None = None
    period: str | None = None


class Contacts(BaseModel):
    emails: list[str] = Field(default_factory=list)
    telegram: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)


class VacancyData(BaseModel):
    """One collected publication normalized into vacancy fields.

    item_key is the stable identity of the publication inside a platform:
      hh:<vacancy_id>, telegram:<chat_id>:<message_id>, linkedin:<job_id>.
    """

    item_key: str
    platform: Platform
    external_id: str
    url: str | None = None
    title: str | None = None
    company: str | None = None
    location: str | None = None
    salary: Salary | None = None
    employment: str | None = None
    work_format: list[str] = Field(default_factory=list)
    published_at: datetime | None = None
    edited_at: datetime | None = None
    text: str = ""
    contacts: Contacts = Field(default_factory=Contacts)
    is_vacancy: Literal["likely", "unlikely", "unknown"] = "unknown"
    completeness: Literal["snippet", "full"] = "snippet"
    content_hash: str
    fingerprint: str
    raw: dict[str, Any] = Field(default_factory=dict)


class Vacancy(VacancyData):
    """VacancyData as stored by the stateful collector."""

    vacancy_id: str
    candidate_id: str
    source_id: str
    version: int
    status: Literal["collected", "normalized", "removed"] = "normalized"
    first_seen_at: datetime
    last_seen_at: datetime
    changed_at: datetime
    canonical_id: str
    duplicate_status: Literal["unique", "duplicate", "suspected_duplicate"] = "unique"


# ---------------------------------------------------------------- operation envelopes


class ErrorInfo(BaseModel):
    code: ErrorCode
    message: str
    stage: str
    can_retry: bool
    retry_after_seconds: int | None = None


class OpEnvelope(BaseModel):
    platform: Platform
    transport: Transport
    operation: str
    adapter_version: str
    observed_at: datetime
    status: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    error: ErrorInfo | None = None


class CapabilityLevels(BaseModel):
    configured: bool
    service_reachable: bool | None = None
    account_authorized: bool | None = None
    scenario_verified: bool | None = None


class ProbeResult(OpEnvelope):
    operation: Literal["probe_capabilities"] = "probe_capabilities"
    status: Literal["ok", "error"]
    levels: CapabilityLevels
    access_status: AccessStatus
    supported_operations: list[str]
    limits: list[str] = Field(default_factory=list)
    source_meta: dict[str, Any] = Field(default_factory=dict)


class Coverage(BaseModel):
    from_at: datetime | None = Field(None, description="Oldest covered publication time")
    to_at: datetime | None = Field(None, description="Newest covered publication time")
    complete: bool
    pages: int
    items_seen: int
    reason: str | None = Field(None, description="Why coverage is incomplete")


class BatchResult(OpEnvelope):
    operation: Literal["collect_batch"] = "collect_batch"
    status: Literal["ok", "partial", "no_new", "error"]
    items: list[VacancyData]
    next_cursor: CursorState
    coverage: Coverage
    source_meta: dict[str, Any] = Field(default_factory=dict)


class FetchResult(OpEnvelope):
    operation: Literal["fetch_full_item"] = "fetch_full_item"
    status: Literal["ok", "error"]
    item: VacancyData | None = None


# ---------------------------------------------------------------- stateful layer


class SourceIn(BaseModel):
    source_id: str = Field(pattern=r"^[A-Za-z0-9_.:\-]{1,80}$")
    spec: SourceSpec
    title: str | None = None
    category: str | None = None
    enabled: bool = True
    origin: str = "user"


class Source(SourceIn):
    candidate_id: str
    access_status: AccessStatus = "unknown"
    current_title: str | None = None
    stable_chat_id: str | None = None
    checked_at: datetime | None = None
    last_success_at: datetime | None = None
    next_retry_at: datetime | None = None
    fail_count: int = 0
    last_error_code: str | None = None
    last_error_message: str | None = None
    cursor: CursorState = Field(default_factory=CursorState)


class RunRequest(BaseModel):
    candidate_id: str = "default"
    source_ids: list[str] | None = Field(None, description="Default: all enabled sources")
    platforms: list[Platform] | None = None
    params: CollectParams = Field(default_factory=CollectParams)
    force: bool = Field(False, description="Ignore next_retry_at backoff")
    credentials: Credentials | None = None


class RunSourceReport(BaseModel):
    source_id: str
    status: Literal["ok", "partial", "no_new", "no_access", "error", "skipped", "pending"]
    pages: int = 0
    items_seen: int = 0
    items_new: int = 0
    items_updated: int = 0
    coverage: Coverage | None = None
    error: ErrorInfo | None = None


class Run(BaseModel):
    run_id: str
    candidate_id: str
    status: Literal["running", "ok", "partial", "error"]
    started_at: datetime
    finished_at: datetime | None = None
    params: CollectParams
    sources: list[RunSourceReport]
    totals: dict[str, int] = Field(default_factory=dict)


class VacancyPage(BaseModel):
    items: list[Vacancy]
    next_page_token: str | None = None
