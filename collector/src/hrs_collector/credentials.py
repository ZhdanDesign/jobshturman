"""Local credentials for the single-user (local) mode.

Order: per-operation credentials from the caller > environment > credentials.json
in the data dir (written with mode 0600). The service mode never uses this file:
the web app passes credentials per operation and the collector keeps them only in
memory for that call.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .models import Credentials, HHCredentials, TelegramCredentials


def credentials_path(data_dir: Path) -> Path:
    return data_dir / "credentials.json"


def load_local(data_dir: Path) -> Credentials:
    data: dict = {}
    path = credentials_path(data_dir)
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    if os.environ.get("HRS_HH_ACCESS_TOKEN"):
        data["hh"] = {"access_token": os.environ["HRS_HH_ACCESS_TOKEN"]}
    if os.environ.get("HRS_TG_API_ID") and os.environ.get("HRS_TG_API_HASH") and os.environ.get("HRS_TG_SESSION"):
        data["telegram"] = {"api_id": int(os.environ["HRS_TG_API_ID"]), "api_hash": os.environ["HRS_TG_API_HASH"],
                            "session_string": os.environ["HRS_TG_SESSION"]}
    return Credentials.model_validate(data)


def save_local(data_dir: Path, creds: Credentials) -> Path:
    path = credentials_path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    payload: dict = {}
    if creds.hh:
        payload["hh"] = {"access_token": creds.hh.access_token.get_secret_value()}
    if creds.telegram:
        payload["telegram"] = {"api_id": creds.telegram.api_id,
                               "api_hash": creds.telegram.api_hash.get_secret_value(),
                               "session_string": creds.telegram.session_string.get_secret_value()}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.chmod(path, 0o600)
    return path


def merge(base: Credentials, override: Credentials | None) -> Credentials:
    if override is None:
        return base
    return Credentials(hh=override.hh or base.hh, telegram=override.telegram or base.telegram)


__all__ = ["Credentials", "HHCredentials", "TelegramCredentials", "load_local", "merge", "save_local"]
