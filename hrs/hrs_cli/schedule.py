"""hrs schedule: the system scheduler runs the deterministic tasks of hrs without the agent.

  macOS    LaunchAgents ~/Library/LaunchAgents/ru.owai.jobshturman.<task>.plist (a run missed in sleep
           happens on wake)
  Windows  Task Scheduler, folder JobShturman
  Linux    systemd user timers jobshturman-<task>.timer (Persistent=true)

Times are the computer's local time. Every task runs `python -m hrs_cli schedule run <task>` with the
same interpreter and data folder as the install. Nothing here applies or writes to an employer.
"""
import os
import plistlib
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from hrshturman_core import Store
from hrshturman_core import schedule as core
from hrshturman_core.brand import PRODUCT_NAME
from hrshturman_core.workspace import create_candidate

from . import paths

LABEL = "ru.owai.jobshturman"
ENV_KEYS = ("HRSHTURMAN_HOME", "HRSHTURMAN_WORKSPACE", "HRSHTURMAN_CANDIDATE", "HRSHTURMAN_TZ",
            "HRS_COLLECTOR_DATA", "HRS_KEY_SERVER", "HRS_KEYSTORE")
FIRST_LOOKBACK_DAYS = 7


def store_for(settings=None):
    s = settings or paths.settings()
    store = Store(s.db_path).init()
    create_candidate(store, s.candidate_id, s.timezone, actor="installer")
    return s, store


def _command(task):
    return [sys.executable, "-m", "hrs_cli", "schedule", "run", task]


def _env():
    return {k: os.environ[k] for k in ENV_KEYS if os.environ.get(k)}


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=False).returncode


def launch_agents_dir():
    return Path(os.environ.get("HRS_LAUNCH_AGENTS_DIR") or Path.home() / "Library" / "LaunchAgents")


def systemd_dir():
    return Path(os.environ.get("HRS_SYSTEMD_USER_DIR") or Path.home() / ".config" / "systemd" / "user")


def _times(entry):
    return [tuple(int(x) for x in t.split(":")) for t in entry["times"]]


def plan(settings_dict, platform=None):
    """What install writes and runs on this platform, without doing it."""
    platform = platform or sys.platform
    steps = []
    logs = paths.data_dir() / "logs"
    for task, entry in settings_dict["tasks"].items():
        if platform == "darwin":
            label = f"{LABEL}.{task}"
            path = launch_agents_dir() / f"{label}.plist"
            if not entry["enabled"]:
                steps.append({"task": task, "remove": str(path), "run": [["launchctl", "bootout", _gui(label)]]})
                continue
            body = plistlib.dumps({
                "Label": label, "ProgramArguments": _command(task),
                "StartCalendarInterval": [{"Hour": h, "Minute": m} for h, m in _times(entry)],
                "EnvironmentVariables": _env(), "RunAtLoad": False,
                "StandardOutPath": str(logs / f"{task}.log"), "StandardErrorPath": str(logs / f"{task}.log")})
            steps.append({"task": task, "write": {str(path): body.decode()},
                          "run": [["launchctl", "bootout", _gui(label)], ["launchctl", "bootstrap", _gui(), str(path)]]})
        elif platform == "win32":
            run = [["schtasks", "/Delete", "/F", "/TN", f"{PRODUCT_NAME}\\{task}-{n}"] for n in range(6)]
            if entry["enabled"]:
                line = subprocess.list2cmdline(_command(task))
                run += [["schtasks", "/Create", "/F", "/SC", "DAILY", "/ST", f"{h:02d}:{m:02d}",
                         "/TN", f"{PRODUCT_NAME}\\{task}-{n}", "/TR", line] for n, (h, m) in enumerate(_times(entry))]
            steps.append({"task": task, "run": run})
        else:
            unit = f"jobshturman-{task}"
            service, timer = systemd_dir() / f"{unit}.service", systemd_dir() / f"{unit}.timer"
            if not entry["enabled"]:
                steps.append({"task": task, "remove": [str(service), str(timer)],
                              "run": [["systemctl", "--user", "disable", "--now", f"{unit}.timer"]]})
                continue
            env = "".join(f"Environment={k}={v}\n" for k, v in _env().items())
            calendars = "".join(f"OnCalendar=*-*-* {h:02d}:{m:02d}:00\n" for h, m in _times(entry))
            steps.append({"task": task, "write": {
                str(service): f"[Unit]\nDescription={PRODUCT_NAME}: {core.TASKS[task]}\n\n[Service]\nType=oneshot\n"
                              f"{env}ExecStart={subprocess.list2cmdline(_command(task))}\n",
                str(timer): f"[Unit]\nDescription={PRODUCT_NAME}: {task}\n\n[Timer]\n{calendars}Persistent=true\n\n"
                            "[Install]\nWantedBy=timers.target\n"},
                "run": [["systemctl", "--user", "daemon-reload"],
                        ["systemctl", "--user", "enable", "--now", f"{unit}.timer"]]})
    return steps


def _gui(label=None):
    target = f"gui/{os.getuid()}" if hasattr(os, "getuid") else "gui/0"
    return f"{target}/{label}" if label else target


def install(dry_run=False, platform=None, runner=_run):
    """Put the enabled tasks into the system scheduler and take the disabled ones out. Safe to repeat."""
    s, store = store_for()
    steps = plan(core.get_settings(store, s.candidate_id), platform)
    if dry_run:
        return {"installed": False, "steps": steps}
    (paths.data_dir() / "logs").mkdir(parents=True, exist_ok=True)
    for step in steps:
        for path, body in (step.get("write") or {}).items():
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(body, encoding="utf-8")
        removed = step.get("remove")
        for path in [removed] if isinstance(removed, str) else removed or []:
            Path(path).unlink(missing_ok=True)
        step["codes"] = [runner(cmd) for cmd in step["run"]]
    return {"installed": True, "steps": steps}


