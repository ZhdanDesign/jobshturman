"""`hrs setup` and the bootstrap: connect hrs to Claude Code, Codex, Grok Build, Hermes and OpenClaw without
breaking their configs."""
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from hrs_cli import setup

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "plugin" / "skills" / "hrshturman"
UV = shutil.which("uv")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("OPENCLAW_STATE_DIR", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))  # no real claude/codex
    return tmp_path


def fake_claude(home: Path) -> Path:
    """A `claude` that keeps registered MCP servers in a JSON file, like `claude mcp add/get`."""
    bin_dir = home / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "claude"
    script.write_text(f"""#!{sys.executable}
import json, sys
from pathlib import Path
db = Path({str(home / "claude-mcp.json")!r})
servers = json.loads(db.read_text()) if db.exists() else {{}}
args = sys.argv[1:]
if args[:2] == ["mcp", "get"]:
    sys.exit(0 if args[2] in servers else 1)
if args[:2] == ["mcp", "add"]:
    name = args[args.index("--") - 1]
    servers[name] = args[args.index("--") + 1:]
    db.write_text(json.dumps(servers))
    sys.exit(0)
sys.exit(2)
""")
    script.chmod(0o755)
    return home / "claude-mcp.json"


def test_codex_block_is_appended_once_and_keeps_the_users_settings(home):
    config = home / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text('# мои настройки\nmodel = "gpt-5"\n\n[mcp_servers.other]\ncommand = "x"\n', encoding="utf-8")

    first = setup.setup_codex(r"C:\Users\Иван\.local\bin\hrs.exe")
    second = setup.setup_codex("/elsewhere/hrs")

    assert (first["status"], second["status"]) == ("added", "already")
    text = config.read_text(encoding="utf-8")
    assert text.startswith("# мои настройки\n")
    data = tomllib.loads(text)
    assert data["model"] == "gpt-5" and data["mcp_servers"]["other"]["command"] == "x"
    ours = data["mcp_servers"]["hrshturman"]
    assert ours["command"] == r"C:\Users\Иван\.local\bin\hrs.exe"
    assert ours["args"] == ["serve"] and ours["default_tools_approval_mode"] == "approve"
    assert ours["tool_timeout_sec"] == 600  # a person filling an MCP form needs more than 60 s
    assert (config.parent / "config.toml.bak-hrs").read_text(encoding="utf-8").startswith("# мои настройки")


def test_codex_is_skipped_when_not_installed(home):
    assert setup.setup_codex("/x/hrs") == {"agent": "codex", "status": "not_found"}
    assert not (home / ".codex").exists()


def test_claude_server_is_added_once(home):
    db = fake_claude(home)
    assert setup.setup_claude("/x/hrs")["status"] == "added"
    assert setup.setup_claude("/y/hrs")["status"] == "already"
    assert json.loads(db.read_text()) == {"hrshturman": ["/x/hrs", "serve"]}


def test_skill_goes_next_to_each_agent_but_not_twice_into_claude_from_the_plugin(home):
    fake_claude(home)
    report = setup.run(str(SKILL))
    assert {s["skill"] for s in report["skills"]} == {
        str(home / ".agents" / "skills" / "hrshturman"), str(home / ".claude" / "skills" / "hrshturman")}
    assert (home / ".claude" / "skills" / "hrshturman" / "scripts" / "bootstrap.ps1").is_file()

    shutil.rmtree(home / ".claude" / "skills")
    report = setup.run(str(SKILL), from_plugin=True)
    assert [s["skill"] for s in report["skills"]] == [str(home / ".agents" / "skills" / "hrshturman")]
    assert not (home / ".claude" / "skills").exists()
    assert report["agents"][0]["status"] == "already"  # the plugin carries no MCP server: setup registers it


def test_claude_skill_follows_claude_config_dir(home, monkeypatch):
    fake_claude(home)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "claude-config"))
    report = setup.run(str(SKILL))
    assert str(home / "claude-config" / "skills" / "hrshturman") in {s["skill"] for s in report["skills"]}
    assert not (home / ".claude" / "skills").exists()


def fake_npx(home: Path) -> Path:
    npx = home / "bin" / "npx"
    npx.parent.mkdir(exist_ok=True)
    npx.write_text("#!/bin/sh\n")
    npx.chmod(0o755)
    return npx


@pytest.mark.skipif(os.name == "nt", reason="POSIX npx path")
def test_browser_is_registered_for_both_agents_with_its_own_profile(home, monkeypatch):
    monkeypatch.setenv("HRSHTURMAN_HOME", str(home / "hrs-data"))
    db = fake_claude(home)
    npx = fake_npx(home)
    (home / ".codex").mkdir()

    first, second = ([r for r in setup.setup_browser() if r["status"] != "not_found"] for _ in range(2))

    assert [r["status"] for r in first] == ["added", "added"]
    assert [r["status"] for r in second] == ["already", "already"]
    claude_args = json.loads(db.read_text())["playwright"]
    codex = tomllib.loads((home / ".codex" / "config.toml").read_text(encoding="utf-8"))["mcp_servers"]["playwright"]
    assert claude_args[0] == codex["command"] == str(npx)
    assert claude_args[1:] == codex["args"]
    assert setup.PLAYWRIGHT_MCP in codex["args"] and "--snapshot-mode" in codex["args"]
    assert codex["args"][codex["args"].index("--user-data-dir") + 1] == str(home / "hrs-data" / "browser-profile")


