"""Connect hrs to the coding agents found on this machine.

`hrs setup` is idempotent and is what the bootstrap scripts run after installing hrs:

- Claude Code: `claude mcp add --scope user hrshturman -- <hrs> serve`, unless a server
  named hrshturman is already registered. The hrshturman plugin ships only the skill and
  relies on this too: a plugin `.mcp.json` can't point at ~/.local/bin on every OS.
- Codex: a `[mcp_servers.hrshturman]` block appended to ~/.codex/config.toml with tool
  approval set to "approve"; without it Codex refuses MCP calls in non-interactive runs.
- Grok Build: a `[mcp_servers.hrshturman]` block appended to ~/.grok/config.toml.
- Hermes: an `hrshturman` entry under `mcp_servers` in ~/.hermes/config.yaml.
- OpenClaw: `openclaw mcp set hrshturman <json>` (an `mcp.servers` entry in OpenClaw config).
- Schedule: the regular tasks, the agent's three wake-ups a day and the hourly watch go into the system
  scheduler at once (schedule.ensure); the person does not have to ask.
- Skill: the installer skill folder is copied to ~/.agents/skills (also read by DeepSeek Harness and other
  agents), to ~/.claude/skills unless it came with the plugin, and to the skills folders of Grok Build,
  Hermes and OpenClaw when they are on this machine.
- Rules: AGENTS.md and CLAUDE.md (which includes AGENTS.md) in the data folder, so an agent opened there and
  every scheduled wake read how to work with JobShturman. A file the person took over is left alone.
- Browser: a Playwright MCP server named playwright, so the agent applies on hh.ru itself.
  It runs Chrome with its own profile under the hrs data folder: the person signs in once,
  their everyday Chrome stays untouched. Needs Node.js (npx); without it the report says needs_node.

The servers are registered with the absolute path to hrs, so agents started from the
Dock or Start menu find it even when ~/.local/bin is not on their PATH.
"""
import json
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
    if me.name.lower() in ("jbs", "jbs.exe") and me.is_file():  # agents keep the old name: same program
        twin = me.with_name(me.name.lower().replace("jbs", "hrs"))
        if twin.is_file():
            return str(twin.absolute())
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
    The flags keep pages out of the model's context unless asked: a full hh.ru vacancy snapshot costs ~11k tokens.
    --allow-unrestricted-file-access stays: the resume the agent attaches on hh.ru lies in the data folder, and
    the agent's workspace roots (the only files Playwright allows by default) are usually another folder."""
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


def grok_home() -> Path:
    return Path.home() / ".grok"


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def openclaw_home() -> Path:
    return Path(os.environ.get("OPENCLAW_STATE_DIR") or Path.home() / ".openclaw")


def setup_grok(command: str, name: str = SERVER, args: list[str] | None = None) -> dict:
    home = grok_home()
    if not home.exists() and not shutil.which("grok"):
        return {"agent": "grok", "status": "not_found"}
    config = home / "config.toml"
    text = config.read_text(encoding="utf-8") if config.exists() else ""
    current = tomllib.loads(text).get("mcp_servers", {}).get(name) if text else None
    if current is not None:
        return {"agent": "grok", "status": "already", "config": str(config)}
    block = (f"\n[mcp_servers.{name}]\n"
             f"command = {_toml_str(command)}\n"
             f"args = [{', '.join(_toml_str(a) for a in (args or ['serve']))}]\n")
    home.mkdir(parents=True, exist_ok=True)
    if text:
        shutil.copy2(config, config.with_name("config.toml.bak-hrs"))
    config.write_text(text.rstrip("\n") + ("\n" if text else "") + block, encoding="utf-8")
    tomllib.loads(config.read_text(encoding="utf-8"))  # never leave a broken config behind
    return {"agent": "grok", "status": "added", "config": str(config)}


def _yaml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def setup_hermes(command: str, name: str = SERVER, args: list[str] | None = None) -> dict:
    """Hermes keeps MCP servers under the top-level `mcp_servers` key of config.yaml. Without a YAML library
    the entry goes in only where that is unambiguous: no such key yet, or the key itself on its own line."""
    home = hermes_home()
    if not home.exists() and not shutil.which("hermes"):
        return {"agent": "hermes", "status": "not_found"}
    config = home / "config.yaml"
    entry = (f"  {name}:\n    command: {_yaml_str(command)}\n"
             f"    args: [{', '.join(_yaml_str(a) for a in (args or ['serve']))}]\n")
    lines = config.read_text(encoding="utf-8").splitlines(keepends=True) if config.exists() else []
    key = next((i for i, line in enumerate(lines) if line.split("#")[0].rstrip() in ("mcp_servers:", "mcp_servers: {}")), None)
    if key is None and any(line.startswith("mcp_servers:") for line in lines):
        return {"agent": "hermes", "status": "manual", "config": str(config),
                "message": f"Добавьте сервер в mcp_servers файла {config} вручную:\n{entry}"}
    if key is not None:
        block = [line for line in lines[key + 1:]]
        end = next((j for j, line in enumerate(block) if line.strip() and not line.startswith((" ", "\t", "#"))), len(block))
        if any(line.strip().rstrip(":") == name and line.startswith("  ") and not line.startswith("   ")
               for line in block[:end]):
            return {"agent": "hermes", "status": "already", "config": str(config)}
        lines[key] = "mcp_servers:\n"
        lines[key + 1:key + 1] = [entry]
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines += ["mcp_servers:\n", entry]
    home.mkdir(parents=True, exist_ok=True)
    if config.exists():
        shutil.copy2(config, config.with_name("config.yaml.bak-hrs"))
    config.write_text("".join(lines), encoding="utf-8")
    return {"agent": "hermes", "status": "added", "config": str(config)}


