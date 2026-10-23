"""hrs updates itself: check GitHub, download, install like the installer, put the old one back on failure,
tell the person once."""
import asyncio
import io
import json
import os
import subprocess
import tarfile
import zipfile
from datetime import datetime, timedelta, timezone

import httpx
from hrs_cli import paths, selfupdate, update_runner
from hrs_cli.server import build
from hrs_collector.service import Collector

NOTES = "Автоотклики в двух режимах."


def _zip(name):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, b"wheel")
    return buf.getvalue()


def _source(tag):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        data = b"---\nname: hrshturman\n---\n"
        info = tarfile.TarInfo(f"jobshturman-{tag}/plugin/skills/hrshturman/SKILL.md")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def github(latest="v0.6.0", calls=None):
    def handler(request):
        url = str(request.url)
        if calls is not None:
            calls.append(url)
        if url.endswith("/releases/latest"):
            return httpx.Response(200, json={"tag_name": latest, "body": NOTES})
        if "/releases/download/" in url:
            tag = url.split("/")[-2]
            return httpx.Response(200, content=_zip(f"hrshturman-{tag[1:]}-py3-none-any.whl"))
        if "/archive/refs/tags/" in url:
            return httpx.Response(200, content=_source(latest[1:]))
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def home(tmp_path, monkeypatch, version="0.5.0"):
    monkeypatch.setenv("HRSHTURMAN_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HRSHTURMAN_WORKSPACE", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setattr(selfupdate, "current", lambda: version)
    monkeypatch.setattr(selfupdate, "disabled", lambda: None)
    monkeypatch.setattr(selfupdate, "hrs_path", lambda uv: "/bin/hrs")


def test_check_asks_github_once_a_day(tmp_path, monkeypatch):
    home(tmp_path, monkeypatch)
    calls = []
    info = selfupdate.check(http=github(calls=calls))
    assert info["available"] and info["latest"] == "0.6.0" and info["notes"] == NOTES
    assert selfupdate.check(http=github(calls=calls))["available"] and len(calls) == 1
    selfupdate.check(force=True, http=github(calls=calls))
    assert len(calls) == 2
    monkeypatch.setattr(selfupdate, "current", lambda: "0.6.0")
    assert not selfupdate.check(http=github())["available"]


def test_editable_and_switched_off_never_update(tmp_path, monkeypatch):
    monkeypatch.setenv("HRSHTURMAN_HOME", str(tmp_path / "home"))
    assert selfupdate.disabled() == "editable"  # tests run hrs from the project folder
    monkeypatch.setenv("HRS_AUTO_UPDATE", "off")
    assert selfupdate.start(http=github()) == {"skipped": "off"}


def test_start_downloads_and_hands_over_to_the_runner(tmp_path, monkeypatch):
    home(tmp_path, monkeypatch)
    calls, spawned = [], []
    out = selfupdate.start(http=github(calls=calls), spawn=spawned.append)
    assert out["started"]
    job = json.loads(spawned[0].read_text(encoding="utf-8"))
    assert job["from"] == "0.5.0" and job["to"] == "0.6.0" and job["notes"] == NOTES
    assert any("/releases/download/v0.6.0/jobshturman-wheels.zip" in c for c in calls)
    assert any("/releases/download/v0.5.0/jobshturman-wheels.zip" in c for c in calls)
    assert os.listdir(job["wheels"]) == ["hrshturman-0.6.0-py3-none-any.whl"]
    assert os.listdir(job["old_wheels"]) == ["hrshturman-0.5.0-py3-none-any.whl"]
    assert job["skill_dir"].endswith(os.path.join("plugin", "skills", "hrshturman"))
    assert job["from_plugin"]  # no ~/.claude/skills/hrshturman: the skill came with the plugin
    assert open(job["runner"], encoding="utf-8").read().startswith('"""Installs a downloaded hrs release')


def test_windows_waits_while_the_agent_holds_hrs(tmp_path, monkeypatch):
    home(tmp_path, monkeypatch)
    monkeypatch.setattr(update_runner, "IN_PLACE", False)
    held = update_runner.claim(selfupdate.pids_dir(), 999999998)  # a live hrs holds its lock
    (selfupdate.pids_dir() / "999999999.pid").write_text("")  # a dead one is cleaned up
    spawned = []
    try:
        out = selfupdate.start(http=github(), spawn=spawned.append)
        assert out["pending"] and not spawned
        assert sorted(p.name for p in selfupdate.pids_dir().iterdir()) == ["999999998.pid"]
    finally:
        held.close()


def _job(tmp_path):
    return {"uv": "uv", "hrs": "hrs", "from": "0.5.0", "to": "0.6.0", "notes": NOTES, "wheels": "new",
            "old_wheels": "old", "skill_dir": "skill", "from_plugin": False, "pids": str(tmp_path / "running"),
            "result": str(tmp_path / "result.json")}


def test_runner_installs_then_lays_out_the_skill(tmp_path):
    calls = []

    def run(cmd, timeout=600):
        calls.append(cmd)
        return 0, "hrs 0.6.0" if cmd[1:] == ["--version"] else ""

    result = update_runner.apply(_job(tmp_path), run=run)
    assert result["ok"] and result["setup_ok"]
    assert calls[0][:4] == ["uv", "tool", "install", "--force"] and calls[0][-1] == "hrshturman==0.6.0"
    assert calls[-1] == ["hrs", "setup", "--skill-dir", "skill"]


def test_runner_puts_the_old_version_back_when_the_new_one_does_not_start(tmp_path):
    calls = []

    def run(cmd, timeout=600):
        calls.append(cmd)
        return (1, "ImportError") if cmd[1:] == ["--version"] else (0, "")

    result = update_runner.apply(_job(tmp_path), run=run)
    assert not result["ok"] and result["rolled_back"] and result["error"] == "install"
    assert calls[-1][-1] == "hrshturman==0.5.0" and "old" in calls[-1]
    assert not any(c[1] == "setup" for c in calls)


def test_agent_tells_the_person_once(tmp_path, monkeypatch):
    home(tmp_path, monkeypatch, version="0.6.0")
    server = build(paths.settings(), Collector(tmp_path / "collector"))
    selfupdate.result_file().parent.mkdir(parents=True, exist_ok=True)
    selfupdate.result_file().write_text(json.dumps({"ok": True, "from": "0.5.0", "to": "0.6.0", "notes": NOTES}))

    def status():
        result = asyncio.run(server.call_tool("license_status", {}))
        assert not result.is_error, result.content[0].text
        return result.structured_content or json.loads(result.content[0].text)

    first = status()
    assert first["hrs_updated"]["message"] == f"JobShturman обновился до версии 0.6.0. Что нового: {NOTES}"
    assert "hrs_updated" not in status()


def test_a_reused_pid_of_another_program_does_not_block_the_update(tmp_path):
    """The pid of an hrs that died long ago now belongs to another program (on the owner's Mac a system
    daemon): its file is stale and goes, or the update waits forever."""
    if os.name == "nt":
        return
    other = subprocess.Popen(["sleep", "30"])
    try:
        folder = tmp_path / "running"
        folder.mkdir()
        (folder / f"{other.pid}.pid").write_text("")
        assert update_runner.running(folder) == []
        assert not list(folder.iterdir())
    finally:
        other.kill()


def test_a_running_hrs_is_known_by_the_lock_on_its_pid_file(tmp_path):
    folder = tmp_path / "running"
    held = update_runner.claim(folder, 999999999)  # the pid itself proves nothing: the lock does
    assert update_runner.running(folder) == [999999999]
    held.close()
    assert update_runner.running(folder) == [] and not list(folder.iterdir())


def test_mac_and_linux_update_while_the_agent_keeps_hrs_open(tmp_path, monkeypatch):
    """Claude Desktop and Codex keep `hrs serve` open all day: waiting for them to close means never.
    On macOS and Linux the new release goes in next to the running one and works from the next start;
    Windows locks the running files, so there the update still waits."""
    home(tmp_path, monkeypatch)
    held = update_runner.claim(selfupdate.pids_dir(), 999999999)
    try:
        monkeypatch.setattr(update_runner, "IN_PLACE", True)
        out = selfupdate.start(http=github(), spawn=lambda job: None)
        assert out["started"]
        calls = []
        job = _job(tmp_path) | {"pids": str(selfupdate.pids_dir())}
        result = update_runner.apply(job, run=lambda cmd, timeout=600: calls.append(cmd) or (0, "hrs 0.6.0"))
        assert result["ok"] and calls
        monkeypatch.setattr(update_runner, "IN_PLACE", False)
        assert selfupdate.start(http=github(), spawn=lambda job: None)["pending"]
        assert update_runner.apply(job, run=lambda cmd, timeout=600: (0, ""))["error"] == "busy"
    finally:
        held.close()


def test_serve_loads_all_its_code_before_an_update_can_replace_it(monkeypatch):
    import sys
    monkeypatch.delitem(sys.modules, "hrs_cli.wake", raising=False)
    selfupdate.preload()
    assert "hrs_cli.wake" in sys.modules and "hrs_cli.__main__" not in sys.modules


def test_a_release_out_last_night_is_installed_by_the_morning_task(tmp_path, monkeypatch):
    """0.9.4.2 came out at 21:27; the morning task trusted yesterday's answer from GitHub (asked under 20 hours
    ago) and kept 0.9.4.1. The daily task always asks GitHub, and the hourly watch asks every few hours."""
    from hrs_cli import schedule
    home(tmp_path, monkeypatch)
    old = {"latest": {"tag": "v0.5.0", "version": "0.5.0", "notes": ""},
           "checked_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")}
    selfupdate._write(selfupdate.state_file(), old)
    assert not selfupdate.check(http=github())["available"]  # a recent answer is trusted...
    started = []
    monkeypatch.setattr(selfupdate, "start", lambda force=False, **kw: started.append(force) or {})
    monkeypatch.setattr(schedule, "catch_up", lambda settings, now=None: [])
    monkeypatch.setattr(schedule, "ensure", lambda **kw: {})
    schedule.run("license")
    schedule.run("watch")
    assert started == [True, False]
    assert selfupdate.CHECK_EVERY <= timedelta(hours=3)


def test_a_failed_update_is_not_retried_every_hour(tmp_path, monkeypatch):
    home(tmp_path, monkeypatch)
    selfupdate._write(selfupdate.result_file(), {"ok": False, "to": "0.6.0", "error": "install",
                                                 "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    spawned = []
    assert selfupdate.start(http=github(), spawn=spawned.append).get("skipped") == "failed_recently"
    assert selfupdate.start(force=True, http=github(), spawn=spawned.append)["started"]


def test_the_agent_hears_to_restart_when_its_hrs_is_older_than_the_installed_one(tmp_path, monkeypatch):
    """On macOS and Linux the update goes in under a running `hrs serve`; that serve still runs the old code."""
    home(tmp_path, monkeypatch, version="0.6.0")
    monkeypatch.setattr(selfupdate, "_started", "0.5.0")
    selfupdate._write(selfupdate.result_file(), {"ok": True, "from": "0.5.0", "to": "0.6.0", "notes": NOTES})
    message = selfupdate.news()["hrs_updated"]["message"]
    assert "перезапустите" in message.replace(" ", " ")
