"""hrs schedule: tasks in the system scheduler, runs without the agent, MCP tools for the agent, product help."""
import asyncio
import json
import plistlib
from datetime import datetime, timezone

import httpx
from collector_fixtures import T0, mock_http, tg_page  # loaded in conftest.py
from hrs_cli import paths, schedule
from hrs_cli.main import main
from hrs_cli.server import build
from hrs_collector.service import Collector
from hrs_collector.store import Store as CollectorStore
from hrshturman_core import applications as apps
from hrshturman_core import schedule as core

NOON = datetime(2026, 10, 20, 11, 0, tzinfo=timezone.utc)  # 14:00 in Moscow, outside quiet hours
POST = "<b>Вакансия: Финансовый директор (CFO)</b><br/>Москва, гибрид. Резюме: cv@example.com"


def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HRSHTURMAN_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HRSHTURMAN_WORKSPACE", raising=False)
    monkeypatch.setenv("HRS_LAUNCH_AGENTS_DIR", str(tmp_path / "LaunchAgents"))
    monkeypatch.setenv("HRS_SYSTEMD_USER_DIR", str(tmp_path / "systemd"))
    monkeypatch.setenv("HRS_NOTIFY", "print")


def test_launchd_install_writes_agents_and_removes_disabled(tmp_path, monkeypatch):
    home(tmp_path, monkeypatch)
    calls = []
    out = schedule.install(platform="darwin", runner=lambda cmd: calls.append(cmd) or 0)
    plists = sorted((tmp_path / "LaunchAgents").glob("*.plist"))
    assert [p.name for p in plists] == [f"ru.owai.jobshturman.{t}.plist" for t in
                                        ("collect", "digest", "license", "reminders")]
    collect = plistlib.loads((tmp_path / "LaunchAgents" / "ru.owai.jobshturman.collect.plist").read_bytes())
    assert collect["ProgramArguments"][-4:] == ["hrs_cli", "schedule", "run", "collect"]
    assert collect["StartCalendarInterval"] == [{"Hour": 8, "Minute": 0}]
    assert collect["EnvironmentVariables"]["HRSHTURMAN_HOME"] == str(tmp_path / "home")
    assert ["launchctl", "bootstrap"] == calls[1][:2] and out["installed"]
    assert schedule.installed("darwin") == ["collect", "digest", "license", "reminders"]

    assert main(["schedule", "set", "reminders", "--off"]) == 0
    assert main(["schedule", "set", "collect", "--time", "9:15", "--time", "18:00"]) == 0
    schedule.install(platform="darwin", runner=lambda cmd: 0)
    assert schedule.installed("darwin") == ["collect", "digest", "license"]
    collect = plistlib.loads((tmp_path / "LaunchAgents" / "ru.owai.jobshturman.collect.plist").read_bytes())
    assert collect["StartCalendarInterval"] == [{"Hour": 9, "Minute": 15}, {"Hour": 18, "Minute": 0}]
    schedule.remove(platform="darwin", runner=lambda cmd: 0)
    assert schedule.installed("darwin") == []


def test_windows_and_linux_plans(tmp_path, monkeypatch):
    home(tmp_path, monkeypatch)
    s, store = schedule.store_for()
    settings = core.get_settings(store, s.candidate_id)
    win = {step["task"]: step["run"] for step in schedule.plan(settings, "win32")}
    create = [c for c in win["reminders"] if c[1] == "/Create"]
    assert [c[c.index("/ST") + 1] for c in create] == ["12:00", "16:00", "19:00"]
    assert "schedule run reminders" in create[0][-1]
    linux = {step["task"]: step for step in schedule.plan(settings, "linux")}
    timer = next(body for path, body in linux["digest"]["write"].items() if path.endswith(".timer"))
    assert "OnCalendar=*-*-* 08:30:00" in timer and "Persistent=true" in timer


def test_runs_collect_digest_and_reminders_without_the_agent(tmp_path, monkeypatch, capsys):
    home(tmp_path, monkeypatch)
    monkeypatch.setenv("HRS_PACE_FACTOR", "0")
    collector = Collector(tmp_path / "collector", store=CollectorStore(tmp_path / "collector.sqlite3"),
                          http_factory=mock_http({"t.me/s/execjobs": lambda r: httpx.Response(
                              200, text=tg_page("execjobs", -42, [(5, T0, POST, False)], has_prev=False))}))
    server = build(paths.settings(), collector)
    asyncio.run(server.call_tool("add_telegram_channel", {"source_id": "TG-M", "username": "@execjobs"}))

    collected = schedule.run("collect", collector=collector)
    assert collected["imported"]["added"] == 1
    monkeypatch.setattr(schedule, "_license_info", lambda: {})
    digest = schedule.run("digest")
    assert digest["counts"]["new"] == 1 and digest["notified"]
    assert "Сводка JobShturman" in (paths.settings().workspace / "digest.md").read_text(encoding="utf-8")
    reminded = schedule.run("reminders", now=NOON)
    assert reminded["reminder"] == "vacancies_unscored"
    assert "Новых вакансий без оценки: 1" in capsys.readouterr().out
    assert schedule.run("reminders", now=NOON)["reason"] == "nothing_new"
    status = schedule.status()
    assert set(status["last_runs"]) == {"collect", "digest", "reminders"}


def test_paused_candidate_gets_no_runs(tmp_path, monkeypatch):
    home(tmp_path, monkeypatch)
    from hrshturman_core import intake
    s, store = schedule.store_for()
    intake.set_paused(store, s.candidate_id, True)
    assert schedule.run("reminders", now=NOON)["skipped"] == "paused"


def test_agent_tools_and_help(tmp_path, monkeypatch):
    home(tmp_path, monkeypatch)
    server = build(paths.settings(), Collector(tmp_path / "collector"))
    s, store = schedule.store_for()
    apps.import_vacancies(store, s.candidate_id, [{"source": "hh", "external_id": "1", "title": "CFO", "text": "x"}])

    async def call(tool, **kw):
        result = await server.call_tool(tool, kw)
        assert not result.is_error, result.content[0].text
        data = result.structured_content
        if data is None:
            return json.loads(result.content[0].text)
        return data["result"] if set(data) == {"result"} else data

    async def go():
        names = {t.name for t in await server.list_tools()}
        assert {"schedule_status", "schedule_set", "schedule_install", "schedule_register", "reminders_list",
                "reminder_done", "reminder_snooze", "digest_get", "product_help"} <= names
        await call("schedule_register", agent="claude_code", name="Утренний разбор", cadence="ежедневно 09:00",
                   purpose="оценить новые вакансии")
        status = await call("schedule_status")
        reminders = await call("reminders_list")
        digest = await call("digest_get")
        contents = await call("product_help")
        where = await call("product_help", topic="где хранятся данные")
        return status, reminders, digest, contents, where

    status, reminders, digest, contents, where = asyncio.run(go())
    assert status["agent_tasks"][0]["name"] == "Утренний разбор"
    assert status["digest_file"].endswith("digest.md")
    assert [r["rule"] for r in reminders] == ["vacancies_unscored"]
    assert digest["counts"]["unscored"] == 1
    assert any("Расписание" in t for t in contents["topics"])
    assert "Application Support" in where["sections"][0]
