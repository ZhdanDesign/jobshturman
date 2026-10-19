"""Connect hrs to the coding agents found on this machine.

`hrs setup` is idempotent and is what the bootstrap scripts run after installing hrs:

- Claude Code: `claude mcp add --scope user hrshturman -- <hrs> serve`, unless a server
  named hrshturman is already registered. The hrshturman plugin ships only the skill and
  relies on this too: a plugin `.mcp.json` can't point at ~/.local/bin on every OS.
- Codex: a `[mcp_servers.hrshturman]` block appended to ~/.codex/config.toml with tool
  approval set to "approve"; without it Codex refuses MCP calls in non-interactive runs.
- Skill: the installer skill folder is copied to ~/.agents/skills, and to ~/.claude/skills
  unless it came with the plugin.
- Browser: a Playwright MCP server named playwright, so the agent applies on hh.ru itself.
  It runs Chrome with its own profile under the hrs data folder: the person signs in once,
  their everyday Chrome stays untouched. Needs Node.js (npx); without it the report says needs_node.

The servers are registered with the absolute path to hrs, so agents started from the
Dock or Start menu find it even when ~/.local/bin is not on their PATH.
"""
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from . import paths

SERVER = "hrshturman"
SKILL = "hrshturman"
BROWSER = "playwright"
PLAYWRIGHT_MCP = "@playwright/mcp@0.0.82"


def hrs_command() -> str:
    me = Path(sys.argv[0])
    if me.name.lower() in ("hrs", "hrs.exe") and me.is_file():
        return str(me.absolute())
    return shutil.which("hrs") or str(me.absolute())


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def claude_home() -> Path:
    """Where Claude Code reads user skills; `claude mcp add` follows the same variable."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def browser_server() -> tuple[str, list[str]] | None:
    """Command and args for the Playwright MCP server, or None without Node.js.
    The flags keep pages out of the model's context unless asked: a full hh.ru vacancy snapshot costs ~11k tokens."""
    npx = shutil.which("npx")
    if not npx:
        return None
    args = ["-y", PLAYWRIGHT_MCP, "--browser=chrome", "--user-data-dir", str(paths.data_dir() / "browser-profile"),
            "--no-webmcp", "--allow-unrestricted-file-access", "--snapshot-mode", "none", "--image-responses", "omit",
            "--codegen", "none", "--console-level", "error"]
    if os.name == "nt":  # npx is a .cmd script; agents start it through cmd
        return "cmd", ["/c", npx, *args]
    return npx, args


def setup_codex(command: str, name: str = SERVER, args: list[str] | None = None) -> dict:
    home = codex_home()
    if not home.exists() and not shutil.which("codex"):
        return {"agent": "codex", "status": "not_found"}
    config = home / "config.toml"
    text = config.read_text(encoding="utf-8") if config.exists() else ""
    current = tomllib.loads(text).get("mcp_servers", {}).get(name) if text else None
    if current is not None:
        return {"agent": "codex", "status": "already", "command": current.get("command"), "config": str(config)}
    block = (f"\n[mcp_servers.{name}]\n"
             f"command = {_toml_str(command)}\n"
             f"args = [{', '.join(_toml_str(a) for a in (args or ['serve']))}]\n"
             "startup_timeout_sec = 30\n"
             "tool_timeout_sec = 600\n"
             'default_tools_approval_mode = "approve"\n')
    home.mkdir(parents=True, exist_ok=True)
    if text:
        shutil.copy2(config, config.with_name("config.toml.bak-hrs"))
    config.write_text(text.rstrip("\n") + ("\n" if text else "") + block, encoding="utf-8")
    tomllib.loads(config.read_text(encoding="utf-8"))  # never leave a broken config behind
    return {"agent": "codex", "status": "added", "command": command, "config": str(config)}


def setup_claude(command: str, name: str = SERVER, args: list[str] | None = None) -> dict:
    claude = shutil.which("claude")
    if not claude:
        return {"agent": "claude", "status": "not_found"}
    listed = subprocess.run([claude, "mcp", "get", name], capture_output=True, text=True, timeout=60, check=False)
    if listed.returncode == 0:
        return {"agent": "claude", "status": "already"}
    added = subprocess.run([claude, "mcp", "add", "--scope", "user", name, "--", command, *(args or ["serve"])],
                           capture_output=True, text=True, timeout=60, check=False)
    if added.returncode != 0:
        return {"agent": "claude", "status": "error", "message": (added.stderr or added.stdout).strip()}
    return {"agent": "claude", "status": "added", "command": command}


def setup_browser() -> list[dict]:
    server = browser_server()
    if server is None:
        return [{"agent": "browser", "status": "needs_node",
                 "message": "Нет Node.js: агент не сможет откликаться через браузер. Поставьте Node.js LTS и повторите hrs setup"}]
    command, args = server
    return [setup_claude(command, name=BROWSER, args=args), setup_codex(command, name=BROWSER, args=args)]


def install_skill(skill_dir: Path, targets: list[Path]) -> list[dict]:
    if not (skill_dir / "SKILL.md").is_file():
        return [{"skill": str(skill_dir), "status": "error", "message": "нет SKILL.md"}]
    done = []
    for root in targets:
        dest = root / SKILL
        if dest.resolve() == skill_dir.resolve():
            done.append({"skill": str(dest), "status": "already"})
            continue
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(skill_dir, dest)
        done.append({"skill": str(dest), "status": "installed"})
    return done


def run(skill_dir: str | None = None, from_plugin: bool = False) -> dict:
    command = hrs_command()
    report = {"hrs": command, "agents": [setup_claude(command), setup_codex(command)],
              "browser": setup_browser(), "skills": []}
    if skill_dir:
        targets = [Path.home() / ".agents" / "skills"]
        claude_found = report["agents"][0]["status"] != "not_found"
        if claude_found and not from_plugin:
            targets.append(claude_home() / "skills")
        report["skills"] = install_skill(Path(skill_dir), targets)
    return report
