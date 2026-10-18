"""Deterministic checks that the methodology used to describe in prose (stages 06, 09-10, 15, 17, 19).

They run in code so the agent gets the same answer every time: the hard limits of a vacancy against the
candidate's policy, a candidate-facing text against the fact ledger, and an action plan for dates,
dependencies and cycles. Judgement about meaning stays with the agent and the candidate.
"""
from datetime import date

from .common import DomainError
from .resume import PLACEHOLDER, _numbers, validate_resume

FORMATS = {"REMOTE", "HYBRID", "ON_SITE"}
MONTH = {None, "month", "monthly", "MONTH", "месяц"}


def _status(ok: bool | None) -> str:
    return "unknown" if ok is None else ("ok" if ok else "conflict")


def _currency(code):
    code = (code or "RUB").upper()
    return "RUB" if code == "RUR" else code  # hh.ru still says RUR


def vacancy_constraints(vacancy: dict, policy: dict) -> dict:
    """Stage 15, step 4. policy: salary_min, currency (default RUB), gross (true/false/null),
    formats (REMOTE/HYBRID/ON_SITE), locations (cities or countries), relocation (bool).
    Unknown stays unknown: a missing salary is never read as zero and never as a match."""
    allowed = {"salary_min", "currency", "gross", "formats", "locations", "relocation"}
    if not isinstance(policy, dict) or set(policy) - allowed:
        raise DomainError("invalid_schema", f"policy fields: {sorted(allowed)}")
    formats = {f.upper() for f in policy.get("formats") or []}
    if formats - FORMATS:
        raise DomainError("invalid_schema", f"formats: {sorted(FORMATS)}")
    notes = []

    salary = vacancy.get("salary") or {}
    top = salary.get("max") if salary.get("max") is not None else salary.get("min")
    salary_ok = None
    if policy.get("salary_min") is None:
        salary_ok = True
    elif top is None:
        notes.append("зарплата не указана")
    elif _currency(salary.get("currency")) != _currency(policy.get("currency")):
        notes.append(f"валюта {salary.get('currency')} не совпадает с политикой, пересчёт не делается")
    elif salary.get("period") not in MONTH:
        notes.append(f"зарплата указана за {salary['period']}, не за месяц")
    else:
        salary_ok = top >= policy["salary_min"]
        if policy.get("gross") is not None and salary.get("gross") is not None and salary["gross"] != policy["gross"]:
            notes.append("в вакансии и в политике разные gross/net: сравнение приблизительное")

    offered = {f.upper() for f in vacancy.get("work_format") or []} & FORMATS
    format_ok = True if not formats else (bool(offered & formats) if offered else None)
    if format_ok is None:
        notes.append("формат работы не указан")

    places = [p.casefold() for p in policy.get("locations") or []]
    where = (vacancy.get("location") or "").casefold()
    if not places or ("REMOTE" in offered and (not formats or "REMOTE" in formats)):
        location_ok = True
    elif not where:
        location_ok = None
        notes.append("место работы не указано")
    else:
        location_ok = any(p in where for p in places) or bool(policy.get("relocation"))
        if location_ok and not any(p in where for p in places):
            notes.append("нужен переезд")

    result = {"salary": "below" if salary_ok is False else _status(salary_ok),
              "format": _status(format_ok), "location": _status(location_ok)}
    return {**result, "hard_conflicts": [k for k, v in result.items() if v in ("below", "conflict")],
            "unknown": [k for k, v in result.items() if v == "unknown"], "notes": notes}


def check_text(text: str, fact_ids: list[str], facts_by_id: dict) -> dict:
    """Stages 09, 10, 17, 19: a letter, portfolio card or answer to an employer before it goes out.
    Same rules as a CV claim: no placeholders, no unknown or rejected facts, no NDA facts, every number
    comes from a linked fact. Without fact_ids any number in the text is an error."""
    if not isinstance(text, str) or not text.strip():
        raise DomainError("invalid_schema", "text is required")
    if fact_ids:
        report = validate_resume({"summary": [{"text": text, "fact_ids": fact_ids}]}, facts_by_id)
        for item in report["errors"] + report["warnings"]:
            item["path"] = "text"
        return {"ok": report["ok"], "errors": report["errors"], "warnings": report["warnings"]}
    errors = [{"code": "invalid_material", "path": "text", "message": f"placeholder left in text: {m}"}
              for m in PLACEHOLDER.findall(text)]
    errors += [{"code": "missing_fact", "path": "text", "message": f"number {n} has no linked fact"}
               for n in sorted(_numbers(text))]
    return {"ok": not errors, "errors": errors, "warnings": []}


def _day(value, name, task_id):
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise DomainError("invalid_schema", f"task {task_id}: {name} must be YYYY-MM-DD") from exc


def check_plan(tasks: list[dict]) -> dict:
    """Stage 06, step 6. tasks: [{id, title, owner, criterion, start, due, depends_on[]}].
    A task without owner or done-criterion is not part of a ready plan; dates and dependencies must agree."""
    if not isinstance(tasks, list) or not all(isinstance(t, dict) and t.get("id") for t in tasks):
        raise DomainError("invalid_schema", "tasks: list of {id, title, owner, criterion, start, due, depends_on}")
    errors = []

    def err(code, task_id, message):
        errors.append({"code": code, "task_id": task_id, "message": message})

    ids = [t["id"] for t in tasks]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        err("duplicate_id", dup, "task id repeats")
    by_id = {t["id"]: t for t in tasks}
    for t in tasks:
        for field in ("owner", "criterion"):
            if not str(t.get(field) or "").strip():
                err(f"missing_{field}", t["id"], f"task has no {field}")
        start = _day(t["start"], "start", t["id"]) if t.get("start") else None
        due = _day(t["due"], "due", t["id"]) if t.get("due") else None
        if due is None:
            err("missing_due", t["id"], "task has no due date")
        if start and due and due < start:
            err("due_before_start", t["id"], "due date is before start")
        for dep in t.get("depends_on") or []:
            if dep not in by_id:
                err("unknown_dependency", t["id"], f"depends on unknown task {dep}")
                continue
            dep_due = by_id[dep].get("due")
            if start and dep_due and _day(dep_due, "due", dep) > start:
                err("starts_before_dependency", t["id"], f"starts before {dep} is due")

    state = {}

    def visit(task_id, path):
        state[task_id] = "open"
        for dep in by_id[task_id].get("depends_on") or []:
            if dep not in by_id:
                continue
            if state.get(dep) == "open":
                cycle = path[path.index(dep):] + [dep]
                err("cycle", task_id, "dependency cycle: " + " -> ".join(cycle))
            elif dep not in state:
                visit(dep, path + [dep])
        state[task_id] = "done"

    for task_id in by_id:
        if task_id not in state:
            visit(task_id, [task_id])
    return {"ok": not errors, "errors": errors, "tasks": len(tasks)}
