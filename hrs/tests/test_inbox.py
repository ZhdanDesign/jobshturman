"""Answering employers in the person's own Telegram and mail: only fresh threads, only messages that need an action."""
from test_web import home, run, tool  # noqa: F401 (fixture)
from mcp import Client

from hrs_cli import web
from hrs_cli.server import build


def test_rules_keep_old_threads_and_small_talk_out(home):
    async def go():
        async with Client(build()) as c:
            first = await tool(c, "inbox_rules")
            changed = await tool(c, "inbox_rules", max_age_days=7)
            bad = await c.call_tool("inbox_rules", {"max_age_days": 0})
            return first, changed, bad
    first, changed, bad = run(go())
    assert first["max_age_days"] == 14 and changed["max_age_days"] == 7 and bad.is_error
    rules = first["rules"]
    for words in ("старше max_age_days", "просто разговор не отвечай", "не больше одного ответа", "явного «да»",
                  "новых диалогов сам не начинай", "В плановом пробуждении"):
        assert words in rules, words


def test_the_setting_lives_next_to_ui_mode(home):
    async def go():
        async with Client(build()) as c:
            await tool(c, "ui_mode", mode="chat")
            await tool(c, "inbox_rules", max_age_days=30)
            return await tool(c, "ui_mode")
    assert run(go())["ui_mode"] == "chat"
    assert web.get_mode() == "chat"


def test_the_agent_is_told_before_it_touches_the_inbox(home):
    server = build()
    assert "inbox_rules" in server.instructions
    async def go():
        async with Client(server) as c:
            return (await c.call_tool("work_guide", {})).content[0].text
    assert "inbox_rules(max_age_days=N)" in run(go())
