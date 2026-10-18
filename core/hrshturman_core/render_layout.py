"""Renderer-neutral CV layout: ordered lines and the text extraction check.

Each branch draws DOCX/PDF with its own libraries and fonts; both use these
lines, so the same check proves that every line survived rendering.
"""
import re


def _text(item):
    return item.get("text", "") if isinstance(item, dict) else str(item)


def blocks(content):
    """Ordered (kind, text) lines shared by both renderers and by the extraction check."""
    out = []
    if content.get("full_name"):
        out.append(("name", content["full_name"]))
    if content.get("contacts"):
        out.append(("meta", content["contacts"]))
    if content.get("headline"):
        out.append(("title", content["headline"]))
    if content.get("summary"):
        out.append(("section", "О себе"))
        out += [("para", _text(c)) for c in content["summary"]]
    if content.get("experience"):
        out.append(("section", "Опыт работы"))
        for role in content["experience"]:
            period = " – ".join(p for p in (role.get("start"), role.get("end")) if p)
            out.append(("role", ", ".join(p for p in (role.get("title"), role.get("employer")) if p)))
            if period:
                out.append(("meta", period))
            if role.get("scope"):
                out.append(("para", role["scope"]))
            out += [("bullet", _text(b)) for b in role.get("bullets", [])]
    for key, title in (("competencies", "Компетенции"), ("education", "Образование"), ("languages", "Языки")):
        if content.get(key):
            out.append(("section", title))
            out += [("bullet", _text(c)) for c in content[key]]
    return [(kind, text.strip()) for kind, text in out if text and text.strip()]


def _squash(text):
    return re.sub(r"\s+", "", text).replace("•", "").casefold()


def extraction_check(lines, extracted):
    haystack = _squash(extracted)
    missing = [text for _, text in lines if _squash(text) not in haystack]
    return {"ok": not missing, "missing": missing, "checked_lines": len(lines)}
