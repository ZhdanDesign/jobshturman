import importlib.util
import sys
from pathlib import Path

# Synthetic Telegram pages and the mock HTTP transport come from the collector's own fixtures.
_path = Path(__file__).resolve().parents[2] / "collector" / "tests" / "conftest.py"
_spec = importlib.util.spec_from_file_location("collector_fixtures", _path)
sys.modules["collector_fixtures"] = _module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)


import pytest


@pytest.fixture(autouse=True)
def _file_keystore(monkeypatch):
    """Tests never touch the login Keychain (test_keystore.py uses a throwaway keychain file)
    and never reach the production key server."""
    monkeypatch.setenv("HRS_KEYSTORE", "file")
    monkeypatch.delenv("HRS_KEYCHAIN", raising=False)
    # Never the production key server: anything not given a test client fails locally.
    monkeypatch.setenv("HRS_KEY_SERVER", "http://127.0.0.1:9")
