"""The methodology route: which stages there are, in what order, in which big step.

The library's own manifest decides it (library_manifest.json, "format": 2), so the author of the
methodology can add, remove and reorder stages without a code change. A stage has a stable id; its
number is only its place in the list, counted from 0. Older libraries (format 1, folders
skills/stage-NN-slug) are read through LEGACY: the 26 stages as they were before the manifest.

Code knows a fixed set of progress signals (what in the candidate's data lights a stage up) and the
tools of the MCP server; a stage only points at them. A stage that needs a new tool needs a developer.

Everything here takes a library: any object with names() and read(name).
"""
import json
import posixpath
import re
from dataclasses import dataclass, field
from urllib.parse import unquote

from .common import DomainError

MANIFEST = "library_manifest.json"
GENESIS = "agent/genesis.md"
FORMAT = 2

# What in the candidate's data marks a stage as started or done (hrs web.progress computes them).
PROGRESS_SIGNALS = ("intake", "facts", "facts_confirmed", "resume", "vacancies", "scored", "decided", "sent",
                    "funnel")

LEGACY_STEPS = [{"id": "preparation", "title": "Подготовка"}, {"id": "vacancies", "title": "Вакансии"},
                {"id": "applications", "title": "Отклики"}, {"id": "replies", "title": "Ответы и встречи"}]
# folder → (title, step, progress signal, tools): the route of format-1 libraries.
LEGACY = {
    "stage-00-intake-diagnostic": ("Диагностика", "preparation", "intake",
                                   ["intake_get", "intake_ask", "intake_answer", "intake_confirm"]),
    "stage-01-candidate-intake": ("Материалы об опыте", "preparation", None, ["document_import"]),
    "stage-02-target-market": ("Желаемые должности и компании", "preparation", None, []),
    "stage-03-experience-interview": ("Сильные стороны", "preparation", "facts", ["fact_add"]),
    "stage-04-market-swot": ("Сравнение с требованиями", "preparation", None, []),
    "stage-05-competency-matrix": ("Навыки", "preparation", None, []),
    "stage-06-gap-action-plan": ("Пробелы и план", "preparation", None, ["plan_check"]),
    "stage-07-master-cv": ("Основное резюме", "preparation", "resume", ["resume_get"]),
    "stage-08-platform-profiles": ("Профили на сайтах", "preparation", None, []),
    "stage-09-cover-letter-base": ("Основа письма", "preparation", None, ["text_check"]),
    "stage-10-case-portfolio": ("Примеры проектов", "preparation", None, ["text_check"]),
    "stage-11-candidate-knowledge-base": ("Проверенные факты", "preparation", "facts_confirmed", []),
    "stage-12-positioning-validation": ("Проверка на реальных вакансиях", "preparation", None, []),
    "stage-13-source-connections": ("Источники вакансий", "vacancies", None, ["list_sources"]),
    "stage-14-vacancy-collection": ("Регулярный сбор", "vacancies", "vacancies", ["vacancies_sync"]),
    "stage-15-vacancy-scoring": ("Отбор", "vacancies", "scored", ["vacancy_constraints", "application_score"]),
    "stage-16-candidate-authorization": ("Ваше решение, куда откликаться", "vacancies", "decided", []),
    "stage-17-tailored-application": ("Резюме и письмо под вакансию", "applications", None, ["text_check"]),
    "stage-18-application-delivery": ("Отправка по вашим правилам", "applications", "sent", []),
    "stage-19-employer-questions": ("Анкеты работодателя", "applications", None, ["text_check"]),
    "stage-20-assessment-task": ("Решение о тестовом задании", "applications", None, []),
    "stage-21-channel-duty": ("Ответы работодателей", "replies", None, []),
    "stage-22-funnel-event-register": ("Статусы откликов", "replies", "funnel", []),
    "stage-23-candidate-alert": ("Когда нужен ваш ответ", "replies", None, []),
    "stage-24-meeting-preparation": ("Подготовка к встрече", "replies", None, []),
    "stage-25-meeting-debrief": ("Разбор встречи", "replies", None, []),
}
LEGACY_DIR = re.compile(r"skills/(stage-(\d{2})-[a-z0-9-]+)/")
ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


