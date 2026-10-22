"""Installs a downloaded hrs release once no hrs is running. Only the standard library: it runs from a copy
outside the hrs environment, with uv's own Python, because that environment is the thing being replaced.

  python update_runner.py <job.json>

The job (written by selfupdate.prepare) names uv, the new and the old wheels, the skill folder of the new
release and where to write the result. Steps, as in the installer: `uv tool install --force` the new wheels,
check that the new hrs starts, then `hrs setup --skill-dir` lays the new skill out for the agents. If the new
hrs does not start, the old wheels go back. The person's data folder is never touched.

Who holds hrs open: every `hrs serve` and `hrs ui` keeps a lock on its file <pid>.pid in the job's `pids`
folder. A file nobody locks is stale, even when its pid now belongs to another program; files of older hrs
without the lock count while their pid is still an hrs. Claude Desktop and Codex keep `hrs serve` open all
day, so on macOS and Linux the new release goes in while it runs (the running hrs has its code loaded and
switches at its next start); Windows locks the files of a running program, so there the install waits.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

WAIT_PARENT = 120  # seconds
IN_PLACE = os.name != "nt"
HRS_NAMES = ("hrs", "jbs", "hrs.exe", "jbs.exe", "python.exe", "pythonw.exe")


def alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, check=False)
        return str(pid) in out.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def is_hrs(pid: int) -> bool:
    """The process is hrs, not another program that got the pid of an hrs that ended long ago."""
    cmd = (["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"] if os.name == "nt"
           else ["ps", "-o", "command=", "-p", str(pid)])
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return True  # cannot tell: better to wait than to replace a running hrs
    if os.name == "nt":
        return out.stdout.split(",")[0].strip('"').lower() in HRS_NAMES
    return any(Path(word).name in HRS_NAMES or "hrs_cli" in word for word in out.stdout.split())


def _lock(handle) -> bool:
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def claim(folder: Path, pid: int):
    """Mark this hrs as holding the program open for as long as the returned file stays open."""
    folder.mkdir(parents=True, exist_ok=True)
    handle = (folder / f"{pid}.pid").open("a+", encoding="utf-8")
    _lock(handle)
    return handle


def locked(path: Path) -> bool:
    """Some running hrs holds the lock on this pid file."""
    try:
        with path.open("a+", encoding="utf-8") as handle:
            return not _lock(handle)
    except OSError:  # Windows does not open a file another process holds
        return True


def running(folder: Path, exclude: int = 0) -> list[int]:
    """Live hrs processes that hold the program open (serve, ui), from their pid files. Stale files go."""
    live = []
    for f in folder.glob("*.pid") if folder.is_dir() else []:
        try:
            pid = int(f.stem)
        except ValueError:
            continue
        if pid == exclude:
            continue
        if locked(f) or (alive(pid) and is_hrs(pid)):
            live.append(pid)
        else:
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
    return live


def _run(cmd: list[str], timeout: int = 600) -> tuple[int, str]:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return out.returncode, (out.stdout + out.stderr)[-2000:]


def _install(job: dict, wheels: str, version: str, run) -> tuple[int, str]:
    return run([job["uv"], "tool", "install", "--force", "--python", "3.12", "--find-links", wheels,
                f"hrshturman=={version}"])


def apply(job: dict, run=_run) -> dict:
    """Install the new release; on failure put the old one back. Returns what happened."""
    result = {"from": job["from"], "to": job["to"], "notes": job.get("notes", ""),
              "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    busy = running(Path(job["pids"]))
    if busy and not IN_PLACE:
        return result | {"ok": False, "error": "busy", "message": "JobShturman ещё работает у агента; обновлю, когда он закроется"}
    code, log = _install(job, job["wheels"], job["to"], run)
    if code == 0:
        code, log = run([job["hrs"], "--version"], timeout=120)
        if code == 0 and job["to"] not in log:
            code, log = 1, f"после установки другая версия: {log.strip()}"
    if code != 0:
        back, back_log = _install(job, job["old_wheels"], job["from"], run)
        return result | {"ok": False, "error": "install", "message": log.strip()[-600:],
                         "rolled_back": back == 0, **({} if back == 0 else {"rollback_log": back_log[-600:]})}
    setup = [job["hrs"], "setup", "--skill-dir", job["skill_dir"], *(["--from-plugin"] if job.get("from_plugin") else [])]
    code, log = run(setup, timeout=300)
    return result | {"ok": True, "setup_ok": code == 0, **({} if code == 0 else {"setup_log": log.strip()[-600:]})}


def main(job_path: str) -> int:
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    deadline = time.monotonic() + WAIT_PARENT
    while alive(job.get("parent", 0)) and time.monotonic() < deadline:
        time.sleep(1)
    result = apply(job)
    out = Path(job["result"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
