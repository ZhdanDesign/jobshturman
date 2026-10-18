from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import T0, hh_search_page, hh_vacancy, mock_http, tg_page
from hrs_collector.adapters.hh import HHApiAdapter, HHWebAdapter
from hrs_collector.adapters.linkedin import LinkedInGuestAdapter
from hrs_collector.adapters.telegram_web import TelegramWebAdapter
from hrs_collector.api import create_app
from hrs_collector.errors import SourceError
from hrs_collector.models import SourceSpec
from hrs_collector.service import Collector
from hrs_collector.store import Store

TG = SourceSpec(platform="telegram", transport="telegram_web", kind="channel", config={"username": "execjobs"})
HH = SourceSpec(platform="hh", transport="hh_web", kind="search", config={"text": "финансовый директор", "area": "1"})
LI = SourceSpec(platform="linkedin", transport="linkedin_guest", kind="search", config={"keywords": "CFO", "location": "Dubai"})

POST = ("<b>Вакансия: Финансовый директор (CFO)</b><br/><br/><b>Компания:</b> Холдинг Пример<br/>"
        "<b>Локация:</b> Москва, гибрид<br/>Требования: 10+ лет. Резюме: cv@example.com, @hr_example")


def test_telegram_web_parses_posts_ids_and_pagination(monkeypatch):
    monkeypatch.setenv("HRS_PACE_FACTOR", "0")
    page_html = tg_page("execjobs", -2459163270, [(101, T0, POST, True), (100, T0 - timedelta(hours=1), "Реклама курса", False)], has_prev=True)
    http = mock_http({"t.me/s/execjobs": httpx.Response(200, text=page_html)})()
    page = TelegramWebAdapter(http).read_page(TG, None)
    assert page.next_token == "100"
    assert page.source_meta == {"current_username": "execjobs", "current_title": "Вакансии для руководителей", "stable_chat_id": "2459163270"}
    post = next(i for i in page.items if i.external_id == "101")
    assert post.item_key == "telegram:2459163270:101"
    assert post.title == "Финансовый директор (CFO)" and post.company == "Холдинг Пример" and post.location == "Москва, гибрид"
    assert post.contacts.emails == ["cv@example.com"] and "hr_example" in post.contacts.telegram
    assert post.raw["edited"] is True and post.is_vacancy == "likely" and post.completeness == "full"
    assert next(i for i in page.items if i.external_id == "100").is_vacancy == "unlikely"


def test_telegram_web_without_preview_reports_web_preview_unavailable(monkeypatch):
    monkeypatch.setenv("HRS_PACE_FACTOR", "0")
    routes = {
        "t.me/s/closedchat": httpx.Response(302, headers={"Location": "https://t.me/closedchat"}),
        "t.me/closedchat": httpx.Response(200, text='<div class="tgme_page_title"><span>Closed chat</span></div>'),
    }
    with pytest.raises(SourceError) as exc:
        TelegramWebAdapter(mock_http(routes)()).read_page(SourceSpec(platform="telegram", transport="telegram_web", kind="channel", config={"username": "closedchat"}), None)
    assert exc.value.code == "web_preview_unavailable" and not exc.value.can_retry


def test_hh_web_reads_embedded_state(monkeypatch):
    monkeypatch.setenv("HRS_PACE_FACTOR", "0")
    seen = {}

    def search(request: httpx.Request):
        seen.update(dict(request.url.params))
        return httpx.Response(200, text=hh_search_page([hh_vacancy(1001, "Финансовый директор", T0)], next_page=1, total=120))

    page = HHWebAdapter(mock_http({"hh.ru/search/vacancy": search})()).read_page(HH, None)
    assert seen["order_by"] == "publication_time" and seen["area"] == "1" and seen["page"] == "0"
    item = page.items[0]
    assert item.item_key == "hh:1001" and item.url == "https://hh.ru/vacancy/1001"
    assert item.salary.min == 500000 and item.salary.period == "month" and item.work_format == ["HYBRID"]
    assert item.completeness == "snippet" and page.next_token == "1"


def test_hh_web_captcha_is_reported(monkeypatch):
    monkeypatch.setenv("HRS_PACE_FACTOR", "0")
    routes = {"hh.ru/search/vacancy": httpx.Response(302, headers={"Location": "https://hh.ru/account/captcha"}),
              "hh.ru/account/captcha": httpx.Response(200, text="<html>captcha</html>")}
    with pytest.raises(SourceError) as exc:
        HHWebAdapter(mock_http(routes)()).read_page(HH, None)
    assert exc.value.code == "captcha_required"


