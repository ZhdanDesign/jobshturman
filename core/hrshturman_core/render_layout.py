"""Renderer-neutral CV layout: ordered lines and the checks run on the rendered files.

Each branch draws DOCX/PDF with its own libraries and fonts; both use these
lines, so the same check proves that every line survived rendering.

Line kinds, in reading order:
  name, title, contacts                  the header: name, headline, one contact line
  section                                a section heading
  role, dates                            an experience entry: «Компания — Должность» and its period
  project                                a project sub-heading inside a grouped entry (from scope)
  scope                                  the company or project description of a single entry
  bullet                                 a list item
  line                                   a plain line (education, languages)
"""
import re

NBSP = " "
DASH = f"{NBSP}— "
PRESENT = {"ru": "н.в."}
# Contact parts in print order; anything else in the contacts dict (fact_ids, ids, notes) is not printed.
CONTACT_KEYS = ("phone", "email", "city", "location", "relocation", "telegram", "linkedin", "github",
                "website", "site", "url")
NAME_KEYS = ("name", "full_name")
# Store ids are «<prefix>_<16 hex>» (common.new_id): fact_…, cv_…, cov_…, evt_…
INTERNAL_ID = re.compile(r"\b[a-z]{2,12}_[0-9a-f]{16}\b")


def _is_service_key(key):
    key = str(key)
    return key in ("fact_ids", "fact_id", "id", "ids") or key.endswith(("_id", "_ids"))


def _text(item):
    """Plain text of a field: a string, a claim {text, fact_ids}, a dict of parts or a list of them.

    Service keys (fact_ids, *_id) never become text.
    """
    if item is None:
        return ""
    if isinstance(item, dict):
        if "text" in item:
            return _text(item["text"])
        return " · ".join(t for t in (_text(v) for k, v in item.items() if not _is_service_key(k)) if t)
    if isinstance(item, (list, tuple)):
        return " · ".join(t for t in (_text(v) for v in item) if t)
    return str(item)


def _first(item, keys):
    for key in keys:
        if isinstance(item, dict) and _text(item.get(key)).strip():
            return _text(item[key]).strip()
    return ""


def _contacts(contacts):
    """One contact line from a string, a list or a dict; a dict prints only the known contact fields."""
    if isinstance(contacts, dict) and "text" not in contacts:
        return " · ".join(t for t in (_text(contacts.get(k)).strip() for k in CONTACT_KEYS) if t)
    return _text(contacts)


def _dashes(text):
    """Year ranges get one dash: «1991–1997», «2022 - н.в.» → «1991 — 1997», «2022 — н.в.»."""
    return re.sub(r"(\d{4})\s*[-–—]\s*(\d{4}|н\.\s?в\.|present|now)", lambda m: f"{m[1]}{DASH}{m[2]}", text,
                  flags=re.IGNORECASE)


def period(start, end, language="ru"):
    """«2019 — 2020»; an entry with a start and no end is current: «2019 — н.в.» (en: «Present»)."""
    start, end = _text(start).strip(), _text(end).strip()
    if start and not end:
        end = PRESENT.get(language, "Present")
    return DASH.join(p for p in (start, end) if p)


def _entry(item):
    """Education or language: «Заведение — специальность · годы», or the text as is."""
    if not isinstance(item, dict) or "text" in item:
        return _dashes(_text(item))
    head = _first(item, ("institution", "university", "school", "organization", "name", "language"))
    detail = _first(item, ("specialty", "speciality", "degree", "faculty", "program", "field", "level"))
    years = _first(item, ("years", "period", "year")) or period(item.get("start"), item.get("end"))
    if not (head or detail or years):
        return _dashes(_text(item))
    text = DASH.join(p for p in (head, detail) if p)
    return _dashes(" · ".join(p for p in (text, years) if p))


def _role_key(role):
    return tuple(_text(role.get(k)).strip() for k in ("employer", "title", "start", "end"))


def _grouped(experience):
    """Neighbouring entries with the same employer, title and dates form one entry with several projects."""
    groups = []
    for role in experience:
        if groups and _role_key(groups[-1][0]) == _role_key(role):
            groups[-1].append(role)
        else:
            groups.append([role])
    return groups


def blocks(content):
    """Ordered (kind, text) lines shared by both renderers and by the extraction check."""
    language = _text(content.get("language")).strip() or "ru"
    contacts = content.get("contacts")
    out = [("name", _text(content.get("full_name")) or _first(contacts, NAME_KEYS)),
           ("title", _text(content.get("headline"))),
           ("contacts", _contacts(contacts))]
    if content.get("summary"):
        out.append(("section", "О себе"))
        out += [("bullet", _text(c)) for c in content["summary"]]
    if content.get("experience"):
        out.append(("section", "Опыт работы"))
        for group in _grouped(content["experience"]):
            head = group[0]
            out.append(("role", DASH.join(p for p in (_text(head.get("employer")), _text(head.get("title"))) if p)))
            out.append(("dates", period(head.get("start"), head.get("end"), language)))
            for role in group:
                out.append(("project" if len(group) > 1 else "scope", _text(role.get("scope"))))
                out += [("bullet", _text(b)) for b in role.get("bullets", [])]
    if content.get("competencies"):
        out.append(("section", "Компетенции"))
        out += [("bullet", _text(c)) for c in content["competencies"]]
    for key, title in (("education", "Образование"), ("languages", "Языки")):
        if content.get(key):
            out.append(("section", title))
            out += [("line", _entry(c)) for c in content[key]]
    return [(kind, text.strip()) for kind, text in out if text and text.strip()]


def _squash(text):
    return re.sub(r"\s+", "", text).replace("•", "").casefold()


def extraction_check(lines, extracted):
    """Every line is in the extracted text, and no internal id (fact_…, cv_…) leaked into it."""
    haystack = _squash(extracted)
    missing = [text for _, text in lines if _squash(text) not in haystack]
    leaked = sorted(set(INTERNAL_ID.findall(extracted)))
    first = next((s.strip() for s in extracted.splitlines() if s.strip()), "")
    name = next((text for kind, text in lines if kind == "name"), None)
    result = {"ok": not missing and not leaked, "missing": missing, "checked_lines": len(lines)}
    if leaked:
        result["leaked_ids"] = leaked
    if name is not None:
        result["name_first"] = _squash(first) == _squash(name)
        result["ok"] = result["ok"] and result["name_first"]
    return result
