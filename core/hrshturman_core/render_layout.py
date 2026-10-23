"""Renderer-neutral CV layout: ordered lines and the text extraction check.

Each branch draws DOCX/PDF with its own libraries and fonts; both use these
lines, so the same check proves that every line survived rendering.
"""
import re


def _text(item):
    """Plain text of a field: a string, a claim {text, fact_ids}, a dict of parts or a list of them."""
    if item is None:
        return ""
    if isinstance(item, dict):
        if "text" in item:
            return _text(item["text"])
        return " · ".join(t for t in (_text(v) for v in item.values()) if t)
    if isinstance(item, (list, tuple)):
        return " · ".join(t for t in (_text(v) for v in item) if t)
    return str(item)


def blocks(content):
    """Ordered (kind, text) lines shared by both renderers and by the extraction check."""
    out = []
    out.append(("name", _text(content.get("full_name"))))
    out.append(("meta", _text(content.get("contacts"))))
    out.append(("title", _text(content.get("headline"))))
    if content.get("summary"):
        out.append(("section", "О себе"))
        out += [("para", _text(c)) for c in content["summary"]]
    if content.get("experience"):
        out.append(("section", "Опыт работы"))
        for role in content["experience"]:
            period = " – ".join(p for p in (_text(role.get("start")), _text(role.get("end"))) if p)
            out.append(("role", ", ".join(p for p in (_text(role.get("title")), _text(role.get("employer"))) if p)))
            out.append(("meta", period))
            out.append(("para", _text(role.get("scope"))))
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
