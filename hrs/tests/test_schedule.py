"""hrs schedule: tasks in the system scheduler, runs without the agent, MCP tools for the agent, product help."""
import asyncio
import json
import os
import plistlib
import sys
from datetime import datetime, timezone

import httpx
import pytest
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
                                        ("collect", "digest", "license", "reminders", "wake", "watch")]
    collect = plistlib.loads((tmp_path / "LaunchAgents" / "ru.owai.jobshturman.collect.plist").read_bytes())
    assert collect["ProgramArguments"][-4:] == ["hrs_cli", "schedule", "run", "collect"]
    assert collect["StartCalendarInterval"] == [{"Hour": 8, "Minute": 0}]
    assert collect["EnvironmentVariables"]["HRSHTURMAN_HOME"] == str(tmp_path / "home")
    assert ["launchctl", "bootstrap"] == calls[1][:2] and out["installed"]
    assert schedule.installed("darwin") == ["collect", "digest", "license", "reminders", "wake", "watch"]
    wake = plistlib.loads((tmp_path / "LaunchAgents" / "ru.owai.jobshturman.wake.plist").read_bytes())
    assert wake["StartCalendarInterval"] == [{"Hour": 9, "Minute": 30}, {"Hour": 14, "Minute": 0},
                                             {"Hour": 19, "Minute": 30}]
    watch = plistlib.loads((tmp_path / "LaunchAgents" / "ru.owai.jobshturman.watch.plist").read_bytes())
    assert watch["StartInterval"] == 3600 and watch["RunAtLoad"] and "StartCalendarInterval" not in watch

    assert main(["schedule", "set", "reminders", "--off"]) == 0
    assert main(["schedule", "set", "collect", "--time", "9:15", "--time", "18:00"]) == 0
    schedule.install(platform="darwin", runner=lambda cmd: 0)
    assert schedule.installed("darwin") == ["collect", "digest", "license", "wake", "watch"]
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
    hourly = [c for c in win["watch"] if c[1] == "/Create"]
    assert [c[c.index("/SC") + 1] for c in hourly] == ["HOURLY", "ONLOGON"]
    watch = next(body for path, body in linux["watch"]["write"].items() if path.endswith(".timer"))
    assert "OnBootSec=5min" in watch and "OnUnitActiveSec=60min" in watch and "OnCalendar" not in watch


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


def test_the_schedule_switches_itself_on_and_puts_back_what_went_missing(tmp_path, monkeypatch):
    """Acceptance: setup and the agent's start call ensure(); nothing to ask the person. The watch puts back
    a task that went missing; after the person takes the schedule out nothing comes back on its own."""
    home(tmp_path, monkeypatch)
    monkeypatch.delenv("HRS_SCHEDULE", raising=False)
    calls = []
    runner = lambda cmd: calls.append(cmd) or 0  # noqa: E731
    first = schedule.ensure(platform="darwin", runner=runner)
    assert sorted(first["restored"]) == ["collect", "digest", "license", "reminders", "wake", "watch"]
    assert schedule.ensure(platform="darwin", runner=runner) == {"restored": []}
    (tmp_path / "LaunchAgents" / "ru.owai.jobshturman.collect.plist").unlink()
    calls.clear()
    assert schedule.ensure(platform="darwin", runner=runner, skip=("watch",)) == {"restored": ["collect"]}
    assert not any("ru.owai.jobshturman.watch" in " ".join(c) for c in calls)  # the running watch is not reloaded

    schedule.remove(platform="darwin", runner=runner)
    assert schedule.ensure(platform="darwin", runner=runner) == {"skipped": "removed_by_person"}
    assert schedule.installed("darwin") == []
    schedule.install(platform="darwin", runner=runner)
    assert schedule.status()["system_scheduler"] and len(schedule.installed("darwin")) == 6

    monkeypatch.setenv("HRS_SCHEDULE", "off")
    assert schedule.ensure(platform="darwin", runner=runner) == {"skipped": "off"}


def test_windows_ensure_asks_task_scheduler(tmp_path, monkeypatch):
    home(tmp_path, monkeypatch)
    monkeypatch.delenv("HRS_SCHEDULE", raising=False)
    created = []

    def runner(cmd):
        if cmd[1] == "/Query":
            return 0 if cmd[-1] in created else 1
        if cmd[1] == "/Create":
            created.append(cmd[cmd.index("/TN") + 1])
        return 0
    assert len(schedule.ensure(platform="win32", runner=runner)["restored"]) == 6
    assert "JobShturman\\wake-2" in created and "JobShturman\\watch-1" in created
    assert schedule.ensure(platform="win32", runner=runner) == {"restored": []}


def test_setup_switches_the_schedule_on(tmp_path, monkeypatch):
    from hrs_cli import setup
    home(tmp_path, monkeypatch)
    monkeypatch.setattr(schedule, "ensure", lambda **kw: {"restored": ["wake"]})
    assert setup.setup_schedule() == {"restored": ["wake"]}
    monkeypatch.setattr(schedule, "ensure", lambda **kw: 1 / 0)
    assert "error" in setup.setup_schedule()  # never fails the install


