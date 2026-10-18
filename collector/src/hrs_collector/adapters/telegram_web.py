"""Telegram public web preview (https://t.me/s/<username>).

No account, no keys. Works only for public channels with web preview enabled.
Pagination walks the full history with ?before=<message_id>, so coverage is real,
but private channels, groups without preview and personal chats are out of reach
(those need telegram_mtproto).
"""

from __future__ import annotations

import base64
import json
import re

from bs4 import BeautifulSoup, Tag

from ..errors import SourceError
from ..models import SourceSpec, VacancyData
from ..util import html_to_text, parse_dt
from .base import Page, SourceAdapter, make_vacancy

_LABELS = {
    "location": re.compile(r"^(локация|location|город|место работы|география)\s*[:：-]\s*(.+)$", re.I),
    "company": re.compile(r"^(компания|company|работодатель|employer)\s*[:：-]\s*(.+)$", re.I),
    "employment": re.compile(r"^(занятость|employment|формат работы|формат)\s*[:：-]\s*(.+)$", re.I),
}
_TITLE_PATTERNS = [
    re.compile(r"^(?:#?вакансия|vacancy|позиция|position|должность)\s*[:：-]?\s*(.+)$", re.I),
]


def username_of(spec: SourceSpec) -> str:
    username = str(spec.config.get("username") or "").strip().lstrip("@")
    if not username:
        url = str(spec.config.get("url") or "")
        m = re.match(r"https?://t\.me/(?:s/)?([A-Za-z0-9_]{4,32})/?$", url)
        username = m.group(1) if m else ""
    return username


def parse_post_text(text: str) -> dict:
    """Best-effort labeled fields from a free-form channel post."""
    out: dict = {}
    lines = [ln.strip(" \t*•—-") for ln in text.splitlines()]
    lines = [ln for ln in lines if re.search(r"\w", ln)]
    for ln in lines[:40]:
        for field_name, rx in _LABELS.items():
            m = rx.match(ln)
            if m and field_name not in out:
                out[field_name] = m.group(2).strip()[:200]
    for ln in lines[:6]:
        for rx in _TITLE_PATTERNS:
            m = rx.match(ln)
            if m:
                out["title"] = m.group(1).strip()[:200]
                break
        if "title" in out:
            break
    if "title" not in out and lines:
        out["title"] = lines[0][:200]
    return out


class TelegramWebAdapter(SourceAdapter):
    platform = "telegram"
    transport = "telegram_web"
    version = "telegram_web/0.1.0"
    limits = [
        "public channels with web preview only",
        "no private channels, groups without preview or personal chats",
        "edits are detected only while the post is inside the re-check window",
    ]

    def check_config(self, spec: SourceSpec) -> None:
        super().check_config(spec)
        if not username_of(spec):
            raise SourceError("not_configured", "telegram_web needs config.username (public @username)")

    def read_page(self, spec: SourceSpec, token: str | None, since=None) -> Page:
        self.check_config(spec)
        username = username_of(spec)
        params = {"before": token} if token else None
        resp = self.http.get(f"https://t.me/s/{username}", params=params, ok_statuses=(200, 302, 404))
        if resp.status_code == 404:
            raise SourceError("unavailable", f"@{username} not found")
        return self.parse(resp.text, username, final_path=resp.url.path)

    def parse(self, markup: str, username: str, final_path: str = "") -> Page:
        soup = BeautifulSoup(markup, "html.parser")
        messages = soup.select("div.tgme_widget_message[data-post]")
        channel_title_el = soup.select_one(".tgme_channel_info_header_title")
        if not messages and channel_title_el is None:
            if final_path and not final_path.startswith("/s/"):
                if soup.select_one(".tgme_page_title"):
                    raise SourceError(
                        "web_preview_unavailable",
                        f"@{username} exists but has no public web preview (private, group or preview disabled); needs telegram_mtproto",
                    )
                raise SourceError("unavailable", f"@{username} not found or not a channel")
            raise SourceError("changed_page", f"t.me/s/{username}: no channel markup found")

        meta: dict = {"current_username": username}
        if channel_title_el is not None:
            meta["current_title"] = channel_title_el.get_text(" ", strip=True)

        items: list[VacancyData] = []
        ids: list[int] = []
        for msg in messages:
            item = self._parse_message(msg, username, meta)
            if item is not None:
                items.append(item)
                ids.append(int(item.external_id))
        has_older = soup.select_one('link[rel="prev"]') is not None
        next_token = str(min(ids)) if ids and has_older and min(ids) > 1 else None
        return Page(items=items, next_token=next_token, source_meta=meta)

    def _parse_message(self, msg: Tag, username: str, meta: dict) -> VacancyData | None:
        post = str(msg.get("data-post", ""))
        m = re.match(r"^([^/]+)/(\d+)$", post)
        if not m:
            return None
        msg_id = m.group(2)
        chat_id = _chat_id(str(msg.get("data-view", "")))
        if chat_id:
            meta.setdefault("stable_chat_id", chat_id)
        time_el = msg.select_one(".tgme_widget_message_date time[datetime]")
        if time_el is None:
            return None  # service message
        published = parse_dt(str(time_el["datetime"]))
        meta_el = msg.select_one(".tgme_widget_message_meta")
        edited = meta_el is not None and "edited" in meta_el.get_text(" ", strip=True).lower()

        texts = [t for t in msg.select(".js-message_text") if not t.find_parent(class_="tgme_widget_message_reply")]
        text_el = texts[-1] if texts else None
        text = html_to_text(str(text_el)) if text_el else ""
        links = [str(a["href"]) for a in text_el.select("a[href]")] if text_el else []
        fields = parse_post_text(text)
        key_chat = chat_id or f"@{username.lower()}"
        return make_vacancy(
            platform="telegram",
            item_key=f"telegram:{key_chat}:{msg_id}",
            external_id=msg_id,
            url=f"https://t.me/{username}/{msg_id}",
            title=fields.get("title"),
            company=fields.get("company"),
            location=fields.get("location"),
            employment=fields.get("employment"),
            published_at=published,
            edited_at=None,
            text=text,
            links=links,
            completeness="full",
            raw={"chat_id": chat_id, "username": username, "message_id": int(msg_id), "edited": edited,
                 "has_media": msg.select_one(".tgme_widget_message_photo_wrap, .tgme_widget_message_document") is not None},
        )

    def fetch_full(self, spec: SourceSpec, external_id: str) -> VacancyData:
        """A channel post page already holds the full text; re-read the single post."""
        page = self.read_page(spec, str(int(external_id) + 1))
        for item in page.items:
            if item.external_id == str(external_id):
                return item
        raise SourceError("unavailable", f"post {external_id} not found (deleted or hidden)", stage="fetch_full")


def _chat_id(data_view: str) -> str | None:
    if not data_view:
        return None
    try:
        padded = data_view + "=" * (-len(data_view) % 4)
        value = json.loads(base64.b64decode(padded))["c"]
        return str(abs(int(value)))
    except Exception:
        return None
