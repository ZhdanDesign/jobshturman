"""jbs schedule: the system scheduler runs the deterministic tasks of hrs without the agent.

  macOS    LaunchAgents ~/Library/LaunchAgents/ru.owai.jobshturman.<task>.plist (a run missed in sleep
           happens on wake)
  Windows  Task Scheduler, folder JobShturman
  Linux    systemd user timers jobshturman-<task>.timer (Persistent=true)

Times are the computer's local time. Every task runs `python -m hrs_cli schedule run <task>` with the
same interpreter and data folder as the install. Nothing here applies or writes to an employer.
The daily `license` task and the hourly `watch` also install a newer hrs if one is out (see selfupdate.py).

The schedule switches itself on: `jbs setup` (the installer and every self-update) and the start of
`jbs serve` call ensure(), which puts back whatever is missing. `wake` starts the agent three times a day
(wake.py). `watch` runs every hour and at login: it puts back tasks that went missing and runs today's
tasks that were missed while the computer slept or was off. After the person takes the tasks out
(`jbs schedule remove`), nothing puts them back until `jbs schedule install`.
"""
import os
import plistlib
import shutil
import subprocess
import sys
from datetime import UTC, datetime
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


def plan(settings_dict, platform=None, only=None):
    """What install writes and runs on this platform, without doing it. only: these tasks."""
    platform = platform or sys.platform
    steps = []
    logs = paths.data_dir() / "logs"
    for task, entry in settings_dict["tasks"].items():
        if only is not None and task not in only:
            continue
        hourly = task == "watch"
        if platform == "darwin":
            label = f"{LABEL}.{task}"
            path = launch_agents_dir() / f"{label}.plist"
            if not entry["enabled"]:
                steps.append({"task": task, "remove": str(path), "run": [["launchctl", "bootout", _gui(label)]]})
                continue
            when = ({"StartInterval": entry.get("every_minutes", 60) * 60} if hourly else
                    {"StartCalendarInterval": [{"Hour": h, "Minute": m} for h, m in _times(entry)]})
            body = plistlib.dumps({
                "Label": label, "ProgramArguments": _command(task), **when,
                "EnvironmentVariables": _env(), "RunAtLoad": hourly,
                "StandardOutPath": str(logs / f"{task}.log"), "StandardErrorPath": str(logs / f"{task}.log")})
            steps.append({"task": task, "write": {str(path): body.decode()},
                          "run": [["launchctl", "bootout", _gui(label)], ["launchctl", "bootstrap", _gui(), str(path)]]})
        elif platform == "win32":
            run = [["schtasks", "/Delete", "/F", "/TN", f"{PRODUCT_NAME}\\{task}-{n}"] for n in range(6)]
            # Task Scheduler takes no environment and /TR is limited to 261 characters: the task runs a small
            # .cmd next to the logs that sets HRSHTURMAN_* and HRS_* as launchd and systemd get them.
            script = logs / f"{task}.cmd"
            body = ("@echo off\r\n" + "".join(f'set "{k}={v}"\r\n' for k, v in _env().items())
                    + f'{subprocess.list2cmdline(_command(task))} >> "{logs / f"{task}.log"}" 2>&1\r\n')
            line = f'"{script}"'
            if entry["enabled"] and hourly:
                run += [["schtasks", "/Create", "/F", "/SC", "HOURLY", "/MO", "1", "/TN", f"{PRODUCT_NAME}\\{task}-0",
                         "/TR", line],
                        ["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/TN", f"{PRODUCT_NAME}\\{task}-1", "/TR", line]]
            elif entry["enabled"]:
                run += [["schtasks", "/Create", "/F", "/SC", "DAILY", "/ST", f"{h:02d}:{m:02d}",
                         "/TN", f"{PRODUCT_NAME}\\{task}-{n}", "/TR", line] for n, (h, m) in enumerate(_times(entry))]
            step = {"task": task, "run": run}
            if entry["enabled"]:
                step["write"] = {str(script): body}
            else:
                step["remove"] = str(script)
            steps.append(step)
        else:
            unit = f"jobshturman-{task}"
            service, timer = systemd_dir() / f"{unit}.service", systemd_dir() / f"{unit}.timer"
            if not entry["enabled"]:
                steps.append({"task": task, "remove": [str(service), str(timer)],
                              "run": [["systemctl", "--user", "disable", "--now", f"{unit}.timer"]]})
                continue
            env = "".join(f'Environment="{k}={v}"\n' for k, v in _env().items())  # quoted: paths with spaces
            calendars = (f"OnBootSec=5min\nOnUnitActiveSec={entry.get('every_minutes', 60)}min\n" if hourly else
                         "".join(f"OnCalendar=*-*-* {h:02d}:{m:02d}:00\n" for h, m in _times(entry)))
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


def _apply(steps, runner):
    (paths.data_dir() / "logs").mkdir(parents=True, exist_ok=True)
    for step in steps:
        for path, body in (step.get("write") or {}).items():
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(body, encoding="utf-8", newline="")
        removed = step.get("remove")
        for path in [removed] if isinstance(removed, str) else removed or []:
            Path(path).unlink(missing_ok=True)
        step["codes"] = [runner(cmd) for cmd in step["run"]]
    return steps


def install(dry_run=False, platform=None, runner=_run):
    """Put the enabled tasks into the system scheduler and take the disabled ones out. Safe to repeat.
    Called by the person (or the agent at their word): it also lifts an earlier `remove`."""
    s, store = store_for()
    steps = plan(core.get_settings(store, s.candidate_id), platform)
    if dry_run:
        return {"installed": False, "steps": steps}
    core.set_system_scheduler(store, s.candidate_id, True)
    return {"installed": True, "steps": _apply(steps, runner)}


