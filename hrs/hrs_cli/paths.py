"""Where hrs keeps data on macOS, Windows and Linux.

Everything lives in one per-user application folder, outside the agent's working folder.
The old environment variables still win, so existing installs keep their data where it is.
"""
import dataclasses
import os
import sys
from pathlib import Path

from hrshturman_local import config


def data_dir() -> Path:
    if os.environ.get("HRSHTURMAN_HOME"):
        return Path(os.environ["HRSHTURMAN_HOME"]).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "HRShturman"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "HRShturman"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "hrshturman"


def collector_dir() -> Path:
    return Path(os.environ.get("HRS_COLLECTOR_DATA") or data_dir() / "collector").expanduser()


def documents_dir(settings) -> Path:
    return settings.workspace / "documents"


def settings():
    s = config.load()
    if "HRSHTURMAN_WORKSPACE" not in os.environ:
        s = dataclasses.replace(s, workspace=data_dir() / "workspace")
    return s