def test_hh_api_without_token_is_not_configured():
    with pytest.raises(SourceError) as exc:
        HHApiAdapter(mock_http({})()).read_page(SourceSpec(platform="hh", transport="hh_api", kind="search", config={"text": "CFO"}), None)
    assert exc.value.code == "not_configured"


def test_linkedin_cards(monkeypatch):
    monkeypatch.setenv("HRS_PACE_FACTOR", "0")
    cards = "".join(f"""<li><div class="base-card base-search-card job-search-card" data-entity-urn="urn:li:jobPosting:{4470000000 + i}">
      <h3 class="base-search-card__title"> Chief Financial Officer {i} </h3>
      <h4 class="base-search-card__subtitle"><a>Example Group</a></h4>
      <span class="job-search-card__location">Dubai, United Arab Emirates</span>
      <time class="job-search-card__listdate" datetime="2026-10-0{i + 1}">1 day ago</time></div></li>""" for i in range(3))
    page = LinkedInGuestAdapter(mock_http({"www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search":
                                           httpx.Response(200, text=cards)})()).read_page(LI, None)
    assert [i.item_key for i in page.items] == ["linkedin:4470000000", "linkedin:4470000001", "linkedin:4470000002"]
    assert page.items[0].company == "Example Group" and page.items[0].published_at.isoformat() == "2026-10-01T00:00:00+00:00"
    assert page.next_token == "3"


def _api(tmp_path, monkeypatch, token: str | None = None):
    monkeypatch.setenv("HRS_PACE_FACTOR", "0")
    if token:
        monkeypatch.setenv("HRS_COLLECTOR_TOKEN", token)
    else:
        monkeypatch.delenv("HRS_COLLECTOR_TOKEN", raising=False)
    pages = {
        None: tg_page("execjobs", -42, [(i, T0 - timedelta(hours=30 - i), POST.replace("CFO", f"CFO {i}"), False) for i in range(11, 31)], has_prev=True),
        "11": tg_page("execjobs", -42, [(i, T0 - timedelta(hours=30 - i), POST.replace("CFO", f"CFO {i}"), False) for i in range(1, 11)], has_prev=False),
    }
    http_factory = mock_http({"t.me/s/execjobs": lambda r: httpx.Response(200, text=pages[r.url.params.get("before")])})
    core = Collector(tmp_path, store=Store(tmp_path / "api.sqlite3"), http_factory=http_factory)
    return TestClient(create_app(core))


