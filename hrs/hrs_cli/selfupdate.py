"""hrs updates itself: the person never reinstalls by hand.

When: once a day in the scheduled `license` task, and when the agent starts `hrs serve` (the check runs in the
background). On macOS and Linux the install goes in while the agent keeps hrs open and works from its next
start; on Windows it waits until the agent closes (see update_runner). The newest release on GitHub is
compared with this hrs.

How: the same way as the installer. The new and the current release wheels and the source of the new release
are downloaded here, then update_runner.py, started with uv's own Python outside the hrs environment, waits
until no hrs is running, runs `uv tool install --force`, checks the new hrs and lays the new skill out with
`hrs setup`. If the new hrs does not start, the current one goes back. The data folder is never touched.

Off: HRS_AUTO_UPDATE=off, and always for hrs installed from a project folder (editable).
"""
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from . import license, paths, setup, update_runner

REPO = "ZhdanDesign/jobshturman"
ASSET = "jobshturman-wheels.zip"
CHECK_EVERY = timedelta(hours=20)
TIMEOUT = 60


def folder() -> Path:
    return paths.data_dir() / "self-update"


def pids_dir() -> Path:
    return folder() / "running"


def state_file() -> Path:
    return folder() / "state.json"


def result_file() -> Path:
    return folder() / "result.json"


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def version_tuple(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.lstrip("v").split("."))
    except ValueError:
        return ()


def current() -> str:
    return license.client_version()


def disabled() -> str | None:
    """Why this hrs does not update itself, or None."""
    if os.environ.get("HRS_AUTO_UPDATE", "").lower() in ("off", "0", "no", "false"):
        return "off"
    if current() == "0":
        return "not_installed"
    if "site-packages" not in Path(__file__).resolve().parts:  # run from a project folder (editable install)
        return "editable"
    return None


def _http(http: httpx.Client | None) -> httpx.Client:
    return http or httpx.Client(timeout=TIMEOUT, follow_redirects=True)