def _present(step, platform, runner):
    """Whether the system scheduler already holds this step as planned."""
    files = step.get("write") or {}
    written = all(Path(path).is_file() and Path(path).read_bytes() == body.encode("utf-8") for path, body in files.items())
    if platform == "win32":
        names = [cmd[cmd.index("/TN") + 1] for cmd in step["run"] if cmd[1] == "/Create"]
        return written and all(runner(["schtasks", "/Query", "/TN", name]) == 0 for name in names)
    return written


def ensure(platform=None, runner=_run, skip=()):
    """Put back the enabled tasks that are missing or out of date, leave the rest alone. Does nothing after the
    person took the schedule out, and with HRS_SCHEDULE=off. Never reloads the tasks in skip (the running
    watch would stop itself)."""
    if os.environ.get("HRS_SCHEDULE", "").lower() in ("off", "0", "no", "false"):
        return {"skipped": "off"}
    platform = platform or sys.platform
    s, store = store_for()
    settings = core.get_settings(store, s.candidate_id)
    if not settings["system_scheduler"]:
        return {"skipped": "removed_by_person"}
    enabled = [t for t, e in settings["tasks"].items() if e["enabled"] and t not in skip]
    missing = [step for step in plan(settings, platform, only=enabled) if not _present(step, platform, runner)]
    _apply(missing, runner)
    return {"restored": [step["task"] for step in missing]}


def remove(platform=None, runner=_run):
    """Take every task of hrs out of the system scheduler. Data and settings stay. Nothing puts them back
    on its own until install()."""
    s, store = store_for()
    core.set_system_scheduler(store, s.candidate_id, False)
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
            "system_scheduler": settings["system_scheduler"],
            "ask_wake_hours": not settings["tasks"]["wake"].get("agreed"),
            "note": "Время задач — время этого компьютера. Сторож раз в час возвращает пропавшие задачи и догоняет "
                    "пропущенные за сегодня, пока компьютер спал или был выключен."}


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
        quote = lambda v: '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
        return _run(["osascript", "-e", f"display notification {quote(text)} with title {quote(title)}"]) == 0
    if sys.platform == "win32":
        esc = lambda v: v.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("'", "''")
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
    lock = _lock(task)
    if lock is None:
        return {"task": task, "skipped": "running"}
    try:
        result = _task(task, s, store, settings, paused, collector, now)
    finally:
        lock.close()
        Path(lock.name).unlink(missing_ok=True)
    core.record_run(store, s.candidate_id, task, result)
    return {"task": task, "at": (now or datetime.now(UTC)).isoformat(), **result}


def _lock(task):
    """A task runs once at a time: the watch must not start a second wake while the first one works.
    The running task holds a lock on locks/<task>.pid (returned open; closing it frees the task), so the file of
    a task that crashed does not block it, even when its pid now belongs to another program. A file of an
    older hrs, written without the lock (no "locked" mark), counts while its pid is still an hrs."""
    from .update_runner import alive, is_hrs, try_lock
    path = paths.data_dir() / "locks" / f"{task}.pid"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        words = path.read_text(encoding="utf-8").split()
    except OSError:
        words = []
    legacy = int(words[0]) if len(words) == 1 and words[0].isdigit() else 0
    try:
        handle = path.open("a+", encoding="utf-8")
    except OSError:  # Windows: another process holds the file
        return None
    if not try_lock(handle) or (legacy and legacy != os.getpid() and alive(legacy) and is_hrs(legacy)):
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()} locked")
    handle.flush()
    return handle


def _due(entry, last, local_now):
    """The latest of today's times that has passed and has had no run since, or None."""
    slots = [local_now.replace(hour=h, minute=m, second=0, microsecond=0) for h, m in _times(entry)]
    passed = [t for t in slots if t <= local_now]
    if not passed:
        return None
    slot = max(passed)
    if last and datetime.fromisoformat(last) >= slot:
        return None
    return slot


def catch_up(settings, now=None, runner=None):
    """Run today's tasks whose time passed while the computer slept or was off."""
    local_now = (now or datetime.now(UTC)).astimezone()
    done = []
    for task, entry in settings["tasks"].items():
        if not entry["enabled"] or task in core.NO_CATCH_UP:
            continue
        if _due(entry, (settings["last_runs"].get(task) or {}).get("at"), local_now):
            out = (runner or run)(task, now=now)
            done.append({"task": task, **{k: v for k, v in out.items() if k in ("skipped", "reason", "error")}})
    return done


def _task(task, s, store, settings, paused, collector, now):
    if task == "watch":
        from . import selfupdate
        try:
            update = selfupdate.start()
        except Exception as exc:  # noqa: BLE001 - a failed update never stops the watch
            update = {"error": str(exc)}
        return {"ensure": ensure(skip=("watch",)), "caught_up": catch_up(settings, now=now), "self_update": update}
    if paused and task != "license":
        result = {"skipped": "paused"}
    elif task == "wake":
        from . import wake
        result = wake.run(settings, now=now)
    elif task == "license":
        from . import license
        info = license.refresh(force=True)
        result = {"state": info.get("state"), "methodology": info.get("methodology")}
        if info.get("warning"):
            result["notified"] = notify(PRODUCT_NAME, info["warning"])
        from . import selfupdate
        try:
            result["self_update"] = selfupdate.start(force=True)  # yesterday's answer may miss last night's release
        except Exception as exc:  # noqa: BLE001 - a failed update never stops the key check
            result["self_update"] = {"error": str(exc)}
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
    return result
