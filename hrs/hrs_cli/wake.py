"""The agent wakes up by itself: morning, day and evening the system scheduler starts the agent found on this
computer in its non-interactive mode, with only the JobShturman tools allowed.

The agent continues the person's work where it stopped, only what needs no person: collecting and ranking
vacancies, preparing the application sets. It sends nothing to employers (automatic applications are started
by the person in a conversation) and asks no questions. Its short answer goes into wake.md next to the digest
and into one system notification. Quiet hours and a paused search keep it asleep. When the agent's own login
has expired, the person gets a notification to log in again instead of a silent failure.
"""
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from hrshturman_core import schedule as core
from hrshturman_core.brand import PRODUCT_NAME

from . import paths

TIMEOUT = 20 * 60  # seconds
PARTS = ((12, "утро"), (17, "день"), (24, "вечер"))
PROMPT = (
    "Плановое пробуждение {product} ({part}). Ты работаешь по навыку hrshturman и инструментам {product}. "
    "Человека рядом нет: вопросов не задавай. Вызови license_status, agent_rules, reminders_list и digest_get, затем продолжи работу кандидата там, где она "
    "остановилась, но только то, что можно без человека: собери и оцени новые вакансии, подготовь комплекты "
    "откликов и черновики. Никому ничего не отправляй: ни откликов, ни сообщений работодателям, автоотклики "
    "запускает только человек. В конце ответь человеку двумя-тремя короткими фразами: что сделано и что ждёт "
    "его решения."
)
# Where agents are usually installed: the scheduler starts tasks with a bare PATH.
EXTRA_PATH = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", "~/.npm-global/bin", "~/.bun/bin",
              "~/AppData/Roaming/npm", "~/.cargo/bin")
SERVER = "hrshturman"
# What the agents print when their login is gone (Claude Code: "OAuth session expired", "Please run /login";
# Codex: "Not logged in", "401 Unauthorized").
LOGIN_SIGNS = ("oauth", "/login", "not logged in", "codex login", "invalid api key", "authentication_error",
               "unauthorized", " 401")
LOGIN = {
    "claude": "JobShturman не\u00a0смог начать работу: вход в\u00a0Claude истёк. Пожалуйста, войдите в\u00a0Claude: "
              "откройте Claude Code и\u00a0выполните команду /login. Дальше всё продолжится само.",
    "codex": "JobShturman не\u00a0смог начать работу: вход в\u00a0Codex истёк. Пожалуйста, войдите в\u00a0Codex: "
             "выполните в\u00a0терминале команду codex login. Дальше всё продолжится само.",
}


def _path_env() -> str:
    extra = [str(Path(p).expanduser()) for p in EXTRA_PATH]
    return os.pathsep.join([*extra, os.environ.get("PATH", "")])


def find_agent() -> tuple[str, str] | None:
    """(agent, executable) for the first agent found: Claude Code, then Codex."""
    path = _path_env()
    for agent, exe in (("claude", "claude"), ("codex", "codex")):
        found = shutil.which(exe, path=path)
        if found:
            return agent, found
    return None


def command(agent: str, exe: str, prompt: str) -> list[str]:
    """The non-interactive run of the agent with only the JobShturman tools allowed.
    Claude Code: dontAsk refuses everything not in --allowedTools instead of waiting for a person
    (https://code.claude.com/docs/en/permission-modes). Codex: exec keeps its read-only sandbox, never asks;
    `jbs setup` sets default_tools_approval_mode = "approve" for the hrshturman server."""
    if agent == "claude":
        return [exe, "-p", prompt, "--permission-mode", "dontAsk", "--allowedTools", f"mcp__{SERVER}__*",
                "--max-turns", "40"]
    return [exe, "exec", "--skip-git-repo-check", "-c", 'approval_policy="never"', prompt]


def part_of_day(local: datetime) -> str:
    return next(name for hour, name in PARTS if local.hour < hour)


def wake_file() -> Path:
    return paths.settings().workspace / "wake.md"


def _run(cmd, cwd, env):
    try:
        out = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=TIMEOUT, check=False,
                             stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return out.returncode, (out.stdout or out.stderr or "").strip()


def run(settings: dict, now: datetime | None = None, runner=_run, notify=None) -> dict:
    """One wake-up. Returns what happened; never raises."""
    local = (now or datetime.now(timezone.utc)).astimezone()
    if core.quiet(settings, local):
        return {"skipped": "quiet_hours"}
    found = find_agent()
    if found is None:
        return {"skipped": "no_agent"}
    agent, exe = found
    folder = paths.settings().workspace
    folder.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PATH=_path_env())
    code, text = runner(command(agent, exe, PROMPT.format(product=PRODUCT_NAME, part=part_of_day(local))),
                        str(folder), env)
    result = {"agent": agent, "code": code}
    if code != 0 and not logged_out(text):
        return result | {"error": text[-600:]}
    if code != 0:
        result["error"], answer = "login", LOGIN[agent]
    else:
        answer = text[-2000:]
    wake_file().write_text(f"# {PRODUCT_NAME}: {local:%d.%m.%Y %H:%M}\n\n{answer}\n", encoding="utf-8")
    if notify is None:
        from .schedule import notify
    first = " ".join(answer.split())[:220]
    result["notified"] = bool(first) and notify(PRODUCT_NAME, first)
    return result | {"file": str(wake_file())}


def logged_out(text: str) -> bool:
    """The agent did not start because its login expired."""
    low = text.lower()
    return any(sign in low for sign in LOGIN_SIGNS)