def setup_openclaw(command: str, name: str = SERVER, args: list[str] | None = None) -> dict:
    openclaw = shutil.which("openclaw")
    if not openclaw:
        return {"agent": "openclaw", "status": "not_found"}
    shown = subprocess.run([openclaw, "mcp", "show", name], capture_output=True, text=True, timeout=60, check=False)
    if shown.returncode == 0 and command in shown.stdout:
        return {"agent": "openclaw", "status": "already"}
    value = json.dumps({"command": command, "args": args or ["serve"]}, ensure_ascii=False)
    added = subprocess.run([openclaw, "mcp", "set", name, value], capture_output=True, text=True, timeout=60, check=False)
    if added.returncode != 0:
        return {"agent": "openclaw", "status": "error", "message": (added.stderr or added.stdout).strip()}
    return {"agent": "openclaw", "status": "added"}


AGENTS = (setup_claude, setup_codex, setup_grok, setup_hermes, setup_openclaw)


def _each_agent(*args, **kwargs) -> list[dict]:
    """One agent's broken config (a TOML error in ~/.codex/config.toml, a CLI that hangs) must not stop the
    others from being connected: that agent gets status error in the report, the rest go on."""
    done = []
    for setup in AGENTS:
        agent = setup.__name__.removeprefix("setup_")
        try:
            done.append(setup(*args, **kwargs))
        except Exception as exc:  # noqa: BLE001 - reported per agent, the install goes on
            done.append({"agent": agent, "status": "error", "message": f"{type(exc).__name__}: {exc}"})
    return done


def setup_browser() -> list[dict]:
    server = browser_server()
    if server is None:
        return [{"agent": "browser", "status": "needs_node",
                 "message": "Нет Node.js: агент не сможет откликаться через браузер. Поставьте Node.js LTS и повторите jbs setup"}]
    command, args = server
    return _each_agent(command, name=BROWSER, args=args)


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
    report = {"hrs": command, "agents": _each_agent(command),
              "browser": setup_browser(), "skills": []}
    if skill_dir:
        targets = [Path.home() / ".agents" / "skills"]
        found = {a["agent"] for a in report["agents"] if a["status"] != "not_found"}
        if "claude" in found and not from_plugin:
            targets.append(claude_home() / "skills")
        homes = {"grok": grok_home(), "hermes": hermes_home(), "openclaw": openclaw_home()}
        targets += [homes[agent] / "skills" for agent in ("grok", "hermes", "openclaw") if agent in found]
        report["skills"] = install_skill(Path(skill_dir), targets)
    report["workspace_rules"] = setup_workspace_rules()
    report["schedule"] = setup_schedule()
    return report


MARK = "<!-- JobShturman"


def setup_workspace_rules() -> list[dict]:
    """AGENTS.md and CLAUDE.md in the data folder: an agent opened there, and every scheduled wake (it starts
    there), reads how to work with JobShturman. A file whose first line is no longer ours is the person's."""
    folder = paths.settings().workspace
    folder.mkdir(parents=True, exist_ok=True)
    done = []
    for name in ("AGENTS.md", "CLAUDE.md"):
        text = (Path(__file__).parent / "static" / f"workspace-{name}").read_text(encoding="utf-8")
        dest = folder / name
        old = dest.read_text(encoding="utf-8") if dest.is_file() else None
        if old is not None and not old.startswith(MARK):
            status = "kept"
        elif old == text:
            status = "already"
        else:
            dest.write_text(text, encoding="utf-8")
            status = "installed"
        done.append({"file": str(dest), "status": status})
    return done


def setup_schedule() -> dict:
    """Switch the schedule on right away: the person never has to ask for it (schedule.py)."""
    from . import schedule
    try:
        return schedule.ensure()
    except Exception as exc:  # noqa: BLE001 - a scheduler hiccup never fails the install
        return {"error": str(exc)}
