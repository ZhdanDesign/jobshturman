from __future__ import annotations

from ..errors import SourceError
from ..http import Http
from ..models import Credentials
from .base import Page, SourceAdapter
from .hh import HHApiAdapter, HHWebAdapter
from .linkedin import LinkedInGuestAdapter
from .telegram_mtproto import TelegramMTProtoAdapter
from .telegram_web import TelegramWebAdapter

ADAPTERS: dict[str, type[SourceAdapter]] = {
    "hh_web": HHWebAdapter,
    "hh_api": HHApiAdapter,
    "telegram_web": TelegramWebAdapter,
    "telegram_mtproto": TelegramMTProtoAdapter,
    "linkedin_guest": LinkedInGuestAdapter,
}


def make_adapter(transport: str, http: Http, credentials: Credentials | None = None) -> SourceAdapter:
    try:
        cls = ADAPTERS[transport]
    except KeyError:
        raise SourceError("unsupported_environment", f"unknown transport {transport!r}", can_retry=False)
    return cls(http, credentials)


__all__ = ["ADAPTERS", "Page", "SourceAdapter", "make_adapter"]