@dataclass
class Stage:
    id: str
    number: int
    title: str
    step: str
    dir: str
    progress: str | None = None
    tools: list[str] = field(default_factory=list)

    def files(self, names) -> list[str]:
        return sorted(n for n in names if n.startswith(self.dir + "/"))

    def public(self) -> dict:
        return {"id": self.id, "number": self.number, "title": self.title, "step": self.step,
                "progress": self.progress, "tools": self.tools}


@dataclass
class Route:
    format: int
    version: str | None
    steps: list[dict]
    stages: list[Stage]

    def stage(self, ref) -> Stage:
        """A stage by its number (5, "05") or its id ("master-cv")."""
        text = str(ref).strip()
        if text.isdigit():
            for s in self.stages:
                if s.number == int(text):
                    return s
        else:
            for s in self.stages:
                if s.id == text:
                    return s
        numbers = ", ".join(str(s.number) for s in self.stages)
        raise DomainError("not_found", f"stage {text}: в методике нет такого этапа; номера: {numbers}")

    def step_title(self, step_id: str) -> str:
        return next((s["title"] for s in self.steps if s["id"] == step_id), step_id)

    def public(self) -> dict:
        return {"format": self.format, "version": self.version, "steps": self.steps,
                "stages": [s.public() for s in self.stages]}


def _manifest(library) -> dict:
    names = set(library.names())
    if MANIFEST not in names:
        return {}
    try:
        return json.loads(library.read(MANIFEST))
    except ValueError as exc:
        raise DomainError("bad_manifest", f"{MANIFEST}: {exc}") from exc


def legacy_stages(names, manifest: dict | None = None) -> list[dict]:
    """Format-1 library: the stages its manifest lists (intake_diagnostic and stages[].entrypoint),
    else one stage per skills/stage-NN-slug folder; in the order of NN."""
    manifest = manifest or {}
    declared = [manifest.get("intake_diagnostic") or {}, *(manifest.get("stages") or [])]
    found = {}
    for name in [d.get("entrypoint") or "" for d in declared] or names:
        m = LEGACY_DIR.match(name)
        if m:
            found[m[1]] = int(m[2])
    if not found:
        for name in names:
            m = LEGACY_DIR.match(name)
            if m:
                found[m[1]] = int(m[2])
    out = []
    for folder in sorted(found, key=found.get):
        title, step, progress, tools = LEGACY.get(folder, (folder, LEGACY_STEPS[-1]["id"], None, []))
        out.append({"id": folder.split("-", 2)[2], "title": title, "step": step,
                    "dir": f"skills/{folder}", "progress": progress, "tools": list(tools)})
    return out


def upgrade(manifest: dict, names) -> dict:
    """The format-2 manifest of a format-1 library: the old fields stay, steps and stages are added."""
    if manifest.get("format") == FORMAT:
        return manifest
    return manifest | {"format": FORMAT, "steps": LEGACY_STEPS, "stages": legacy_stages(names, manifest),
                       "legacy_stages": manifest.get("stages")}


def route(library) -> Route:
    names = library.names()
    raw = _manifest(library)
    data = upgrade(raw, names)
    # Format 1 keeps the numbers of its folders (stage-07 is stage 7 even if 01–06 are missing);
    # in format 2 the number is the place in the list.
    legacy = raw.get("format") != FORMAT
    stages = [Stage(id=s["id"], number=int(s["dir"].split("/")[1][6:8]) if legacy else i, title=s.get("title") or s["id"], step=s.get("step") or "",
                    dir=s["dir"].rstrip("/"), progress=s.get("progress"), tools=list(s.get("tools") or []))
              for i, s in enumerate(x for x in data["stages"] if x.get("status", "active") == "active")]
    return Route(format=FORMAT, version=data.get("version"), steps=list(data.get("steps") or []), stages=stages)


def legacy_route() -> Route:
    """The route before the methodology is downloaded: the 26 stages of format 1."""
    return route(_Names([f"skills/{folder}/SKILL.md" for folder in LEGACY]))


def route_or_legacy(library) -> Route:
    """The route of the library, or the format-1 route while the library cannot be read (no key yet)."""
    try:
        return route(library)
    except DomainError:
        return legacy_route()


