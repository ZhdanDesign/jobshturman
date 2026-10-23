"""Synthetic fixtures. Markup mirrors the real pages structurally (checked 2026-10-09);
texts and contacts are invented."""

from __future__ import annotations

import base64
import html
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import httpx
import pytest
from hrs_collector.adapters import Page, SourceAdapter
from hrs_collector.adapters.base import make_vacancy
from hrs_collector.errors import SourceError
from hrs_collector.http import Http
from hrs_collector.models import SourceSpec
from hrs_collector.service import Collector
from hrs_collector.store import Store

T0 = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def tg_message(username: str, chat_c: int, msg_id: int, when: datetime, text_html: str, edited: bool = False) -> str:
    view = base64.b64encode(json.dumps({"c": chat_c, "p": msg_id, "t": 1}).encode()).decode().rstrip("=")
    meta = ("edited &nbsp;" if edited else "")
    return f"""
<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message js-widget_message" data-post="{username}/{msg_id}" data-view="{view}">
  <div class="tgme_widget_message_bubble">
    <div class="tgme_widget_message_text js-message_text" dir="auto">{text_html}</div>
    <div class="tgme_widget_message_footer"><span class="tgme_widget_message_meta">{meta}<a class="tgme_widget_message_date" href="https://t.me/{username}/{msg_id}"><time datetime="{when.isoformat()}" class="time">12:00</time></a></span></div>
  </div></div></div>"""


def tg_page(username: str, chat_c: int, messages: list[tuple[int, datetime, str, bool]], has_prev: bool,
            title: str = "Вакансии для руководителей") -> str:
    prev = f'<link rel="prev" href="/s/{username}?before={min(m[0] for m in messages)}">' if has_prev and messages else ""
    body = "".join(tg_message(username, chat_c, *m) for m in sorted(messages))
    return f"""<!DOCTYPE html><html><head>{prev}</head><body>
<div class="tgme_channel_info"><div class="tgme_channel_info_header_title"><span dir="auto">{title}</span></div></div>
<section class="tgme_channel_history">{body}</section></body></html>"""


def hh_search_page(vacancies: list[dict], next_page: int | None, total: int) -> str:
    state = {"vacancySearchResult": {"vacancies": vacancies, "totalResults": total,
                                     "paging": {"next": {"page": next_page, "disabled": False}} if next_page is not None else {"next": None}}}
    return f'<html><body><template style="display:none" id="HH-Lux-InitialState">{html.escape(json.dumps(state, ensure_ascii=False))}</template></body></html>'


def hh_vacancy(vid: int, name: str, when: datetime, company: str = "ООО Пример") -> dict:
    return {"vacancyId": vid, "name": name, "company": {"id": 1, "name": company, "visibleName": company},
            "compensation": {"from": 500000, "to": 700000, "currencyCode": "RUR", "gross": False, "mode": "MONTH"},
            "publicationTime": {"$": when.isoformat()}, "area": {"@id": 1, "name": "Москва"},
            "workFormats": [{"workFormatsElement": ["HYBRID"]}], "employmentForm": "FULL"}


@dataclass
class FakeMessage:
    id: int
    at: datetime
    text: str


class FakeChannel:
    """In-memory channel; newest-first pages like t.me/s."""

    def __init__(self, n: int, start: datetime, step: timedelta, page_size: int = 20, text=lambda i: f"Вакансия {i}: требования и условия"):
        self.page_size = page_size
        self.messages = {i: FakeMessage(i, start + step * (i - 1), text(i)) for i in range(1, n + 1)}
        self.reads = 0
        self.fail_on_read: int | None = None

    def add(self, at: datetime, text: str) -> int:
        i = max(self.messages, default=0) + 1
        self.messages[i] = FakeMessage(i, at, text)
        return i


class FakeAdapter(SourceAdapter):
    platform = "telegram"
    transport = "telegram_web"
    version = "fake/1"
    channels: ClassVar[dict[str, FakeChannel]] = {}

    def read_page(self, spec: SourceSpec, token: str | None, since=None) -> Page:
        ch = self.channels[spec.config["username"]]
        ch.reads += 1
        if ch.fail_on_read is not None and ch.reads >= ch.fail_on_read:
            raise SourceError("service_unreachable", "simulated network failure")
        before = int(token) if token else 10**9
        ids = sorted((i for i in ch.messages if i < before), reverse=True)[: ch.page_size]
        items = [self._item(spec, ch.messages[i]) for i in ids]
        older = ids and min(ids) > min(ch.messages)
        return Page(items=items, next_token=str(min(ids)) if older else None,
                    source_meta={"stable_chat_id": "777", "current_title": "Fake"})

    def _item(self, spec, m: FakeMessage):
        return make_vacancy(platform="telegram", item_key=f"telegram:{spec.config['username']}:{m.id}",
                            external_id=str(m.id), text=m.text, published_at=m.at, completeness="full")

    def fetch_full(self, spec, external_id):
        ch = self.channels[spec.config["username"]]
        return self._item(spec, ch.messages[int(external_id)])


@pytest.fixture
def fake_adapters(monkeypatch):
    import hrs_collector.service as service_mod

    FakeAdapter.channels = {}

    def make(transport, http, credentials=None):
        if transport == "telegram_web":
            return FakeAdapter(http, credentials)
        raise SourceError("not_configured", f"no fake for {transport}", can_retry=False)

    monkeypatch.setattr(service_mod, "make_adapter", make)
    return FakeAdapter.channels


@pytest.fixture
def collector(tmp_path, monkeypatch):
    monkeypatch.delenv("HRS_HH_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("HRS_PACE_FACTOR", "0")
    return Collector(tmp_path, store=Store(tmp_path / "c.sqlite3"))


def mock_http(routes: dict[str, httpx.Response | callable]):
    """Http whose requests are answered from routes keyed by 'host/path'."""

    def handler(request: httpx.Request) -> httpx.Response:
        key = f"{request.url.host}{request.url.path}"
        target = routes.get(key)
        if target is None:
            return httpx.Response(404, text="not found")
        return target(request) if callable(target) else target

    return lambda: Http(transport=httpx.MockTransport(handler), retries=0)
