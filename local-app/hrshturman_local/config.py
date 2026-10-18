"""Local settings from environment variables, with defaults for a single-user Mac install."""
import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LIBRARY = (PROJECT_ROOT / "Метапроект_поиска_работы_—_ТЗ_и_библиотека" / "ФИНАЛЬНОЕ"
                   / "MD библиотека" / "Библиотека навыков")


@dataclass(frozen=True)
class Settings:
    workspace: Path
    candidate_id: str
    timezone: str
    library: Path
    collector_url: str = "http://127.0.0.1:8765"
    collector_token: str | None = None

    @property
    def db_path(self):
        return self.workspace / "workspace.sqlite3"


def load():
    return Settings(
        workspace=Path(os.environ.get("HRSHTURMAN_WORKSPACE", "~/HRShturman/workspace")).expanduser(),
        candidate_id=os.environ.get("HRSHTURMAN_CANDIDATE", "me"),
        timezone=os.environ.get("HRSHTURMAN_TZ", "Europe/Moscow"),
        library=Path(os.environ.get("HRSHTURMAN_LIBRARY", DEFAULT_LIBRARY)).expanduser(),
        collector_url=os.environ.get("HRSHTURMAN_COLLECTOR_URL", "http://127.0.0.1:8765"),
        collector_token=os.environ.get("HRS_COLLECTOR_TOKEN") or None,
    )
