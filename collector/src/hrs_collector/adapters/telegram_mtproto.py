"""Telegram through the candidate's own user account (MTProto via Telethon).

Needs api_id/api_hash from https://my.telegram.org and a session string created by
`hrs-collector telegram-login` (the owner types the code and 2FA password).
Reads channels and groups the account can see, including private ones it already
joined. Never joins, never writes.

Credentials come per operation (Credentials.telegram) or from the local
credentials file (see credentials.py). The session string is never logged.
"""

from __future__ import annotations

import asyncio

from ..errors import SourceError
from ..models import SourceSpec, VacancyData
from .base import Page, SourceAdapter, make_vacancy
from .telegram_web import parse_post_text

PAGE_SIZE = 100


class TelegramMTProtoAdapter(SourceAdapter):
    platform = "telegram"
    transport = "telegram_mtproto"
    version = "telegram_mtproto/0.1.0"
    limits = [
        "reads only what the candidate's own account can see",
        "does not join channels or accept invites",
        "Telegram FLOOD_WAIT pauses the source with rate_limited",
    ]

    def __init__(self, http, credentials=None) -> None:
        super().__init__(http, credentials)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client = None

    # ------------------------------------------------------------ client

    def _creds(self):
        creds = self.credentials.telegram if self.credentials else None
        if creds is None:
            raise SourceError("not_configured", "telegram_mtproto needs credentials.telegram (api_id, api_hash, session_string); run `hrs-collector telegram-login`")
        return creds

    def _run(self, coro):
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
        return self._loop.run_until_complete(coro)

    def _connect(self):
        if self._client is not None:
            return self._client
        creds = self._creds()
        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession
        except ImportError as exc:
            raise SourceError("unsupported_environment", "telethon is not installed: uv sync --extra telegram", can_retry=False) from exc
        client = TelegramClient(StringSession(creds.session_string.get_secret_value()), creds.api_id, creds.api_hash.get_secret_value())
        try:
            self._run(client.connect())
        except OSError as exc:
            raise SourceError("service_unreachable", f"telegram connect failed: {exc}") from exc
        if not self._run(client.is_user_authorized()):
            self._run(client.disconnect())
            raise SourceError("not_authenticated", "telegram session is not authorized; run `hrs-collector telegram-login`", can_retry=False)
        self._client = client
        return client

    def account_authorized(self) -> bool | None:
        return True

    def close(self) -> None:
        if self._client is not None:
            self._run(self._client.disconnect())
            self._client = None
        if self._loop is not None:
            self._loop.close()
            self._loop = None

    def check_config(self, spec: SourceSpec) -> None:
        super().check_config(spec)
        if not any(spec.config.get(k) for k in ("username", "chat_id", "title")):
            raise SourceError("not_configured", "telegram_mtproto needs config.username, config.chat_id or config.title")
        self._creds()

    # ------------------------------------------------------------ entity

    def _entity(self, spec: SourceSpec):
        client = self._connect()
        from telethon import errors, utils
        from telethon.tl.types import PeerChannel

        try:
            if spec.config.get("chat_id"):
                entity = self._run(client.get_entity(PeerChannel(int(spec.config["chat_id"]))))
            elif spec.config.get("username"):
                entity = self._run(client.get_entity(str(spec.config["username"]).lstrip("@")))
            else:
                entity = self._run(self._by_title(client, str(spec.config["title"])))
        except errors.FloodWaitError as exc:
            raise SourceError("rate_limited", f"telegram FLOOD_WAIT {exc.seconds}s", retry_after=exc.seconds) from exc
        except (ValueError, errors.UsernameNotOccupiedError, errors.UsernameInvalidError) as exc:
            raise SourceError("unavailable", f"telegram entity not found: {exc}") from exc
        except errors.ChannelPrivateError as exc:
            raise SourceError("private_invite_required", "channel is private and the account is not a member") from exc
        # A public channel is readable without membership; a private one is not.
        if getattr(entity, "left", False) and not getattr(entity, "username", None):
            raise SourceError("private_invite_required",
                              "account is not a member of this private channel; joining is a separate owner-approved action", can_retry=False)
        return entity, str(utils.get_peer_id(entity, add_mark=False))

    @staticmethod
    async def _by_title(client, title: str):
        async for dialog in client.iter_dialogs():
            if dialog.name == title:
                return dialog.entity
        raise ValueError(f"no dialog titled {title!r} in the account")

    # ------------------------------------------------------------ operations

    def read_page(self, spec: SourceSpec, token: str | None, since=None) -> Page:
        self.check_config(spec)
        entity, chat_id = self._entity(spec)
        from telethon import errors

        try:
            messages = self._run(self._connect().get_messages(entity, limit=PAGE_SIZE, offset_id=int(token or 0)))
        except errors.FloodWaitError as exc:
            raise SourceError("rate_limited", f"telegram FLOOD_WAIT {exc.seconds}s", retry_after=exc.seconds) from exc
        username = getattr(entity, "username", None)
        items = [self._item(m, chat_id, username) for m in messages if getattr(m, "message", None) is not None]
        ids = [m.id for m in messages]
        next_token = str(min(ids)) if len(messages) == PAGE_SIZE and min(ids) > 1 else None
        meta = {"stable_chat_id": chat_id, "current_title": getattr(entity, "title", None), "current_username": username}
        return Page(items=items, next_token=next_token, source_meta=meta)

    def _item(self, m, chat_id: str, username: str | None) -> VacancyData:
        text = m.message or ""
        fields = parse_post_text(text)
        url = f"https://t.me/{username}/{m.id}" if username else f"https://t.me/c/{chat_id}/{m.id}"
        return make_vacancy(
            platform="telegram", item_key=f"telegram:{chat_id}:{m.id}", external_id=str(m.id), url=url,
            title=fields.get("title"), company=fields.get("company"), location=fields.get("location"),
            employment=fields.get("employment"), published_at=m.date, edited_at=m.edit_date,
            text=text, completeness="full",
            raw={"chat_id": chat_id, "username": username, "message_id": m.id, "edited": m.edit_date is not None,
                 "has_media": m.media is not None},
        )

    def fetch_full(self, spec: SourceSpec, external_id: str) -> VacancyData:
        entity, chat_id = self._entity(spec)
        msg = self._run(self._connect().get_messages(entity, ids=int(external_id)))
        if msg is None:
            raise SourceError("unavailable", f"message {external_id} not found", stage="fetch_full")
        return self._item(msg, chat_id, getattr(entity, "username", None))
