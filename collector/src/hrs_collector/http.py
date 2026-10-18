"""Polite HTTP: one process-wide per-host pacing, bounded retries, error mapping."""

from __future__ import annotations

import os
import threading
import time
from urllib.parse import urlsplit

import httpx

from .errors import SourceError

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

# Minimum seconds between two requests to the same host.
HOST_INTERVAL = {
    "hh.ru": 2.0,
    "api.hh.ru": 0.5,
    "t.me": 1.0,
    "www.linkedin.com": 3.0,
}
DEFAULT_INTERVAL = 1.0

_lock = threading.Lock()
_next_slot: dict[str, float] = {}


def _pace(host: str) -> None:
    interval = HOST_INTERVAL.get(host, DEFAULT_INTERVAL) * float(os.environ.get("HRS_PACE_FACTOR", "1"))
    with _lock:
        now = time.monotonic()
        slot = max(now, _next_slot.get(host, 0.0))
        _next_slot[host] = slot + interval
    delay = slot - now
    if delay > 0:
        time.sleep(delay)


class Http:
    def __init__(self, *, user_agent: str = BROWSER_UA, timeout: float = 30.0, retries: int = 3,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.retries = retries
        self.client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"},
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )

    def close(self) -> None:
        self.client.close()

    def get(self, url: str, *, params: dict | None = None, headers: dict | None = None,
            ok_statuses: tuple[int, ...] = (200,)) -> httpx.Response:
        host = urlsplit(url).hostname or ""
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            _pace(host)
            try:
                resp = self.client.get(url, params=params, headers=headers)
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError) as exc:
                last_exc = exc
                time.sleep(min(2 ** attempt, 20))
                continue
            if resp.status_code in ok_statuses:
                return resp
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = _retry_after(resp)
                if attempt < self.retries:
                    time.sleep(min(retry_after or 2 ** (attempt + 1), 60))
                    continue
                code = "rate_limited" if resp.status_code == 429 else "service_unreachable"
                raise SourceError(code, f"HTTP {resp.status_code} from {host}", retry_after=retry_after)
            return resp  # 3xx/4xx: adapter interprets
        raise SourceError("service_unreachable", f"{host}: {type(last_exc).__name__}: {last_exc}")


def _retry_after(resp: httpx.Response) -> int | None:
    value = resp.headers.get("Retry-After")
    try:
        return int(value) if value else None
    except ValueError:
        return None