def test_api_requires_token_when_configured(tmp_path, monkeypatch):
    client = _api(tmp_path, monkeypatch, token="s3cret")
    assert client.get("/health").status_code == 200
    assert client.get("/v1/sources").status_code == 401
    assert client.get("/v1/sources", headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_api_stateless_collect_and_stateful_flow(tmp_path, monkeypatch):
    client = _api(tmp_path, monkeypatch)
    # stateless: caller holds cursor
    body = {"spec": TG.model_dump(), "params": {"lookback_days": 7, "page_budget": 1, "now": T0.isoformat()}}
    r1 = client.post("/v1/ops/collect", json=body).json()
    assert r1["status"] == "partial" and len(r1["items"]) == 20 and r1["next_cursor"]["resume_token"] == "11"
    # budget 1 finishes the resumed range but leaves the fresh top unread: still partial
    r2 = client.post("/v1/ops/collect", json=body | {"cursor": r1["next_cursor"]}).json()
    assert r2["status"] == "partial" and r2["next_cursor"]["watermark_at"].startswith("2026-10-09T12:00")
    body["params"]["page_budget"] = 2
    r2 = client.post("/v1/ops/collect", json=body | {"cursor": r1["next_cursor"]}).json()
    assert r2["status"] == "ok" and r2["coverage"]["complete"] and r2["next_cursor"]["watermark_at"].startswith("2026-10-09T12:00")

    # stateful
    assert client.post("/v1/sources", json={"source_id": "TG-X", "spec": TG.model_dump()}).status_code == 200
    run = client.post("/v1/runs?wait=true", json={"params": {"now": T0.isoformat(), "page_budget": 5}}).json()
    assert run["status"] == "ok" and run["totals"]["items_new"] == 30
    page = client.get("/v1/vacancies", params={"limit": 25, "q": "финансовый директор"}).json()
    assert len(page["items"]) == 25 and page["next_page_token"]
    rest = client.get("/v1/vacancies", params={"limit": 25, "q": "финансовый директор", "page_token": page["next_page_token"]}).json()
    assert len(rest["items"]) == 5 and rest["next_page_token"] is None
    ids = [v["vacancy_id"] for v in page["items"] + rest["items"]]
    assert len(set(ids)) == 30
    # deterministic: same query, same answer
    assert client.get("/v1/vacancies", params={"limit": 25, "q": "финансовый директор"}).json() == page
    detail = client.get(f"/v1/vacancies/{ids[0]}").json()
    assert detail["origins"][0]["source_id"] == "TG-X" and detail["versions"][0]["version"] == 1
    assert client.get("/v1/vacancies/vac_missing").status_code == 404


def test_mcp_tools_over_same_core(tmp_path, monkeypatch):
    import asyncio

    from hrs_collector.mcp_server import build_server

    monkeypatch.setenv("HRS_PACE_FACTOR", "0")
    server = build_server(Collector(tmp_path, store=Store(tmp_path / "api.sqlite3"),
                                    http_factory=mock_http({"t.me/s/execjobs": lambda r: httpx.Response(200, text=tg_page(
                                        "execjobs", -42, [(5, T0, POST, False)], has_prev=False))})))

    async def go():
        tools = {t.name for t in await server.list_tools()}
        assert {"list_sources", "run_collection", "search_vacancies", "get_vacancy", "probe_source"} <= tools
        await server.call_tool("add_telegram_channel", {"source_id": "TG-M", "username": "@execjobs"})
        report = await server.call_tool("run_collection", {"page_budget": 2})
        found = await server.call_tool("search_vacancies", {"q": "CFO"})
        return report, found

    report, found = asyncio.run(go())
    assert "TG-M" in str(report) and "Финансовый директор" in str(found)


def test_engine_ignores_promoted_items_and_unordered_sources_use_end_of_results(monkeypatch):
    from hrs_collector.adapters import Page
    from hrs_collector.adapters.base import make_vacancy
    from hrs_collector.engine import collect_batch
    from hrs_collector.models import CollectParams

    def item(i, at, promoted=False):
        return make_vacancy(platform="hh", item_key=f"hh:{i}", external_id=str(i), text=f"v{i}", published_at=at,
                            raw={"promoted": promoted})

    class Ads(HHWebAdapter):
        def read_page(self, spec, token, since=None):
            n = int(token or 0)
            items = ([item(999, T0 - timedelta(days=30), promoted=True)] if n == 0 else []) + \
                [item(n * 10 + k, T0 - timedelta(hours=n * 10 + k)) for k in range(10)]
            return Page(items=items, next_token=str(n + 1) if n < 20 else None)

    res = collect_batch(Ads(None), HH, None, CollectParams(now=T0, lookback_days=1, page_budget=10))
    assert res.status == "ok" and res.coverage.pages == 3  # the 30-day-old ad did not stop page 1

    seen_since = []

    class Unordered(LinkedInGuestAdapter):
        def read_page(self, spec, token, since=None):
            seen_since.append(since)
            n = int(token or 0)
            items = [make_vacancy(platform="linkedin", item_key=f"linkedin:{n}{k}", external_id=f"{n}{k}", text="x",
                                  published_at=T0 - timedelta(days=(k * 7) % 5)) for k in range(10)]
            return Page(items=items, next_token=str(n + 1) if n < 2 else None)

    res = collect_batch(Unordered(None), LI, None, CollectParams(now=T0, lookback_days=7, page_budget=10))
    assert res.status == "ok" and res.coverage.pages == 3 and res.next_cursor.watermark_at == T0
    assert seen_since[0] == T0 - timedelta(days=7)


def test_linkedin_end_of_results_is_not_changed_page(monkeypatch):
    monkeypatch.setenv("HRS_PACE_FACTOR", "0")
    end = "<!DOCTYPE html>\n\n<!---->  "  # real body past the last page, 2026-10-10
    page = LinkedInGuestAdapter(mock_http({"www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search":
                                           httpx.Response(200, text=end)})()).read_page(LI, "90")
    assert page.items == [] and page.next_token is None
    with pytest.raises(SourceError) as exc:
        LinkedInGuestAdapter(mock_http({"www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search":
                                        httpx.Response(200, text="<div class='other-layout'>x</div>")})()).read_page(LI, None)
    assert exc.value.code == "changed_page"


def test_hh_search_never_filters_by_salary():
    """Salary is checked after collection (vacancy_constraints): hh hides vacancies without a stated salary."""
    from hrs_collector.adapters.hh import _search_params
    spec = HH.model_copy(update={"config": HH.config | {"params": {"salary": 1200000, "only_with_salary": True,
                                                                  "experience": "moreThan6"}}})
    params = _search_params(spec)
    assert "salary" not in params and "only_with_salary" not in params
    assert params["experience"] == "moreThan6"
