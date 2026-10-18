"""The candidate's page: one-time link, localhost only, same answers as the chat, no methodology text."""
import asyncio
import http.client
import json
from urllib.parse import urlsplit

import pytest
from mcp import Client
from test_license import issue, keys  # noqa: F401 (fixture)

from hrs_cli import license, paths, web
from hrs_cli.server import APP_MIME, APP_URI, build


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HRSHTURMAN_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HRSHTURMAN_WORKSPACE", raising=False)
    return tmp_path / "home"


class Browser:
    def __init__(self, url):
        parts = urlsplit(url)
        self.port, self.cookie = parts.port, None
        self.first = f"{parts.path}?{parts.query}"

    def request(self, method, path, body=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        if body is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        resp = conn.getresponse()
        data = resp.read()
        if resp.getheader("Set-Cookie"):
            self.cookie = resp.getheader("Set-Cookie").split(";")[0]
        try:
            return resp.status, json.loads(data)
        except ValueError:
            return resp.status, data.decode("utf-8", "replace")


def run(coro):
    return asyncio.run(coro)


def tool(c, name, **args):
    async def go():
        return json.loads((await c.call_tool(name, args)).content[0].text)
    return go()


def test_one_time_link_cookie_and_host_check(home):
    screen = web.WebUI(*_store())
    try:
        b = Browser(screen.link())
        assert b.request("GET", "/api/state")[0] == 401  # no session yet
        status, _ = b.request("GET", b.first)
        assert status == 303 and b.cookie
        assert b.request("GET", "/app")[1].startswith("<!doctype html>")
        assert "/*TOKENS*/" not in b.request("GET", "/app")[1]
        stranger = Browser(f"http://127.0.0.1:{b.port}{b.first}")
        assert stranger.request("GET", stranger.first)[0] == 401  # the token is spent
        assert b.request("GET", "/api/state", host=f"evil.test:{b.port}")[0] == 403
        assert b.request("POST", "/api/answer", body=None)[0] == 415
    finally:
        screen.stop()


def _store():
    from hrshturman_core import Store
    from hrshturman_core.workspace import create_candidate
    s = paths.settings()
    store = Store(s.db_path).init()
    create_candidate(store, s.candidate_id, s.timezone, actor="installer")
    return store, s.candidate_id


def test_browser_and_chat_share_the_answers(home):
    async def go():
        async with Client(build()) as c:
            link = (await tool(c, "ui_open", view="intake"))["url"]
            b = Browser(link)
            b.request("GET", b.first)
            status, state = b.request("POST", "/api/answer", {"block": "pains", "answer": [2, "свой: нет времени"]})
            assert status == 200 and state["intake"]["pain_ids"] == [2]
            seen_by_agent = await tool(c, "intake_get")
            await tool(c, "intake_answer", block="situation_goal", answer={"target_roles": "Коммерческий директор"})
            _, from_chat = b.request("GET", "/api/state")
            _, done = b.request("POST", "/api/confirm", {"expected_version": from_chat["intake"]["version"]})
            return seen_by_agent, from_chat, done
    seen_by_agent, from_chat, done = run(go())
    intake = seen_by_agent.get("intake", seen_by_agent)
    assert intake["pain_ids"] == [2] and intake["custom_pain"] == "нет времени"
    assert from_chat["intake"]["situation"]["target_roles"] == "Коммерческий директор"
    assert done["intake"]["status"] == "confirmed"
    assert done["progress"][0] == {"stage": 0, "title": "Диагностика", "group": "Подготовка", "status": "done",
                                   "count": None}
    assert len(done["progress"]) == 26


def test_mode_is_asked_once_and_kept(home):
    async def go():
        async with Client(build()) as c:
            before = await tool(c, "ui_mode")
            after = await tool(c, "ui_mode", mode="builtin_browser")
            bad = await c.call_tool("ui_mode", {"mode": "tv"})
            return before, after, bad
    before, after, bad = run(go())
    assert before["ui_mode"] is None and after["ui_mode"] == "builtin_browser" and bad.is_error
    assert web.get_mode() == "builtin_browser"


def test_mcp_app_resource_and_tools(home):
    async def go():
        async with Client(build()) as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
            page = (await c.read_resource(APP_URI)).contents[0]
            shown = await tool(c, "ui_show")
            answered = await tool(c, "ui_answer", block="pains", answer="пропустить")
            return tools, page, shown, answered
    tools, page, shown, answered = run(go())
    assert tools["ui_show"].meta == {"ui": {"resourceUri": APP_URI}}
    assert all(tools[n].meta == {"ui": {"visibility": ["app"]}} for n in ("ui_state", "ui_answer", "ui_confirm"))
    assert page.mime_type == APP_MIME and "ui/initialize" in page.text and "tools/call" in page.text
    assert {"intake", "blocks", "progress", "ui_mode"} <= set(shown)
    assert answered["intake"]["skipped"] is True


def test_the_page_never_carries_methodology_text(keys):  # noqa: F811
    license.login(issue(keys)["key"], http=keys)
    screen = web.WebUI(*_store())
    try:
        b = Browser(screen.link("progress"))
        b.request("GET", b.first)
        everything = json.dumps(b.request("GET", "/api/state")[1], ensure_ascii=False) + b.request("GET", "/app")[1]
    finally:
        screen.stop()
    for phrase in ("Мастер-резюме", "Сначала выясни", "stage-07-master-cv", "procedure.md"):
        assert phrase not in everything
