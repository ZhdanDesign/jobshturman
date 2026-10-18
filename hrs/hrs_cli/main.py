"""hrs command line.

  hrs serve [--http --port 8742]   MCP server for Claude Code, Codex and other agents (stdio by default)
  hrs collect <command> ...        vacancy collector: sources, runs, vacancies (see `hrs collect --help`)
  hrs doc import <file>            read a PDF or Word file into the local workspace
  hrs paths                        where hrs keeps its data
  hrs setup [--skill-dir DIR]      connect hrs to Claude Code and Codex, install the skill
  hrs login <key> [--server URL]   enter the access key and download the methodology
  hrs sync                         check the key and download a newer methodology
  hrs license                      key state: tariff, end date, renewal link
  hrs stage <n>                    procedure of one methodology stage (number or id), never the whole bundle
  hrs ui [--view intake|progress] [--no-open]   the candidate's page in a browser (127.0.0.1, one-time link)
  hrs selftest [--library DIR [--include DEST=SRC]] [--json]   contract check of the methodology bundle
  hrs schedule install|remove|status   regular tasks in the system scheduler (launchd, Task Scheduler, systemd)
  hrs schedule run <task>              run one task now: license, collect, digest, reminders
  hrs schedule set <task> [--time HH:MM ...] [--on|--off]   when a task runs
"""
import argparse
import json
import sys

from hrshturman_core import DomainError
from hrshturman_core.brand import PRODUCT_NAME

