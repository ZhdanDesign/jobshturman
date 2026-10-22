"""What the agent reads first fits the agents' limit: Claude Code cuts server instructions and tool descriptions at
about 2 KB, so the must-not-miss rules are short and first, and the rest comes from the work_guide tool."""
import asyncio

from hrs_cli.server import build

LIMIT = 2000


def test_instructions_fit_and_put_the_licence_first():
    text = build().instructions
    assert len(text) <= LIMIT, len(text)
    assert text.index("запрещено лицензией") < 300
    for name in ("methodology_download", "license_status", "agent_rules", "work_guide", "status_report"):
        assert name in text


def test_the_details_come_from_work_guide_and_every_tool_fits():
    server = build()
    tools = asyncio.run(server.list_tools())
    assert all(len(t.description or "") <= 2048 for t in tools), [t.name for t in tools if len(t.description) > 2048]
    guide = asyncio.run(server.call_tool("work_guide", {})).content[0].text
    for name in ("intake_ask", "vacancies_ranked", "application_send_start", "ui_mode", "methodology_update",
                 "schedule_set", "ask_wake_hours", "document_import", "product_help", "plan_check"):
        assert name in guide, name
