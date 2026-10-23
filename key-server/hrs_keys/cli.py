"""hrs-keys: run the key server and manage it from the server's shell.

  hrs-keys serve [--host 127.0.0.1 --port 8098]
  hrs-keys signing-key                      create the bundle signing key, print the public key for hrs
  hrs-keys sign-release FILE                sign a release archive (jobshturman-wheels.zip): writes FILE.sig
  hrs-keys tariffs
  hrs-keys issue <tariff> [--months N | --until DATE] [--order REF] [--devices N]
  hrs-keys list [--state active|expiring|expired|revoked]
  hrs-keys extend <key_id> (--months N | --until DATE)
  hrs-keys revoke <key_id> --reason refund|violation|reissue|other
  hrs-keys restore <key_id> | reset-devices <key_id>
  hrs-keys bundle build <library_dir> <out.zip> [--include DEST=SRC ...]
  hrs-keys bundle publish <bundle.zip> --note «что изменилось» [--important]
  hrs-keys watermark decode <file>           whose key a copied methodology text came from

Settings come from the environment: HRS_KEYS_DATA, HRS_KEYS_SECRET, HRS_KEYS_ADMIN_TOKEN, HRS_KEYS_METHODOLOGIST_TOKEN,
HRS_KEYS_SIGNING_KEY, HRS_KEYS_RENEW_URL, HRS_KEYS_ADMIN_ORIGINS (comma separated, for the admin page).
"""
import argparse
import json
import os
import sys
from pathlib import Path

from . import bundle
from .service import KeyServer, KeyServerError, Settings, generate_signing_key


def _print(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="hrs-keys", description="Сервер ключей JobShturman")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("serve")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8098)
    sub.add_parser("signing-key")
    sub.add_parser("sign-release").add_argument("file")
    sub.add_parser("tariffs")
    p = sub.add_parser("issue")
    p.add_argument("tariff")
    p.add_argument("--months", type=int)
    p.add_argument("--until")
    p.add_argument("--order")
    p.add_argument("--devices", type=int)
    p = sub.add_parser("list")
    p.add_argument("--state")
    p = sub.add_parser("extend")
    p.add_argument("key_id")
    p.add_argument("--months", type=int)
    p.add_argument("--until")
    p = sub.add_parser("revoke")
    p.add_argument("key_id")
    p.add_argument("--reason", required=True, choices=["refund", "violation", "reissue", "other"])
    for name in ("restore", "reset-devices"):
        sub.add_parser(name).add_argument("key_id")
    p = sub.add_parser("bundle")
    bsub = p.add_subparsers(dest="bundle_cmd", required=True)
    b = bsub.add_parser("build")
    b.add_argument("library")
    b.add_argument("out")
    b.add_argument("--include", action="append", default=[], metavar="DEST=SRC")
    b.add_argument("--version")
    b = bsub.add_parser("publish")
    b.add_argument("file")
    b.add_argument("--note", required=True, help="что изменилось: это увидят люди, когда им предложат обновиться")
    b.add_argument("--important", action="store_true", help="поставить всем без вопроса, с этим объяснением")
    p = sub.add_parser("watermark")
    wsub = p.add_subparsers(dest="watermark_cmd", required=True)
    wsub.add_parser("decode").add_argument("file")
    args = ap.parse_args(argv)

    if args.cmd == "bundle" and args.bundle_cmd == "build":
        extra = dict(item.split("=", 1) for item in args.include)
        version, data = bundle.build(Path(args.library), {k: Path(v) for k, v in extra.items()}, args.version)
        Path(args.out).write_bytes(data)
        _print({"version": version, "file": args.out, "size": len(data)})
        return 0

    settings = Settings.from_env()
    if args.cmd == "serve":
        import uvicorn

        from .app import create_app
        origins = [o for o in os.environ.get("HRS_KEYS_ADMIN_ORIGINS", "").split(",") if o]
        uvicorn.run(create_app(settings, origins), host=args.host, port=args.port, access_log=False)
        return 0
    if args.cmd == "signing-key":
        if settings.signing_key.exists():
            print(f"Ключ подписи уже есть: {settings.signing_key}. Публичный ключ:", file=sys.stderr)
            _print({"public_key": KeyServer(settings).public_key()})
            return 1
        _print({"public_key": generate_signing_key(settings.signing_key), "file": str(settings.signing_key)})
        return 0

    ks = KeyServer(settings)
    try:
        if args.cmd == "tariffs":
            _print(ks.tariffs())
        elif args.cmd == "issue":
            _print(ks.issue(args.tariff, args.months, args.until, args.order, args.devices))
        elif args.cmd == "list":
            _print(ks.keys(args.state))
        elif args.cmd == "extend":
            _print(ks.extend(args.key_id, args.months, args.until))
        elif args.cmd == "revoke":
            _print(ks.revoke(args.key_id, args.reason))
        elif args.cmd == "restore":
            _print(ks.restore(args.key_id))
        elif args.cmd == "reset-devices":
            _print(ks.reset_devices(args.key_id))
        elif args.cmd == "sign-release":
            signature = ks.sign_release(Path(args.file).read_bytes())
            Path(args.file + ".sig").write_text(signature + "\n", encoding="utf-8")
            _print({"file": args.file, "signature": args.file + ".sig"})
        elif args.cmd == "bundle":
            _print(ks.publish(Path(args.file).read_bytes(), note=args.note, important=args.important))
        elif args.cmd == "watermark":
            found = ks.find_watermark(Path(args.file).read_text(encoding="utf-8"))
            _print(found)
            return 0 if found["keys"] else 1
    except KeyServerError as exc:
        _print({"error": exc.code, "message": str(exc)})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
