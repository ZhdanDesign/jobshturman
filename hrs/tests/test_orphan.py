"""An `hrs serve` whose agent is gone ends by itself, so an old version does not stay in memory after an update."""
import os
import subprocess
import sys
import time

import pytest

from hrs_cli import main

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Windows does not hand orphans to another parent")


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_serve_without_its_agent_ends(tmp_path):
    started = tmp_path / "started"
    child = (f"import sys, threading; sys.path[:0] = {sys.path!r}; from hrs_cli import main; "
             f"main.exit_with_parent(interval=0.1); open({str(started)!r}, 'w').close(); threading.Event().wait(30)")
    # The shell is the agent: it starts hrs in the background and quits at once.
    out = subprocess.run(["sh", "-c", f'"{sys.executable}" -c "{child}" >/dev/null 2>&1 & echo $!; sleep 1'],
                         capture_output=True, text=True, check=True)
    pid = int(out.stdout.strip())
    deadline = time.time() + 10
    while _alive(pid) and time.time() < deadline:
        time.sleep(0.1)
    assert started.exists() and not _alive(pid)


def test_serve_with_its_agent_keeps_running():
    stopped = []
    ppids = iter([42, 42, 42])
    main.exit_with_parent(interval=0, getppid=lambda: next(ppids, 42), stop=stopped.append, rounds=2)
    assert stopped == []
    ppids = iter([42, 1])
    main.exit_with_parent(interval=0, getppid=lambda: next(ppids, 1), stop=stopped.append, rounds=2)
    assert stopped == [0]
