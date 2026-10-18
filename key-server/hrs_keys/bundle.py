"""Build the methodology bundle from the skills library.

The bundle is a zip with the library files and bundle.json (version, format, the oldest hrs that can
read it, file hashes). The server signs it when it is published. Files meant for us, not for the
candidate's agent, stay out: per-stage acceptance scenarios, the library's own tests and their fixtures.
"""
import fnmatch
import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from hrshturman_core import DomainError, methodology

EXCLUDE = ("*/references/acceptance.md", "references/acceptance.md", "scripts/test_*", "scripts/fixtures/*",
           "*__pycache__*", "*.pyc", "*.DS_Store", "._*", "*/._*")
FIXED_TIME = (2026, 1, 1, 0, 0, 0)
# The first hrs that reads the route from the manifest. A bundle whose stages still sit in the old
# folders stage-00…25 in that order is readable by older hrs too, so it carries no minimum.
ROUTE_HRS = "0.3.0"


def excluded(rel: str) -> bool:
    return any(fnmatch.fnmatch(rel, pattern) for pattern in EXCLUDE)


class _Files:
    def __init__(self, files: dict[str, bytes]):
        self.files = files

    def names(self):
        return list(self.files)

    def read(self, name):
        return self.files[name].decode("utf-8")


def min_hrs(files: dict[str, bytes]) -> str | None:
    """The oldest hrs that hands out this bundle right: None while the old folder layout still holds."""
    try:
        route = methodology.route(_Files(files))
    except (DomainError, KeyError, ValueError):  # a broken manifest is caught by the checks before publishing
        return ROUTE_HRS
    old = [s for s in route.stages if s.dir == f"skills/stage-{s.number:02d}-{s.id}"]
    return None if len(old) == len(route.stages) else ROUTE_HRS


def build_files(files: dict[str, bytes], version: str | None = None) -> tuple[str, bytes]:
    """files: {path inside the bundle: bytes}, already without the excluded ones or not."""
    files = {rel: data for rel, data in sorted(files.items()) if not excluded(rel) and rel != "bundle.json"}
    manifest = json.loads(files.get(methodology.MANIFEST, b"{}") or b"{}")
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M")
    version = version or f"{manifest.get('version', '0')}.{stamp}"
    meta = {"version": version, "library_version": manifest.get("version"), "built_at": stamp,
            "format": manifest.get("format") or 1, "min_hrs": min_hrs(files),
            "files": {rel: hashlib.sha256(data).hexdigest() for rel, data in files.items()}}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, data in [("bundle.json", json.dumps(meta, ensure_ascii=False, indent=1).encode()), *files.items()]:
            info = zipfile.ZipInfo(rel, FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data)
    return version, out.getvalue()


def build(library: Path, extra: dict[str, Path] | None = None, version: str | None = None) -> tuple[str, bytes]:
    """extra: {path inside the bundle: file outside the library}, e.g. the Telegram channel list."""
    library = Path(library)
    files = {p.relative_to(library).as_posix(): p.read_bytes() for p in sorted(library.rglob("*")) if p.is_file()}
    for rel, src in (extra or {}).items():
        files[rel] = Path(src).read_bytes()
    return build_files(files, version)
