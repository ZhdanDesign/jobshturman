"""Contract check of the methodology bundle as hrs delivers it. No model, no network.

For each stage of the manifest (any number of them): the stage is found and handed out through the same path as the
stage_procedure tool; with the watermark stripped the text equals the source byte for byte; the
YAML front matter, code blocks and table rows are untouched; every file a stage links to is in the
bundle (links in the shared files are checked too); no stage carries another stage's own text. For the bundle as a whole: nothing that must
stay with us (acceptance scenarios, library tests) got in. Tool names the texts mention are matched
against the tools of `hrs serve`.

Errors are faults of the delivery (ours). Findings are about the library texts: hrs reports them and
never edits the methodology.
"""
import fnmatch
import io
import posixpath
import re
import zipfile
from urllib.parse import unquote

from hrshturman_core import DomainError, methodology
from hrshturman_local.mcp_server import stage_text

from . import watermark

FORBIDDEN = ("*/references/acceptance.md", "references/acceptance.md", "scripts/test_*", "scripts/fixtures/*",
             "*__pycache__*", "*.pyc", "*.DS_Store", "._*", "*/._*")
LINK = re.compile(r"\]\(([^)\s]+)\)")
ZERO_WIDTH = re.compile("[" + watermark.START + watermark.END + watermark.DIGITS + "]")
FRAME = re.compile(watermark.START + "[" + watermark.DIGITS + "]{16}" + watermark.END)
TOOL_LIKE = re.compile(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\(")
TEST_MARK = "5e1f7e57"


class ZipLibrary:
    def __init__(self, data: bytes):
        self.zip = zipfile.ZipFile(io.BytesIO(data))

    def names(self):
        return self.zip.namelist()

    def read(self, name):
        try:
            return self.zip.read(name).decode("utf-8")
        except KeyError:
            raise DomainError("not_found", name) from None


class MarkedLibrary:
    """The bundle as BundleLibrary hands it out: every text with the watermark."""

    def __init__(self, raw):
        self.raw = raw

    def names(self):
        return self.raw.names()

    def read(self, name):
        text = self.raw.read(name)
        return watermark.mark(text, TEST_MARK) if name.endswith(".md") else text


def plain(text: str) -> str:
    return ZERO_WIDTH.sub("", text)


def front_matter(text: str) -> dict | None:
    """The `key: value` header of a SKILL.md; None when there is none. Raises ValueError when broken."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 3)
    if end < 0:
        raise ValueError("front matter is not closed")
    fields = {}
    for line in text[4:end].split("\n"):
        key, sep, value = line.partition(":")
        if not sep or not re.fullmatch(r"[a-z_][a-z0-9_-]*", key):
            raise ValueError(f"not a key: value line: {line[:40]}")
        value = value.strip()
        if value.startswith('"') != value.endswith('"') or (value.startswith('"') and len(value) < 2):
            raise ValueError(f"unbalanced quotes in {key}")
        fields[key] = value.strip('"')
    return fields


def verbatim(text: str) -> list[str]:
    """Lines that must not change: fenced code, indented code and table rows."""
    out, fenced = [], False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            out.append(line)
        elif fenced or watermark.VERBATIM.match(line):
            out.append(line)
    return out


def _paragraphs(text: str) -> set[str]:
    return {p.strip() for p in plain(text).split("\n\n") if len(p.strip()) >= 60}


def run(bundle: bytes, tools: set[str]) -> dict:
    raw = ZipLibrary(bundle)
    marked = MarkedLibrary(raw)
    names = set(raw.names())
    errors, findings = [], []

    def error(stage, code, detail):
        errors.append({"stage": stage, "code": code, "detail": detail})

    def finding(stage, code, detail):
        findings.append({"stage": stage, "code": code, "detail": detail})

    for name in sorted(names):
        if any(fnmatch.fnmatch(name, p) for p in FORBIDDEN):
            error(None, "forbidden_file", name)

    try:
        stages = methodology.route(raw).stages
    except DomainError as exc:
        error(None, exc.code, str(exc))
        stages = []
    check = methodology.validate({n: (raw.read(n) if n.endswith((".md", ".json")) else "") for n in names}, tools)
    for e in check["errors"]:
        error(e["stage"], e["code"], e["detail"])
    for f in check["findings"]:
        if f["code"] not in ("broken_link", "front_matter"):  # checked below, as before
            finding(f["stage"], f["code"], f["detail"])
    if methodology.GENESIS in names:
        rules = marked.read(methodology.GENESIS)
        if plain(rules) != raw.read(methodology.GENESIS) or not FRAME.search(rules):
            error(None, "genesis_changed", "главный промпт выдаётся не так, как написан, или без водяного знака")

    texts, mentioned = {}, {}
    for stage in stages:
        n = stage.number
        try:
            source, given = stage_text(raw, n), stage_text(marked, n)
        except DomainError as exc:
            error(n, "stage_missing", str(exc))
            continue
        texts[n] = given
        if plain(given) != source:
            error(n, "text_changed", "the stage differs from the source beyond the watermark")
        if not FRAME.search(given):
            error(n, "no_watermark", "the stage carries no watermark")
        if verbatim(given) != verbatim(source):
            error(n, "verbatim_changed", "code blocks or table rows changed")
        for name in sorted(x for x in stage.files(names) if x.endswith(".md")):
            text, shown = raw.read(name), marked.read(name)
            if name.endswith("SKILL.md"):
                try:
                    head = front_matter(shown)
                    if head is None or not {"name", "description"} <= set(head):
                        finding(n, "front_matter_fields", f"{name}: name and description expected")
                    if head is not None and shown.split("\n---\n", 1)[0] != text.split("\n---\n", 1)[0]:
                        error(n, "front_matter_changed", name)
                except ValueError as exc:
                    error(n, "front_matter_broken", f"{name}: {exc}")
        words = set(re.findall(r"[a-z][a-z0-9_]+", source))
        mentioned[n] = sorted(words & tools)
        for tool in sorted(set(TOOL_LIKE.findall(source)) - tools):
            finding(n, "unknown_tool", tool)

    for name in sorted(x for x in names if x.endswith(".md")):
        owner = next((st.number for st in stages if name.startswith(st.dir + "/")), None)
        for target in LINK.findall(raw.read(name)):
            if re.match(r"[a-z]+:", target) or target.startswith("#"):
                continue
            path = posixpath.normpath(posixpath.join(posixpath.dirname(name), unquote(target.split("#")[0])))
            if path not in names:
                finding(owner, "broken_link", f"{name} → {target}")

    own = {n: _paragraphs(t) for n, t in texts.items()}
    # Template paragraphs repeat in most stages by design; a paragraph in a few stages is a copy.
    shared = {p for ps in own.values() for p in ps if sum(p in other for other in own.values()) > len(own) // 2}
    for n, ps in own.items():
        for m, other in own.items():
            if m != n and (ps - shared) & other:
                error(n, "foreign_text", f"stage {m} contains text of stage {n}")

    return {"stages": len(texts), "stages_total": len(stages), "ok": not errors, "errors": errors, "findings": findings,
            "tools_mentioned": {n: t for n, t in mentioned.items() if t},
            "tools_never_mentioned": sorted(tools - {t for ts in mentioned.values() for t in ts})}


def installed_bundle() -> bytes:
    """The bundle on this computer, decrypted in memory and checked against its signature."""
    from . import license
    state = license.load()
    if not state or not license.bundle_file().is_file():
        raise DomainError("no_bundle", "Методика не скачана: hrs login <ключ> или hrs selftest --library <папка>")
    data = license.decrypt(license.bundle_file().read_bytes(), state)
    license.verify(data, state["bundle"]["signature"])
    return data


def library_bundle(library, extra: dict | None = None) -> bytes:
    """A bundle built from a library folder the way the key server builds it (needs hrs-keys).
    extra: {path in the bundle: file}, as `hrs-keys bundle build --include`."""
    try:
        from hrs_keys import bundle
    except ImportError as exc:
        raise DomainError("no_builder", "Для --library нужен пакет hrs-keys (среда разработки)") from exc
    return bundle.build(library, extra)[1]


def tool_names() -> set[str]:
    """Tools of `hrs serve`, built on a throwaway workspace so the check leaves no data behind."""
    import asyncio
    import dataclasses
    import tempfile
    from pathlib import Path

    from hrs_collector.service import Collector

    from . import paths
    from .server import build

    with tempfile.TemporaryDirectory() as tmp:
        settings = dataclasses.replace(paths.settings(), workspace=Path(tmp) / "workspace")
        server = build(settings, Collector(Path(tmp) / "collector"))
        return {t.name for t in asyncio.run(server.list_tools())}


def summary(report: dict) -> str:
    lines = [(f"Этапов проверено: {report['stages']} из {report['stages_total']}. Ошибок выдачи: {len(report['errors'])}. "
              f"Замечаний к текстам методики: {len(report['findings'])}.")]

    def where(item):
        return "общий файл" if item["stage"] is None else f"этап {item['stage']}"
    lines += [f"  ошибка: {where(e)}: {e['code']}: {e['detail']}" for e in report["errors"]]
    lines += [f"  замечание: {where(f)}: {f['code']}: {f['detail']}" for f in report["findings"]]
    lines.append(f"Инструменты hrs, которые тексты этапов не упоминают: {len(report['tools_never_mentioned'])}.")
    return "\n".join(lines)