def test_browser_needs_node(home):
    fake_claude(home)
    assert [r["status"] for r in setup.setup_browser()] == ["needs_node"]
    assert setup.run()["browser"][0]["status"] == "needs_node"


def test_skill_frontmatter_is_valid():
    head = (SKILL / "SKILL.md").read_text(encoding="utf-8").split("---")[1]
    fields = dict(line.split(": ", 1) for line in head.strip().splitlines())
    assert fields["name"] == "hrshturman" and len(fields["description"]) < 1024


@pytest.mark.skipif(os.name == "nt" or not UV, reason="POSIX bootstrap, needs uv")
def test_bootstrap_keeps_an_installed_hrs_and_reports_as_json(home):
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    hrs = bin_dir / "hrs"
    hrs.write_text(f"#!{sys.executable}\nimport json, sys\nprint(json.dumps({{'argv': sys.argv[1:]}}))\n")
    hrs.chmod(0o755)
    env = {**os.environ, "HOME": str(home), "HRS_SOURCE": "", "UV_TOOL_BIN_DIR": str(bin_dir),
           "PATH": os.pathsep.join([str(Path(UV).parent), "/usr/bin", "/bin"])}
    out = subprocess.run(["sh", str(SKILL / "scripts" / "bootstrap.sh")], env=env, capture_output=True, text=True,
                         check=False)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {"argv": ["setup", "--skill-dir", str(SKILL)]}

    env["HRS_SOURCE"] = str(home / "nope")
    out = subprocess.run(["sh", str(SKILL / "scripts" / "bootstrap.sh")], env=env, capture_output=True, text=True,
                         check=False)
    assert out.returncode == 1 and "не нашёл источник" in out.stderr


def test_grok_block_is_appended_once(home):
    config = home / ".grok" / "config.toml"
    config.parent.mkdir()
    config.write_text('model = "grok-4"\n', encoding="utf-8")
    assert setup.setup_grok("/x/hrs")["status"] == "added"
    assert setup.setup_grok("/y/hrs")["status"] == "already"
    data = tomllib.loads(config.read_text(encoding="utf-8"))
    assert data["model"] == "grok-4" and data["mcp_servers"]["hrshturman"] == {"command": "/x/hrs", "args": ["serve"]}


def test_hermes_entry_goes_under_mcp_servers_once(home):
    config = home / ".hermes" / "config.yaml"
    config.parent.mkdir()
    config.write_text("model: nous\nmcp_servers:\n  github:\n    command: npx\nterminal:\n  backend: local\n",
                      encoding="utf-8")
    assert setup.setup_hermes(r"C:\Users\Иван\hrs.exe")["status"] == "added"
    assert setup.setup_hermes("/y/hrs")["status"] == "already"
    assert config.read_text(encoding="utf-8") == (
        'model: nous\nmcp_servers:\n  hrshturman:\n    command: "C:\\\\Users\\\\Иван\\\\hrs.exe"\n'
        '    args: ["serve"]\n  github:\n    command: npx\nterminal:\n  backend: local\n')
    (home / ".hermes" / "config.yaml").write_text("mcp_servers: {}\n", encoding="utf-8")
    assert setup.setup_hermes("/x/hrs")["status"] == "added"
    assert config.read_text(encoding="utf-8").startswith("mcp_servers:\n  hrshturman:\n")
    config.write_text("mcp_servers: {github: {command: npx}}\n", encoding="utf-8")
    assert setup.setup_hermes("/x/hrs")["status"] == "manual"  # flow style: not edited by hand-rolled YAML
    config.unlink()
    assert setup.setup_hermes("/x/hrs")["status"] == "added"
    assert config.read_text(encoding="utf-8").startswith("mcp_servers:\n  hrshturman:\n")


def test_openclaw_server_is_set_through_its_cli(home):
    bin_dir = home / "bin"
    bin_dir.mkdir(exist_ok=True)
    db = home / "openclaw-mcp.json"
    script = bin_dir / "openclaw"
    script.write_text(f"""#!{sys.executable}
import json, sys
from pathlib import Path
db = Path({str(db)!r})
servers = json.loads(db.read_text()) if db.exists() else {{}}
args = sys.argv[1:]
if args[:2] == ["mcp", "show"]:
    if args[2] not in servers:
        sys.exit(1)
    print(json.dumps(servers[args[2]]))
    sys.exit(0)
if args[:2] == ["mcp", "set"]:
    servers[args[2]] = json.loads(args[3])
    db.write_text(json.dumps(servers))
    sys.exit(0)
sys.exit(2)
""")
    script.chmod(0o755)
    assert setup.setup_openclaw("/x/hrs")["status"] == "added"
    assert setup.setup_openclaw("/x/hrs")["status"] == "already"
    assert json.loads(db.read_text()) == {"hrshturman": {"command": "/x/hrs", "args": ["serve"]}}


def test_skill_goes_to_grok_hermes_and_openclaw_when_they_are_here(home):
    for folder in (".grok", ".hermes", ".openclaw"):
        (home / folder).mkdir()
    report = setup.run(str(SKILL))
    assert {s["skill"] for s in report["skills"]} == {
        str(home / folder / "skills" / "hrshturman") for folder in (".agents", ".grok", ".hermes")}