class _Names:
    def __init__(self, names):
        self._names = names

    def names(self):
        return self._names

    def read(self, name):
        raise DomainError("not_found", name)


def stage_text(library, ref) -> str:
    """The procedure of one stage: its references/procedure.md, else its SKILL.md. Nothing of other stages."""
    stage = route(library).stage(ref)
    names = set(library.names())
    procedure = f"{stage.dir}/references/procedure.md"
    return library.read(procedure if procedure in names else f"{stage.dir}/SKILL.md")


def genesis_text(library) -> str:
    if GENESIS not in set(library.names()):
        raise DomainError("not_found", "В методике нет главного промпта agent/genesis.md")
    return library.read(GENESIS)


# --- checks before publishing (no watermark, no model: hrs selftest checks the delivery) ---

LINK = re.compile(r"\]\(([^)\s]+)\)")
FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def validate(files: dict[str, str], tools: set[str] | None = None) -> dict:
    """files: {path in the library: text} of the bundle to be published (binary files may be omitted
    from the dict but must be listed with ""). Errors block publishing; findings are shown."""
    errors, findings = [], []

    def error(code, detail, stage=None):
        errors.append({"code": code, "detail": detail, "stage": stage})

    def finding(code, detail, stage=None):
        findings.append({"code": code, "detail": detail, "stage": stage})

    names = set(files)
    raw = {}
    if MANIFEST in files:
        try:
            raw = json.loads(files[MANIFEST])
        except ValueError as exc:
            error("manifest_broken", str(exc))
            return {"ok": False, "errors": errors, "findings": findings, "stages": 0}
    if raw.get("format") not in (None, 1, FORMAT):
        error("manifest_format", f"format {raw.get('format')}: этот код понимает 1 и {FORMAT}")
    data = upgrade(raw, names)
    steps = {s.get("id") for s in data.get("steps") or []}
    if not steps:
        error("no_steps", "в манифесте нет шагов (steps)")
    seen = set()
    active = [s for s in data.get("stages") or [] if s.get("status", "active") == "active"]
    if not active:
        error("no_stages", "в манифесте нет ни одного этапа")
    for s in data.get("stages") or []:
        sid = s.get("id", "")
        if not ID.match(sid or ""):
            error("bad_id", f"id «{sid}»: латиница, цифры и дефис", sid)
        if sid in seen:
            error("duplicate_id", f"id «{sid}» повторяется", sid)
        seen.add(sid)
        if s.get("status", "active") != "active":
            continue
        d = (s.get("dir") or "").rstrip("/")
        if not d or f"{d}/SKILL.md" not in names:
            error("stage_missing", f"нет файла {d}/SKILL.md", sid)
        if s.get("step") not in steps:
            error("unknown_step", f"шаг «{s.get('step')}» не описан в steps", sid)
        if s.get("progress") and s["progress"] not in PROGRESS_SIGNALS:
            error("unknown_progress", f"сигнал «{s['progress']}»: можно {', '.join(PROGRESS_SIGNALS)}", sid)
        for t in s.get("tools") or []:
            if tools is not None and t not in tools:
                finding("unknown_tool", f"инструмента {t} нет в hrs", sid)
        if not (s.get("title") or "").strip():
            finding("no_title", "у этапа нет названия", sid)
    if GENESIS not in names:
        finding("no_genesis", "нет главного промпта agent/genesis.md")
    for name, text in files.items():
        if not name.endswith(".md") or not text:
            continue
        if name.endswith("SKILL.md"):
            m = FRONT.match(text)
            if not m:
                finding("front_matter", f"{name}: нет шапки --- name/description ---")
            elif not {"name:", "description:"} <= {line.split(" ")[0] for line in m[1].splitlines()}:
                finding("front_matter", f"{name}: в шапке нужны name и description")
        for target in LINK.findall(text):
            if re.match(r"[a-z]+:", target) or target.startswith("#"):
                continue
            path = posixpath.normpath(posixpath.join(posixpath.dirname(name), unquote(target.split("#")[0])))
            if path not in names:
                finding("broken_link", f"{name} → {target}")
    return {"ok": not errors, "errors": errors, "findings": findings, "stages": len(active)}