def test_the_watch_catches_up_on_missed_tasks(tmp_path, monkeypatch):
    home(tmp_path, monkeypatch)
    s, store = schedule.store_for()
    settings = core.get_settings(store, s.candidate_id)
    afternoon = datetime.now().astimezone().replace(hour=15, minute=0, second=0, microsecond=0)
    ran = []
    out = schedule.catch_up(settings, now=afternoon, runner=lambda task, now=None: ran.append(task) or {})
    assert sorted(ran) == ["collect", "digest", "license", "wake"]  # never reminders or the watch itself
    assert [d["task"] for d in out] == ran
    done = {t: {"at": afternoon.isoformat()} for t in ran}
    ran.clear()
    schedule.catch_up(dict(settings, last_runs=done), now=afternoon, runner=lambda task, now=None: ran.append(task) or {})
    assert ran == []
    early = afternoon.replace(hour=7, minute=0)
    schedule.catch_up(settings, now=early, runner=lambda task, now=None: ran.append(task) or {})
    assert ran == []  # nothing was due yet today


def test_a_task_never_runs_twice_at_once(tmp_path, monkeypatch):
    home(tmp_path, monkeypatch)
    lock = paths.data_dir() / "locks" / "wake.pid"
    lock.parent.mkdir(parents=True)
    lock.write_text(str(os.getppid()))  # a live process holds the wake
    assert schedule.run("wake") == {"task": "wake", "skipped": "running"}
    lock.write_text("999999999")  # a dead one does not
    assert schedule.run("wake", now=NOON.replace(hour=1))["task"] == "wake"
    assert not lock.exists()


def fake_agent(tmp_path, name, answer="Собрал 12 вакансий, 3 ждут вашего решения."):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / name
    script.write_text(f"#!{sys.executable}\nimport json, sys\n"
                      f"open({str(tmp_path / (name + '.args'))!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
                      f"print({answer!r})\n")
    script.chmod(0o755)
    return bin_dir


@pytest.mark.skipif(os.name == "nt", reason="POSIX fake agent")
def test_wake_starts_the_agent_with_only_jobshturman_tools(tmp_path, monkeypatch, capsys):
    from hrs_cli import wake
    home(tmp_path, monkeypatch)
    monkeypatch.setattr(wake, "EXTRA_PATH", ())
    s, store = schedule.store_for()
    settings = core.get_settings(store, s.candidate_id)
    day = datetime.now().astimezone().replace(hour=14, minute=0, second=0, microsecond=0)

    monkeypatch.setenv("PATH", str(tmp_path / "nothing"))
    assert wake.run(settings, now=day) == {"skipped": "no_agent"}
    assert wake.run(settings, now=day.replace(hour=22)) == {"skipped": "quiet_hours"}

    monkeypatch.setenv("PATH", str(fake_agent(tmp_path, "codex")))
    assert wake.run(settings, now=day)["agent"] == "codex"
    args = json.loads((tmp_path / "codex.args").read_text())
    assert args[:2] == ["exec", "--skip-git-repo-check"] and 'approval_policy="never"' in args

    fake_agent(tmp_path, "claude")
    out = wake.run(settings, now=day)
    args = json.loads((tmp_path / "claude.args").read_text())
    assert out["agent"] == "claude" and out["notified"]
    assert args[0] == "-p" and "день" in args[1] and "Никому ничего не отправляй" in args[1]
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    assert args[args.index("--allowedTools") + 1] == "mcp__hrshturman__*"
    assert "3 ждут вашего решения" in wake.wake_file().read_text(encoding="utf-8")
    assert "Собрал 12 вакансий" in capsys.readouterr().out  # the system notification (HRS_NOTIFY=print)


def test_the_system_scheduler_can_start_wake_and_watch(tmp_path, monkeypatch, capsys):
    """The scheduled command line `schedule run wake|watch` is accepted and does its work."""
    from hrs_cli import wake
    home(tmp_path, monkeypatch)
    monkeypatch.setattr(wake, "run", lambda settings, now=None: {"agent": "claude", "code": 0})
    assert main(["schedule", "run", "wake"]) == 0
    assert '"agent": "claude"' in capsys.readouterr().out
    monkeypatch.setattr(schedule, "catch_up", lambda settings, now=None: [])
    assert main(["schedule", "run", "watch"]) == 0
    assert '"ensure": {' in capsys.readouterr().out
    assert main(["schedule", "set", "wake", "--time", "10:00"]) == 0
    assert schedule.status()["tasks"]["wake"]["times"] == ["10:00"]


def test_the_agent_asks_the_hours_once_and_any_agent_can_register_its_task(tmp_path, monkeypatch):
    home(tmp_path, monkeypatch)
    assert schedule.status()["ask_wake_hours"]
    assert main(["schedule", "set", "wake", "--time", "08:45", "--time", "13:00", "--time", "18:30"]) == 0
    assert not schedule.status()["ask_wake_hours"]
    s, store = schedule.store_for()
    for agent in ("grok", "hermes", "openclaw"):
        core.register_agent_task(store, s.candidate_id, agent, "JobShturman", "08:45, 13:00, 18:30", "работа по поиску")
    assert {t["agent"] for t in schedule.status()["agent_tasks"]} == {"grok", "hermes", "openclaw"}
