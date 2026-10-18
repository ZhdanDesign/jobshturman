from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import os
import sys
from pathlib import Path

from . import credentials as creds_mod
from .models import CollectParams, Credentials, RunRequest, SourceIn, SourceSpec, TelegramCredentials
from .service import Collector, default_data_dir

REGISTRY_NAME = "02 Полный реестр Telegram каналов.csv"


def find_registry() -> Path | None:
    here = Path(__file__).resolve()
    for parent in here.parents:
        hits = sorted(parent.glob(f"*/ФИНАЛЬНОЕ/{REGISTRY_NAME}"))
        if hits:
            return hits[0]
    return None


def _print(obj) -> None:
    if hasattr(obj, "model_dump"):
        obj = obj.model_dump(mode="json")
    elif isinstance(obj, list):
        obj = [o.model_dump(mode="json") if hasattr(o, "model_dump") else o for o in obj]
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hrs-collector", description="JobShturman vacancy collector")
    ap.add_argument("--data", type=Path, default=None, help="data dir (default $HRS_COLLECTOR_DATA or ~/.hrs-collector)")
    ap.add_argument("--candidate", default="default")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("import-telegram", help="import the 210-channel CSV registry")
    p.add_argument("--csv", type=Path)

    p = sub.add_parser("add-hh", help="add an hh.ru search query")
    p.add_argument("source_id")
    p.add_argument("--text", required=True)
    p.add_argument("--area", help="hh area id: 1 Moscow, 2 SPb, 113 Russia")
    p.add_argument("--api", action="store_true", help="use api.hh.ru (needs HRS_HH_ACCESS_TOKEN)")

    p = sub.add_parser("add-linkedin", help="add a LinkedIn guest search query")
    p.add_argument("source_id")
    p.add_argument("--keywords", required=True)
    p.add_argument("--location")

    p = sub.add_parser("add-telegram", help="add one Telegram channel")
    p.add_argument("source_id")
    p.add_argument("--username", required=True)
    p.add_argument("--mtproto", action="store_true", help="read through the candidate's account")

    p = sub.add_parser("enable")
    p.add_argument("source_id")
    p = sub.add_parser("disable")
    p.add_argument("source_id")

    p = sub.add_parser("sources")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("probe")
    p.add_argument("source_id")

    p = sub.add_parser("run", help="collect now")
    p.add_argument("--source", action="append", dest="sources")
    p.add_argument("--platform", action="append", dest="platforms", choices=["hh", "telegram", "linkedin"])
    p.add_argument("--lookback", type=int, default=7)
    p.add_argument("--overlap", type=int, default=72)
    p.add_argument("--budget", type=int, default=10)
    p.add_argument("--fetch-full", action="store_true")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("vacancies")
    p.add_argument("-q")
    p.add_argument("--platform")
    p.add_argument("--source")
    p.add_argument("--all", action="store_true", help="include unlikely and duplicates")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("show")
    p.add_argument("vacancy_id")

    p = sub.add_parser("fetch-full")
    p.add_argument("vacancy_id")

    p = sub.add_parser("serve", help="run the HTTP API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)

    sub.add_parser("mcp", help="run the MCP server on stdio")
    sub.add_parser("telegram-login", help="authorize the candidate's Telegram account (interactive)")

    args = ap.parse_args(argv)
    core = Collector(args.data)
    cid = args.candidate

    if args.cmd == "import-telegram":
        path = args.csv or find_registry()
        if path is None:
            print("registry CSV not found; pass --csv", file=sys.stderr)
            return 2
        _print(core.import_telegram_registry(cid, path) | {"csv": str(path)})
    elif args.cmd == "add-hh":
        spec = SourceSpec(platform="hh", transport="hh_api" if args.api else "hh_web", kind="search",
                          config={"text": args.text, **({"area": args.area} if args.area else {})})
        _print(core.add_source(cid, SourceIn(source_id=args.source_id, spec=spec, title=args.text)))
    elif args.cmd == "add-linkedin":
        spec = SourceSpec(platform="linkedin", transport="linkedin_guest", kind="search",
                          config={"keywords": args.keywords, **({"location": args.location} if args.location else {})})
        _print(core.add_source(cid, SourceIn(source_id=args.source_id, spec=spec, title=args.keywords)))
    elif args.cmd == "add-telegram":
        spec = SourceSpec(platform="telegram", transport="telegram_mtproto" if args.mtproto else "telegram_web",
                          kind="channel", config={"username": args.username.lstrip("@")})
        _print(core.add_source(cid, SourceIn(source_id=args.source_id, spec=spec, title=args.username)))
    elif args.cmd in ("enable", "disable"):
        _print(core.set_enabled(cid, args.source_id, args.cmd == "enable"))
    elif args.cmd == "sources":
        sources = core.list_sources(cid)
        if args.json:
            _print(sources)
        else:
            for s in sources:
                print(f"{s.source_id:12} {s.spec.transport:16} {'on ' if s.enabled else 'off'} {s.access_status:24} "
                      f"{(s.current_title or s.title or '')[:50]}")
    elif args.cmd == "probe":
        _print(core.probe_source(cid, args.source_id))
    elif args.cmd == "run":
        req = RunRequest(candidate_id=cid, source_ids=args.sources, platforms=args.platforms, force=args.force,
                         params=CollectParams(lookback_days=args.lookback, overlap_hours=args.overlap,
                                              page_budget=args.budget, fetch_full=args.fetch_full))
        run = core.run(req)
        for r in run.sources:
            err = f" {r.error.code}: {r.error.message}" if r.error else ""
            cov = ""
            if r.coverage:
                cov = f" cov={r.coverage.from_at:%Y-%m-%d %H:%M}..{r.coverage.to_at:%Y-%m-%d %H:%M}" if r.coverage.from_at and r.coverage.to_at else ""
            print(f"{r.source_id:12} {r.status:9} pages={r.pages:<3} seen={r.items_seen:<4} new={r.items_new:<4} upd={r.items_updated:<3}{cov}{err}")
        print(json.dumps({"run_id": run.run_id, "status": run.status, **run.totals}, ensure_ascii=False))
    elif args.cmd == "vacancies":
        items, _ = core.store.search_vacancies(cid, q=args.q, platform=args.platform, source_id=args.source,
                                               is_vacancy=None if args.all else "likely",
                                               include_duplicates=args.all, limit=args.limit)
        if args.json:
            _print(items)
        else:
            for v in items:
                when = v.published_at.strftime("%Y-%m-%d") if v.published_at else "----------"
                print(f"{when} {v.vacancy_id} {v.platform:8} {(v.title or '')[:60]:60} | {(v.company or '')[:30]}")
    elif args.cmd == "show":
        details = core.store.vacancy_details(cid, args.vacancy_id)
        if details is None:
            print("not found", file=sys.stderr)
            return 1
        _print(details)
    elif args.cmd == "fetch-full":
        _print(core.fetch_full_vacancy(cid, args.vacancy_id))
    elif args.cmd == "serve":
        if not ipaddress.ip_address(args.host if args.host != "localhost" else "127.0.0.1").is_loopback \
                and not os.environ.get("HRS_COLLECTOR_TOKEN"):
            print("refusing to listen on a non-loopback address without HRS_COLLECTOR_TOKEN", file=sys.stderr)
            return 2
        import uvicorn

        from .api import create_app
        uvicorn.run(create_app(core), host=args.host, port=args.port)
    elif args.cmd == "mcp":
        from .mcp_server import build_server
        build_server(core).run()
    elif args.cmd == "telegram-login":
        return telegram_login(args.data or default_data_dir())
    return 0


def telegram_login(data_dir: Path) -> int:
    try:
        from telethon.sessions import StringSession
        from telethon.sync import TelegramClient
    except ImportError:
        print("telethon is not installed: uv sync --extra telegram", file=sys.stderr)
        return 2
    print("api_id and api_hash come from https://my.telegram.org -> API development tools.")
    api_id = int(input("api_id: ").strip())
    api_hash = getpass.getpass("api_hash: ").strip()
    with TelegramClient(StringSession(), api_id, api_hash) as client:  # prompts for phone, code, 2FA
        session = client.session.save()
        me = client.get_me()
    existing = creds_mod.load_local(data_dir)
    creds = Credentials(hh=existing.hh, telegram=TelegramCredentials(api_id=api_id, api_hash=api_hash, session_string=session))
    path = creds_mod.save_local(data_dir, creds)
    print(f"authorized as id={me.id}; session saved to {path} (mode 0600)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
