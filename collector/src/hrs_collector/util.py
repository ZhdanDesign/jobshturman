from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from .models import Contacts


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        value += "T00:00:00+00:00"
    value = value.replace("Z", "+00:00")
    # Python accepts at most 6 fractional digits and needs them padded consistently.
    value = re.sub(r"\.(\d+)", lambda m: "." + m.group(1)[:6].ljust(6, "0"), value)
    return as_utc(datetime.fromisoformat(value))


def iso(value: datetime | None) -> str | None:
    value = as_utc(value)
    return value.isoformat().replace("+00:00", "Z") if value else None


def stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:20]
    return f"{prefix}_{digest}"


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def html_to_text(markup: str | None) -> str:
    if not markup:
        return ""
    soup = BeautifulSoup(markup, "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(["p", "li", "div", "h1", "h2", "h3", "h4", "tr"]):
        block.insert_after("\n")
    text = soup.get_text()
    text = html.unescape(text).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


_URL = re.compile(r"https?://\S+|t\.me/\S+")
_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)


def fingerprint(text: str) -> str:
    """Duplicate-candidate key over the whole text, not the first N chars.

    Lowercased, links/emoji/punctuation removed. Equal fingerprints mean the same
    wording; it never deletes anything, it only links records (see store.link_duplicates).
    """
    norm = _URL.sub(" ", text.lower())
    norm = _NON_WORD.sub(" ", norm)
    norm = " ".join(norm.split())
    return sha256(norm)[:32]


_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_TG = re.compile(r"(?<![\w.])@([A-Za-z][A-Za-z0-9_]{4,31})\b")
_TG_LINK = re.compile(r"(?:https?://)?t\.me/([A-Za-z][A-Za-z0-9_]{4,31})(?![\w/])")
_PHONE = re.compile(r"(?<!\d)(?:\+7|8|\+\d{1,3})[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)")
_LINK = re.compile(r"https?://[^\s<>\"')]+")


def extract_contacts(text: str, links: list[str] | None = None) -> Contacts:
    emails = sorted(set(_EMAIL.findall(text)))
    tg = sorted({m.lower() for m in _TG.findall(text)} | {m.lower() for m in _TG_LINK.findall(text)})
    phones = sorted({re.sub(r"[^\d+]", "", p) for p in _PHONE.findall(text)})
    all_links = sorted(set(_LINK.findall(text)) | set(links or []))
    return Contacts(emails=emails, telegram=tg, phones=phones, links=all_links[:50])


_VACANCY_HINTS = re.compile(
    r"ваканси|ищем|требуется|требования|обязанност|зарплат|з/п|оклад|условия работы|"
    r"резюме|откликнуться|hiring|vacancy|we are looking|requirements|responsibilit|salary|apply",
    re.IGNORECASE,
)


def guess_is_vacancy(text: str) -> str:
    hits = len(set(m.group(0).lower()[:6] for m in _VACANCY_HINTS.finditer(text)))
    if hits >= 2:
        return "likely"
    if hits == 0 and len(text) > 0:
        return "unlikely"
    return "unknown"