from . import docs, paths


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["collect"]:
        from hrs_collector import cli as collector_cli
        s = paths.settings()
        return collector_cli.main(["--data", str(paths.collector_dir()), "--candidate", s.candidate_id, *argv[1:]])

    ap = argparse.ArgumentParser(prog="hrs", description=f"{PRODUCT_NAME}: локальный помощник поиска работы для кодового агента")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("serve", help="MCP-сервер для агента (stdio; --http для streamable HTTP на 127.0.0.1)")
    p.add_argument("--http", action="store_true")
    p.add_argument("--port", type=int, default=8742)
    sub.add_parser("collect", help="сборщик вакансий: hrs collect --help")
    p = sub.add_parser("doc", help="документы кандидата")
    dsub = p.add_subparsers(dest="doc_cmd", required=True)
    p = dsub.add_parser("import", help="прочитать PDF/Word в Markdown и сохранить локально")
    p.add_argument("file")
    sub.add_parser("paths", help="где лежат данные")
    p = sub.add_parser("setup", help="подключить hrs к Claude Code и Codex и поставить навык")
    p.add_argument("--skill-dir", help="папка установочного навыка (с SKILL.md)")
    p.add_argument("--from-plugin", action="store_true", help="навык в Claude Code уже даёт плагин")
    p = sub.add_parser("login", help="ввести ключ доступа и скачать методику")
    p.add_argument("key")
    p.add_argument("--server", help=f"адрес сервера ключей (по умолчанию сервер {PRODUCT_NAME})")
    sub.add_parser("sync", help="проверить ключ и скачать новую методику")
    sub.add_parser("license", help="состояние ключа")
    p = sub.add_parser("stage", help="процедура одного этапа методики")
    p.add_argument("stage_id", help="номер этапа (с 0) или его id")
    p = sub.add_parser("selftest", help="проверка пакета методики: все этапы манифеста, выдача, ссылки, водяной знак")
    p.add_argument("--library", help="собрать пакет из папки библиотеки вместо скачанного")
    p.add_argument("--include", action="append", default=[], metavar="DEST=SRC",
                   help="добавить файл в собранный пакет, как hrs-keys bundle build --include")
    p.add_argument("--json", action="store_true", help="полный отчёт JSON")
    p = sub.add_parser("ui", help="страница анкеты и маршрута в браузере")
    p.add_argument("--view", default="intake", choices=["intake", "progress"])
    p.add_argument("--no-open", action="store_true", help="только напечатать ссылку")
    p = sub.add_parser("schedule", help="регулярные задачи: сбор, сводка, напоминания, проверка ключа")
    ssub = p.add_subparsers(dest="schedule_cmd", required=True)
    p = ssub.add_parser("install", help="поставить задачи в системный планировщик")
    p.add_argument("--dry-run", action="store_true", help="только показать, что будет сделано")
    ssub.add_parser("remove", help="убрать задачи из системного планировщика")
    ssub.add_parser("status", help="расписание, последние запуски, задачи агентов")
    p = ssub.add_parser("run", help="выполнить задачу сейчас")
    p.add_argument("task", choices=["license", "collect", "digest", "reminders"])
    p = ssub.add_parser("set", help="изменить время или выключить задачу")
    p.add_argument("task", choices=["license", "collect", "digest", "reminders"])
    p.add_argument("--time", action="append", help="HH:MM, можно несколько")
    p.add_argument("--on", dest="enabled", action="store_true", default=None)
    p.add_argument("--off", dest="enabled", action="store_false")
    args = ap.parse_args(argv)

    if args.cmd == "schedule":
        from hrshturman_core import schedule as core

        from . import schedule
        try:
            if args.schedule_cmd == "install":
                _print(schedule.install(dry_run=args.dry_run))
            elif args.schedule_cmd == "remove":
                _print(schedule.remove())
            elif args.schedule_cmd == "status":
                _print(schedule.status())
            elif args.schedule_cmd == "run":
                _print(schedule.run(args.task))
            else:
                s, store = schedule.store_for()
                core.set_task(store, s.candidate_id, args.task, times=args.time, enabled=args.enabled,
                              actor="candidate")
                _print(schedule.status())
        except DomainError as exc:
            _print({"error": exc.code, "message": str(exc)})
            return 1
        return 0

    if args.cmd in ("login", "sync", "license"):
        from . import license
        try:
            if args.cmd == "login":
                result = license.login(args.key, args.server)
            elif args.cmd == "sync":
                result = license.sync()
            else:
                result = license.refresh(force=True)
        except DomainError as exc:
            _print({"error": exc.code, "message": str(exc)})
            return 1
        _print(result)
        return 0 if result.get("methodology") else 1

    if args.cmd == "stage":
        from hrshturman_local.mcp_server import stage_text

        from .server import Methodology
        try:
            print(stage_text(Methodology(paths.settings()), args.stage_id))
        except DomainError as exc:
            _print({"error": exc.code, "message": str(exc)})
            return 1
        return 0
    if args.cmd == "selftest":
        from pathlib import Path

        from . import selftest
        try:
            extra = {k: Path(v) for k, v in (item.split("=", 1) for item in args.include)}
            data = selftest.library_bundle(Path(args.library), extra) if args.library else selftest.installed_bundle()
        except DomainError as exc:
            _print({"error": exc.code, "message": str(exc)})
            return 1
        report = selftest.run(data, selftest.tool_names())
        if args.json:
            _print(report)
        else:
            print(selftest.summary(report))
        return 0 if report["ok"] else 1
    if args.cmd == "ui":
        import threading

        from hrshturman_core import Store
        from hrshturman_core.workspace import create_candidate

        from . import web
        from .server import Methodology
        s = paths.settings()
        store = Store(s.db_path).init()
        create_candidate(store, s.candidate_id, s.timezone, actor="installer")
        url = web.WebUI(store, s.candidate_id, Methodology(s)).link(args.view)
        print(url, flush=True)
        if not args.no_open:
            web.open_external(url)
        threading.Event().wait()
        return 0
    if args.cmd == "serve":
        from .server import build
        server = build()
        if args.http:
            server.run("streamable-http", host="127.0.0.1", port=args.port)
        else:
            server.run("stdio")
        return 0
    if args.cmd == "doc":
        s = paths.settings()
        try:
            result = docs.import_document(args.file, paths.documents_dir(s))
        except DomainError as exc:
            _print({"error": exc.code, "message": str(exc)})
            return 1
        result.pop("markdown")
        _print(result)
        return 0
    if args.cmd == "paths":
        s = paths.settings()
        _print({"data": str(paths.data_dir()), "workspace": str(s.workspace), "collector": str(paths.collector_dir()),
                "documents": str(paths.documents_dir(s)), "library": str(s.library), "candidate_id": s.candidate_id})
        return 0
    if args.cmd == "setup":
        from . import setup
        report = setup.run(args.skill_dir, from_plugin=args.from_plugin)
        _print(report)
        return 1 if any(r["status"] == "error" for r in report["agents"] + report["skills"]) else 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
