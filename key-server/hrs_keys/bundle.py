"""Build the methodology bundle from the skills library.

The bundle is a zip with the library files and bundle.json (version and file hashes). The server
signs it when it is published. Files meant for us, not for the candidate's agent, stay out:
per-stage acceptance scenarios, the library's own tests and their fixtures.
"""
import fnmatch
import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

EXCLUDE = ("*/references/acceptance.md", "references/acceptance.md", "scripts/test_*", "scripts/fixtures/*",
           "*__pycache__*", "*.pyc", "*.DS_Store")
FIXED_TIME = (2026, 1, 1, 0, 0, 0)


def excluded(rel: str) -> bool:
    return any(fnmatch.fnmatch(rel, pattern) for pattern in EXCLUDE)


def build(library: Path, extra: dict[str, Path] | None = None, version: str | None = None) -> tuple[str, bytes]:
    """extra: {path inside the bundle: file outside the library}, e.g. the Telegram channel list."""
    library = Path(library)
    manifest = json.loads((library / "library_manifest.json").read_text(encoding="utf-8"))
    files = {p.relative_to(library).as_posix(): p for p in sorted(library.rglob("*")) if p.is_file()}
    files = {rel: p for rel, p in files.items() if not excluded(rel)}
    for rel, src in (extra or {}).items():
        files[rel] = Path(src)
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M")
    version = version or f"{manifest.get('version', '0')}.{stamp}"
    meta = {"version": version, "library_version": manifest.get("version"), "built_at": stamp,
            "files": {rel: hashlib.sha256(p.read_bytes()).hexdigest() for rel, p in files.items()}}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, p in [("bundle.json", None), *files.items()]:
            info = zipfile.ZipInfo(rel, FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            data = json.dumps(meta, ensure_ascii=False, indent=1).encode() if p is None else p.read_bytes()
            z.writestr(info, data)
    return version, out.getvalue()