def check(force: bool = False, http: httpx.Client | None = None) -> dict:
    """The newest release and whether it is newer than this hrs. Asks GitHub at most once in CHECK_EVERY."""
    state = _read(state_file())
    checked = state.get("checked_at")
    fresh = checked and datetime.now(timezone.utc) - datetime.fromisoformat(checked) < CHECK_EVERY
    if force or not fresh or not state.get("latest"):
        try:
            client = _http(http)
            resp = client.get(f"https://api.github.com/repos/{REPO}/releases/latest",
                              headers={"Accept": "application/vnd.github+json"})
            resp.raise_for_status()
            body = resp.json()
            state["latest"] = {"tag": body["tag_name"], "version": body["tag_name"].lstrip("v"),
                               "notes": (body.get("body") or "").strip()[:1500]}
            state["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            _write(state_file(), state)
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            return {"current": current(), "error": f"не удалось узнать новую версию: {exc}"}
    latest = state["latest"]
    return {"current": current(), "latest": latest["version"], "notes": latest["notes"], "tag": latest["tag"],
            "available": version_tuple(latest["version"]) > version_tuple(current()), "checked_at": state["checked_at"]}


def _download(client: httpx.Client, url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with client.stream("GET", url) as resp:
        resp.raise_for_status()
        with dest.open("wb") as f:
            for chunk in resp.iter_bytes():
                f.write(chunk)
    return dest


def uv_path() -> str | None:
    found = shutil.which("uv")
    if found:
        return found
    name = "uv.exe" if os.name == "nt" else "uv"
    for candidate in (Path.home() / ".local" / "bin" / name, Path.home() / ".cargo" / "bin" / name):
        if candidate.is_file():
            return str(candidate)
    return None


def hrs_path(uv: str) -> str:
    out = subprocess.run([uv, "tool", "dir", "--bin"], capture_output=True, text=True, timeout=60, check=False)
    return str(Path(out.stdout.strip()) / ("hrs.exe" if os.name == "nt" else "hrs"))


def prepare(info: dict, http: httpx.Client | None = None, uv: str | None = None) -> Path:
    """Download everything the install needs and write the job for update_runner. Returns the job file."""
    uv = uv or uv_path()
    if not uv:
        raise RuntimeError("нет uv: обновление ставится так же, как установщик, через uv")
    work = folder() / info["latest"]
    if work.exists():
        shutil.rmtree(work)
    client = _http(http)
    base = f"https://github.com/{REPO}/releases/download"
    for name, tag in (("wheels", info["tag"]), ("old-wheels", f"v{info['current']}")):
        archive = _download(client, f"{base}/{tag}/{ASSET}", work / f"{name}.zip")
        with zipfile.ZipFile(archive) as z:
            z.extractall(work / name)
    source = _download(client, f"https://github.com/{REPO}/archive/refs/tags/{info['tag']}.tar.gz", work / "source.tar.gz")
    with tarfile.open(source) as t:
        t.extractall(work / "source", filter="data")
    skill = next((work / "source").glob("*/plugin/skills/hrshturman"), None)
    if skill is None or not (skill / "SKILL.md").is_file():
        raise RuntimeError("в выпуске нет навыка plugin/skills/hrshturman")
    runner = work / "update_runner.py"
    shutil.copy2(update_runner.__file__, runner)
    job = {"uv": uv, "hrs": hrs_path(uv), "from": info["current"], "to": info["latest"], "notes": info["notes"],
           "wheels": str(work / "wheels"), "old_wheels": str(work / "old-wheels"), "skill_dir": str(skill),
           "from_plugin": not (setup.claude_home() / "skills" / setup.SKILL).is_dir(),
           "pids": str(pids_dir()), "result": str(result_file()), "parent": os.getpid(), "runner": str(runner)}
    _write(work / "job.json", job)
    return work / "job.json"


def _detach(cmd: list[str], cwd: Path) -> None:
    """Start a process that outlives this one, its output going to logs/self-update.log."""
    log = paths.data_dir() / "logs" / "self-update.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    flags = {}
    if os.name == "nt":
        flags["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000
    else:
        flags["start_new_session"] = True
    with log.open("a", encoding="utf-8") as out:
        subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=out, **flags)


def _spawn(job_path: Path) -> None:
    job = _read(job_path)
    _detach([job["uv"], "run", "--no-project", "--python", "3.12", "python", "-I", job["runner"], str(job_path)],
            job_path.parent)


def start(force: bool = False, http: httpx.Client | None = None, spawn=_spawn) -> dict:
    """Check for a newer hrs and, if there is one and no agent holds hrs open, install it in the background."""
    reason = disabled()
    if reason:
        return {"skipped": reason}
    info = check(force=force, http=http)
    if info.get("error") or not info["available"]:
        return info
    busy = update_runner.running(pids_dir(), exclude=os.getpid())
    if busy and not update_runner.IN_PLACE:
        return info | {"pending": "JobShturman открыт у агента: обновлю, когда агент закроется"}
    try:
        job = prepare(info, http=http)
    except (httpx.HTTPError, OSError, RuntimeError, zipfile.BadZipFile, tarfile.TarError) as exc:
        return info | {"error": f"не удалось скачать обновление: {exc}"}
    spawn(job)
    return info | {"started": True}


# --- hrs serve: register while running, check on start, install after close ---

PACKAGES = ("hrs_cli", "hrshturman_core", "hrshturman_local", "hrs_collector")
_held = []


def preload() -> None:
    """Import all of our code now: an update may replace the files under a running `hrs serve`, and a module
    imported later would then come from the new release and not match the rest."""
    import importlib
    import pkgutil
    for name in PACKAGES:
        package = _quiet(lambda name=name: importlib.import_module(name))
        for module in pkgutil.walk_packages(getattr(package, "__path__", []), f"{name}."):
            if not module.name.endswith("__main__"):
                _quiet(lambda module=module: importlib.import_module(module.name))


def hold() -> None:
    """Called by `hrs serve` and `hrs ui`: mark this process as using hrs, check for a newer one in the
    background and, when this process ends, install it if nothing else holds hrs."""
    if disabled():
        return
    preload()
    held = update_runner.claim(pids_dir(), os.getpid())
    _held.append(held)
    threading.Thread(target=lambda: _quiet(check), daemon=True).start()

    def release():
        # The download takes a while and the agent may not wait for it: a separate `hrs self-update` does it.
        held.close()
        Path(held.name).unlink(missing_ok=True)
        info = _read(state_file()).get("latest") or {}
        if version_tuple(info.get("version", "")) > version_tuple(current()) \
                and not update_runner.running(pids_dir(), exclude=os.getpid()):
            _quiet(lambda: _detach([sys.executable, "-m", "hrs_cli", "self-update"], folder()))

    import atexit
    atexit.register(release)


def _quiet(fn):
    try:
        return fn()
    except Exception:  # noqa: BLE001 - an update never breaks the agent's session
        return None


# --- what the agent tells the person ---

def news() -> dict:
    """Once after an update: what to tell the person in one phrase. Empty otherwise."""
    result, state = _read(result_file()), _read(state_file())
    if not result.get("ok") or result.get("to") != current() or state.get("told") == result["to"]:
        return {}
    notes = result.get("notes") or ""
    return {"hrs_updated": {"from": result["from"], "to": result["to"], "notes": notes,
                            "message": f"JobShturman обновился до версии {result['to']}."
                                       + (f" Что нового: {notes}" if notes else "")}}


def mark_told() -> None:
    result = _read(result_file())
    if result.get("to"):
        state = _read(state_file())
        state["told"] = result["to"]
        _write(state_file(), state)


def status() -> dict:
    return {"current": current(), "disabled": disabled(), "last": _read(result_file()) or None,
            "checked": _read(state_file()).get("latest"), "log": str(paths.data_dir() / "logs" / "self-update.log")}

