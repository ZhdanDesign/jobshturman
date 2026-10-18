"""collect_batch: the windowing/cursor algorithm shared by every adapter.

Guarantees (ТЗ target_channels §5, ТЗ §13 п.8):
- First run reads back to now - lookback_days.
- Later runs read back to the watermark, so any idle gap (5 days, a month) is
  closed from the stored point, never just "the last 72h"; then, budget
  permitting, overlap_hours further back to catch edits.
- page_budget bounds the work. When it runs out, the unfinished range is kept as
  resume_* and the watermark does not move; the next call finishes that range
  first, then reads the fresh top.
- on_page(items, cursor) is called after every page with the cursor that is
  valid once those items are persisted. A stateful caller stores both in one
  transaction; a crash between pages loses nothing and duplicates nothing,
  because items are keyed by item_key and re-reading is idempotent.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from .adapters import Page, SourceAdapter
from .errors import SourceError
from .models import BatchResult, CollectParams, Coverage, CursorState, SourceSpec, VacancyData
from .util import as_utc, utcnow

OnPage = Callable[[list[VacancyData], CursorState], None]


def _bounds(items: list[VacancyData]) -> tuple[datetime | None, datetime | None, str | None]:
    # Promoted items (hh.ru ads) sit at the top regardless of date; they do not mark the stop line.
    dated = [i for i in items if i.published_at is not None and not i.raw.get("promoted")]
    if not dated:
        return None, None, None
    newest = max(dated, key=lambda i: i.published_at)  # type: ignore[arg-type,return-value]
    oldest = min(i.published_at for i in dated)  # type: ignore[type-var]
    return as_utc(oldest), as_utc(newest.published_at), newest.external_id


def collect_batch(
    adapter: SourceAdapter,
    spec: SourceSpec,
    cursor: CursorState | None,
    params: CollectParams,
    on_page: OnPage | None = None,
) -> BatchResult:
    now = as_utc(params.now) or utcnow()
    cur = (cursor or CursorState()).model_copy(deep=True)
    collected: list[VacancyData] = []
    pages = 0
    meta: dict = {}
    cov_from: datetime | None = None
    cov_to: datetime | None = None

    ordered = adapter.date_ordered

    def read(token: str | None, since: datetime | None) -> Page:
        nonlocal pages, cov_from, cov_to
        page = adapter.read_page(spec, token, since)
        pages += 1
        meta.update({k: v for k, v in page.source_meta.items() if v is not None})
        if params.fetch_full:
            page.items = [_full(adapter, spec, it) for it in page.items]
        oldest, newest, _ = _bounds(page.items)
        if not ordered:
            oldest = as_utc(since) if since and page.next_token is None else oldest
        if oldest and (cov_from is None or oldest < cov_from):
            cov_from = oldest
        if newest and (cov_to is None or newest > cov_to):
            cov_to = newest
        return page

    def emit(page: Page, state: CursorState) -> None:
        if on_page is not None:
            on_page(page.items, state.model_copy(deep=True))
        collected.extend(page.items)

    def result(status: str, complete: bool, reason: str | None = None, error: SourceError | None = None) -> BatchResult:
        if status == "ok" and not collected:
            status = "no_new"
        return BatchResult(
            platform=adapter.platform,  # type: ignore[arg-type]
            transport=adapter.transport,  # type: ignore[arg-type]
            adapter_version=adapter.version,
            observed_at=utcnow(),
            status=status,  # type: ignore[arg-type]
            items=collected,
            next_cursor=cur,
            coverage=Coverage(from_at=cov_from, to_at=cov_to, complete=complete, pages=pages,
                              items_seen=len(collected), reason=reason),
            source_meta=meta,
            evidence={"pages": pages},
            error=error.info() if error else None,
        )

    try:
        # Phase 1: finish an unfinished range from a previous call.
        if cur.resume_token:
            token: str | None = cur.resume_token
            while True:
                if pages >= params.page_budget:
                    return result("partial", False, "page_budget: resumed range not finished")
                page = read(token, cur.resume_stop_at)
                oldest, _, _ = _bounds(page.items) if ordered else (None, None, None)
                done = page.next_token is None or (oldest is not None and cur.resume_stop_at is not None and oldest <= as_utc(cur.resume_stop_at))
                if done:
                    if page.next_token is None:
                        cur.history_complete = True
                    _advance(cur, cur.resume_top_at, cur.resume_top_id)
                    _clear_resume(cur)
                else:
                    cur.resume_token = page.next_token
                emit(page, cur)
                if done:
                    break
                token = page.next_token

        # Phase 2: read the fresh top. Two stop lines:
        #   hard = watermark (first run: now - lookback). Reaching it makes the range
        #          contiguous, so the watermark moves; until then the rest is a resume range.
        #   soft = watermark - overlap. Re-reading recent posts for edits is best effort:
        #          it never blocks progress when the budget is smaller than the overlap.
        hard = as_utc(cur.watermark_at) if cur.watermark_at else now - timedelta(days=params.lookback_days)
        soft = hard - timedelta(hours=params.overlap_hours) if cur.watermark_at else hard
        token = None
        top_at: datetime | None = None
        top_id: str | None = None
        first = True
        covered = False
        while True:
            if pages >= params.page_budget:
                if covered:
                    return result("ok", True, "page_budget: overlap re-check truncated")
                return result("partial", False, "page_budget: fresh range not started")
            page = read(token, soft)
            oldest, newest, newest_id = _bounds(page.items)
            if not ordered:
                oldest = None  # unordered results: only the end of results ends the window
            if first:
                top_at, top_id = (newest, newest_id) if newest and ordered else (now, None)
                first = False
            at_end = page.next_token is None
            if not covered and (at_end or (oldest is not None and oldest <= hard)):
                covered = True
                if at_end:
                    cur.history_complete = True
                _advance(cur, top_at, top_id)
                _clear_resume(cur)
            if covered:
                emit(page, cur)
                if at_end or (oldest is not None and oldest < soft):
                    return result("ok", True)
            else:
                cur.resume_token = page.next_token
                cur.resume_stop_at = hard
                cur.resume_top_at = top_at
                cur.resume_top_id = top_id
                emit(page, cur)
                if pages >= params.page_budget:
                    return result("partial", False, "page_budget: older pages remain in resume range")
            token = page.next_token
    except SourceError as exc:
        return result("error", False, exc.code, exc)


def _advance(cur: CursorState, at: datetime | None, item_id: str | None) -> None:
    if at is None:
        return
    if cur.watermark_at is None or at > as_utc(cur.watermark_at):
        cur.watermark_at = at
        cur.watermark_id = item_id


def _clear_resume(cur: CursorState) -> None:
    cur.resume_token = None
    cur.resume_stop_at = None
    cur.resume_top_at = None
    cur.resume_top_id = None


def _full(adapter: SourceAdapter, spec: SourceSpec, item: VacancyData) -> VacancyData:
    if item.completeness == "full":
        return item
    try:
        full = adapter.fetch_full(spec, item.external_id)
    except SourceError as exc:
        item.raw = {**item.raw, "fetch_full_error": exc.code}
        return item
    return merge_full(item, full)


def merge_full(snippet: VacancyData, full: VacancyData) -> VacancyData:
    """Full fetch wins, but keeps snippet fields the detail page lacks (e.g. LinkedIn date)."""
    data = full.model_dump()
    for key, value in snippet.model_dump().items():
        if data.get(key) in (None, [], {}, "") and value not in (None, [], {}, ""):
            data[key] = value
    data["raw"] = {**snippet.raw, **full.raw}
    return VacancyData.model_validate(data)