def remove(platform=None, runner=_run):
    """Take every task of hrs out of the system scheduler. Data and settings stay."""
    s, store = store_for()
    settings = core.get_settings(store, s.candidate_id)
    off = dict(settings, tasks={t: dict(e, enabled=False) for t, e in settings["tasks"].items()})
    steps = plan(off, platform)
    for step in steps:
        removed = step.get("remove")
        for path in [removed] if isinstance(removed, str) else removed or []:
            Path(path).unlink(missing_ok=True)
        step["codes"] = [runner(cmd) for cmd in step["run"]]
    return {"removed": True, "steps": steps}


def installed(platform=None):
    platform = platform or sys.platform
    if platform == "darwin":
        return sorted(p.stem.rsplit(".", 1)[-1] for p in launch_agents_dir().glob(f"{LABEL}.*.plist"))
    if platform == "win32":
        return None  # Task Scheduler is asked only on demand: `schtasks /Query /TN JobShturman\\`
    return sorted(p.stem.removeprefix("jobshturman-") for p in systemd_dir().glob("jobshturman-*.timer"))


def digest_path(settings=None):
    return (settings or paths.settings()).workspace / "digest.md"


def status():
    s, store = store_for()
    settings = core.get_settings(store, s.candidate_id)
    return {"tasks": settings["tasks"], "quiet_hours": settings["quiet_hours"], "agent_tasks": settings["agent_tasks"],
            "last_runs": settings["last_runs"], "installed": installed(),
            "digest_file": str(digest_path(s)), "logs": str(paths.data_dir() / "logs"),
            "note": "Время задач — время этого компьютера. Компьютер должен быть включён; пропущенный во сне "
                    "запуск macOS и Linux выполнят при пробуждении."}


# --- notifications ---

def notify(title, text):
    """A system notification. HRS_NOTIFY=off silences it, =print prints it (tests, servers)."""
    mode = os.environ.get("HRS_NOTIFY", "")
    if mode == "off":
        return False
    if mode == "print":
        print(f"[{title}] {text}", flush=True)
        return True
    if sys.platform == "darwin":
        quote = lambda v: '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'  # noqa: E731
        return _run(["osascript", "-e", f"display notification {quote(text)} with title {quote(title)}"]) == 0
    if sys.platform == "win32":
        esc = lambda v: v.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("'", "''")  # noqa: E731
        script = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime]"
            " | Out-Null; $x = [Windows.Data.Xml.Dom.XmlDocument]::new(); $x.LoadXml('<toast><visual>"
            f"<binding template=\"ToastGeneric\"><text>{esc(title)}</text><text>{esc(text)}</text></binding>"
            "</visual></toast>'); [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
            "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe').Show("
            "[Windows.UI.Notifications.ToastNotification]::new($x))")
        return _run(["powershell", "-NoProfile", "-Command", script]) == 0
    if shutil.which("notify-send"):
        return _run(["notify-send", title, text]) == 0
    return False


# --- tasks ---

def _license_info():
    from . import license
    try:
        return license.refresh(force=False)
    except Exception:  # noqa: BLE001 - the key server being away never stops a reminder
        return {}


def run(task, collector=None, now=None):
    """One scheduled task. Returns what it did; the result is also kept in last_runs."""
    if task not in core.TASKS:
        raise SystemExit(f"task must be one of {list(core.TASKS)}")
    s, store = store_for()
    settings = core.get_settings(store, s.candidate_id)
    with store.read() as tx:
        paused = tx.find("candidate", s.candidate_id, "workspace")["paused"]
    if paused and task != "license":
        result = {"skipped": "paused"}
    elif task == "license":
        from . import license
        info = license.refresh(force=True)
        result = {"state": info.get("state"), "methodology": info.get("methodology")}
        if info.get("warning"):
            result["notified"] = notify(PRODUCT_NAME, info["warning"])
    elif task == "collect":
        from hrs_collector.service import Collector
        from hrshturman_local import collector_feed

        from .inproc import InProcessCollectorClient
        client = InProcessCollectorClient(collector or Collector(paths.collector_dir()), s.candidate_id)
        first = "collect" not in settings["last_runs"]
        out = collector_feed.sync(store, s.candidate_id, client, run=True,
                                  params={"lookback_days": FIRST_LOOKBACK_DAYS} if first else None)
        result = {"imported": out["imported"], "complete": out["coverage"].get("complete")}
    elif task == "digest":
        since = (settings["last_runs"].get("digest") or {}).get("at")
        d = core.digest(store, s.candidate_id, since=since, at=now, license_info=_license_info())
        path = digest_path(s)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(d["markdown"], encoding="utf-8")
        c = d["counts"]
        result = {"counts": c, "file": str(path)}
        if c["new"] or c["waiting_decision"] or c["waiting_permission"]:
            result["notified"] = notify(PRODUCT_NAME, f"Новых вакансий: {c['new']}. Ждут вашего решения: "
                                                      f"{c['waiting_decision']}. Откройте агента: «JobShturman, продолжи».")
    else:
        picked = core.pick_notification(store, s.candidate_id, at=now, license_info=_license_info())
        result = {"reason": picked["reason"]}
        if picked["notify"]:
            result |= {"reminder": picked["notify"]["id"], "notified": notify(PRODUCT_NAME, picked["notify"]["text"])}
    core.record_run(store, s.candidate_id, task, result)
    return {"task": task, "at": (now or datetime.now(timezone.utc)).isoformat(), **result}
