"""Smoke test of the seams with real agents: Claude Code (`claude -p`) and Codex (`codex exec`).

Each agent gets only the hrs MCP server (no other servers, no changes to its saved MCP config), a
throwaway HRSHTURMAN_HOME and keychain, and a local key server with the real library. One short task:
check the key, enter it, read stage 0, ask the first questionnaire block. The verdict comes from the hrs
data the agent left behind, not from what it says.

    uv run --with pytest python tests/agent_smoke.py [claude] [codex] [scripted]

`scripted` runs the same steps through an MCP client over stdio without a model: it checks the smoke itself.

Login: the agents use their own login. To keep ~/.claude and ~/.codex untouched, log in once into
separate folders and pass them: CLAUDE_CONFIG_DIR=... and CODEX_HOME=... (see docs/AGENT-SMOKE.md).
Costs a few thousand tokens of the subscription per agent.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
from hrs_keys import bundle
from hrs_keys.app import create_app
from hrs_keys.service import KeyServer, Settings, generate_signing_key

HERE = Path(__file__).resolve().parent
LIBRARY = HERE.parents[1] / "MetaProject_Job_Search_—_Specifications_and_Library" / "ФИНАЛЬНОЕ" / "MD библиотека" / "Библиотека навыков"
HRS = Path(sys.executable).with_name("hrs")
TASK = ("Это проверка JobShturman. Сделай по порядку, ничего не спрашивая у меня: 1) license_status; "
        "2) license_login с ключом {key}; 3) stage_procedure с stage_id=0, процитируй первую строку; "
        "4) ui_mode с mode=chat; 5) intake_ask с block=pains; если формы нет, сохрани ответ «1, 5» через intake_answer. "
        "В конце одной строкой: какие инструменты вызвал.")
TIMEOUT = 300


def key_server(root: Path):
    settings = Settings(data=root / "server", secret=os.urandom(24).hex().encode(), admin_token="smoke",
                        signing_key=root / "server" / "signing.pem", renew_url="https://example.test/renew")
    public = generate_signing_key(settings.signing_key)
    ks = KeyServer(settings)
    ks.publish(bundle.build(LIBRARY, version="smoke.1")[1], note="smoke")
    key = ks.issue("self", order_ref="agent smoke")["key"]
    server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=0, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    return f"http://127.0.0.1:{port}", public, key, server


def hrs_env(root: Path, url: str, public: str) -> dict:
    env = {"HRSHTURMAN_HOME": str(root / "hrs"), "HRS_KEY_SERVER": url, "HRS_BUNDLE_PUBKEYS": public}
    if sys.platform == "darwin":
        chain = root / "smoke.keychain-db"
        subprocess.run(["security", "create-keychain", "-p", "smoke", str(chain)], check=True)
        env["HRS_KEYCHAIN"] = str(chain)
    else:
        env["HRS_KEYSTORE"] = "file"
    return env


def run_claude(root: Path, env: dict, prompt: str) -> subprocess.CompletedProcess:
    config = root / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {"hrshturman": {"command": str(HRS), "args": ["serve"], "env": env}}}))
    return subprocess.run(["claude", "-p", prompt, "--strict-mcp-config", "--mcp-config", str(config),
                           "--allowedTools", "mcp__hrshturman", "--output-format", "text"],
                          cwd=root / "work", capture_output=True, text=True, timeout=TIMEOUT, check=False)


def run_codex(root: Path, env: dict, prompt: str) -> subprocess.CompletedProcess:
    overrides = [f'mcp_servers.hrshturman.command="{HRS}"', 'mcp_servers.hrshturman.args=["serve"]',
                 'mcp_servers.hrshturman.default_tools_approval_mode="approve"',
                 "mcp_servers.hrshturman.env=" + "{" + ", ".join(f'{k}="{v}"' for k, v in env.items()) + "}"]
    args = ["codex", "exec", "--skip-git-repo-check", "--sandbox", "read-only"]
    for item in overrides:
        args += ["-c", item]
    return subprocess.run([*args, prompt], cwd=root / "work", capture_output=True, text=True, timeout=TIMEOUT,
                          stdin=subprocess.DEVNULL,
                          check=False)


def run_scripted(root: Path, env: dict, key: str) -> subprocess.CompletedProcess:
    import asyncio

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def go():
        params = StdioServerParameters(command=str(HRS), args=["serve"], env=os.environ | env)
        async with stdio_client(params) as (read, write), ClientSession(read, write) as s:
            await s.initialize()
            called = []
            for name, args in [("license_status", {}), ("license_login", {"key": key}),
                               ("stage_procedure", {"stage_id": 0}), ("ui_mode", {"mode": "chat"}),
                               ("intake_answer", {"block": "pains", "answer": "1, 5"})]:
                result = await s.call_tool(name, args)
                called.append(name + ("!" if result.is_error else ""))
            return ", ".join(called)
    said = asyncio.run(go())
    return subprocess.CompletedProcess([], 0, said, "")


def verdict(root: Path, env: dict) -> dict:
    """What the agent actually did, read from hrs itself."""
    out = subprocess.run([str(HRS), "license"], env=os.environ | env, capture_output=True, text=True, check=False)
    lic = json.loads(out.stdout or "{}")
    checks = {"key_accepted": lic.get("state") == "active" and lic.get("methodology") is True,
              "key_in_keychain": lic.get("key_storage") == ("keychain" if sys.platform == "darwin" else "file")}
    settings = root / "hrs" / "settings.json"
    checks["ui_mode_saved"] = settings.is_file() and json.loads(settings.read_text()).get("ui_mode") == "chat"
    script = ("from hrs_cli import paths\nfrom hrshturman_core import Store, intake\n"
              "s = paths.settings(); i = intake.get_intake(Store(s.db_path).init(), s.candidate_id)\n"
              "print(i['pain_ids'] if i else None)")
    pains = subprocess.run([sys.executable, "-c", script], env=os.environ | env, capture_output=True, text=True,
                           check=False).stdout.strip()
    checks["questionnaire_answered"] = pains not in ("", "None", "[]")
    return checks


def main(agents: list[str]) -> int:
    failed = 0
    for agent in agents:
        if agent != "scripted" and not shutil.which(agent):
            print(f"{agent}: не найден")
            failed += 1
            continue
        root = Path(tempfile.mkdtemp(prefix=f"hrs-smoke-{agent}-"))
        (root / "work").mkdir()
        url, public, key, server = key_server(root)
        env = hrs_env(root, url, public)
        try:
            started = time.time()
            if agent == "scripted":
                done = run_scripted(root, env, key)
            else:
                done = (run_claude if agent == "claude" else run_codex)(root, env, TASK.format(key=key))
            said = (done.stdout + done.stderr).strip()
            if "Not logged in" in said or "401 Unauthorized" in said:
                print(f"{agent}: BLOCKED — агент не вошёл в аккаунт в этой папке настроек")
                failed += 1
                continue
            checks = verdict(root, env)
            ok = all(checks.values())
            failed += not ok
            print(f"{agent}: {'PASS' if ok else 'FAIL'} за {time.time() - started:.0f} с · " +
                  ", ".join(f"{k}={'да' if v else 'нет'}" for k, v in checks.items()))
            print("  ответ агента: " + said.splitlines()[-1][:300] if said else "  ответ агента пуст")
        finally:
            server.should_exit = True
            if "HRS_KEYCHAIN" in env:
                subprocess.run(["security", "delete-keychain", env["HRS_KEYCHAIN"]], check=False)
            shutil.rmtree(root, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["claude", "codex"]))
