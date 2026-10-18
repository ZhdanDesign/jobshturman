"""Access key and the methodology bundle: hrs login, hrs sync, key status for the agent.

The key server sees only the key and a random installation id. The key itself is kept in the macOS
Keychain (keystore.py), not in license.json; an old license.json with the key is moved there on read. The bundle is checked against
the signing key built into hrs. On disk it is kept only encrypted (AES-256-GCM, the key derived
with HKDF from the access key and the installation id); it is decrypted in memory, one stage at a
time is handed out, and every text carries the invisible watermark of the key.
The server is asked at most once a day; without network the last answer holds for 7 days,
but never past the key's end date.
"""
import base64
import contextlib
import hashlib
import importlib.metadata
import io
import json
import os
import secrets
import uuid
import zipfile
from datetime import UTC, datetime, timedelta

import httpx
from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from hrshturman_core import DomainError
from hrshturman_core.brand import PRODUCT_NAME

from . import keystore, paths, watermark

DEFAULT_SERVER = "https://tools.owai.ru/jobshturman/keys"
# Addresses older hrs wrote into license.json; read them as the current one.
OLD_SERVERS = ("https://tools.owai.ru/hrshturman/keys",)
# Public key of the production bundle signing key (hrs-keys signing-key on the server).
PUBLIC_KEYS: tuple[str, ...] = ("g0+l6EZh3voBDLJApGDmDOIPlaE7gXG0leEo/HzJsZ4=",)
CHECK_EVERY = timedelta(hours=24)
OFFLINE_GRACE = timedelta(days=7)
WARN_BEFORE = timedelta(days=1)
TIMEOUT = 15.0


def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def state_file():
    return paths.data_dir() / "license.json"


def bundle_file():
    return paths.data_dir() / "methodology" / "bundle.enc"


def legacy_bundle_file():
    return paths.data_dir() / "methodology" / "bundle.zip"


def _cipher(state: dict) -> AESGCM:
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=state["install_id"].encode(), info=b"hrshturman-bundle-v1")
    return AESGCM(hkdf.derive(state["key"].encode()))


def encrypt(data: bytes, state: dict) -> bytes:
    nonce = secrets.token_bytes(12)
    return nonce + _cipher(state).encrypt(nonce, data, None)


def decrypt(blob: bytes, state: dict) -> bytes:
    try:
        return _cipher(state).decrypt(blob[:12], blob[12:], None)
    except (InvalidTag, ValueError) as exc:
        raise DomainError("bad_bundle", "Методика на диске не расшифровывается этим ключом: выполните hrs sync") from exc


def public_keys() -> list[str]:
    env = os.environ.get("HRS_BUNDLE_PUBKEYS")
    return [k.strip() for k in env.split(",") if k.strip()] if env else list(PUBLIC_KEYS)


def load() -> dict | None:
    """License state with the key taken from the key store. A key still written in the file is moved out."""
    try:
        state = json.loads(state_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if state.get("server") in OLD_SERVERS:
        state["server"] = DEFAULT_SERVER
        save(state)
    if state.get("key"):
        save(state)
    else:
        state["key"] = keystore.store().load(state["install_id"])
    return state


def save(state: dict) -> None:
    state = dict(state)
    key = state.pop("key", None)
    if key:
        keystore.store().save(state["install_id"], key)
    path = state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    if os.name != "nt":
        tmp.chmod(0o600)
    tmp.replace(path)


def client_version() -> str:
    """This hrs's version: the server gives it the newest methodology it can read."""
    try:
        return importlib.metadata.version("hrshturman")
    except importlib.metadata.PackageNotFoundError:
        return "0"


def _headers(token: str | None = None) -> dict:
    return {"X-Hrs-Version": client_version(), **({"Authorization": f"Bearer {token}"} if token else {})}


def _client(server: str, http: httpx.Client | None):
    if http is not None:
        return contextlib.nullcontext(http)
    return httpx.Client(base_url=server.rstrip("/") + "/", timeout=TIMEOUT)


def _error(resp: httpx.Response) -> DomainError:
    try:
        body = resp.json()
        return DomainError(body.get("error", "server_error"), body.get("message", resp.text))
    except ValueError:
        return DomainError("server_error", f"Сервер ключей ответил {resp.status_code}")


def _remember_refusal(state: dict, exc: DomainError) -> None:
    if exc.code in ("key_expired", "key_revoked", "invalid_key"):
        state["refused"] = {"code": exc.code, "message": str(exc), "at": iso(now())}
        state.pop("token", None)
        save(state)


def _activate(state: dict, http) -> dict:
    resp = http.post("v1/activate", json={"key": state["key"], "install_id": state["install_id"], "client": "hrs"},
                     headers=_headers())
    if resp.status_code != 200:
        exc = _error(resp)
        _remember_refusal(state, exc)
        raise exc
    data = resp.json()
    state.update(token=data.pop("token"), token_expires_at=data.pop("token_expires_at"), status=data,
                 checked_at=iso(now()))
    state.pop("refused", None)
    save(state)
    return state


@contextlib.contextmanager
def _network(server: str):
    try:
        yield
    except httpx.HTTPError as exc:
        raise DomainError("connection_error", f"Нет связи с сервером ключей {server}: {exc}") from exc


def login(key: str, server: str | None = None, http: httpx.Client | None = None) -> dict:
    """Activate the key on this computer and download the methodology."""
    old = load() or {}
    server = server or os.environ.get("HRS_KEY_SERVER") or old.get("server") or DEFAULT_SERVER
    state = {"server": server, "key": key.strip(), "install_id": old.get("install_id") or uuid.uuid4().hex,
             "bundle": old.get("bundle")}
    with _network(server), _client(server, http) as client:
        _activate(state, client)
        sync(state, client)
    return view()


def refresh(force: bool = False, http: httpx.Client | None = None) -> dict:
    """Ask the server about the key if the last answer is older than a day. Network errors are not fatal."""
    state = load()
    if not state:
        return view()
    if not force and state.get("checked_at") and now() - parse(state["checked_at"]) < CHECK_EVERY \
            and bundle_file().is_file():
        return view()
    try:
        with _client(state["server"], http) as client:
            token_ok = state.get("token") and parse(state["token_expires_at"]) > now() + timedelta(minutes=5)
            resp = client.get("v1/status", headers=_headers(state["token"])) if token_ok else None
            if resp is not None and resp.status_code == 403:
                _remember_refusal(state, _error(resp))
                return view()
            if resp is not None and resp.status_code == 200:
                state.update(status=resp.json(), checked_at=iso(now()))
                state.pop("refused", None)
                save(state)
            else:
                _activate(state, client)
            if (state["status"].get("bundle") or {}).get("sha256") != (state.get("bundle") or {}).get("sha256") \
                    or not bundle_file().is_file():
                sync(state, client)
    except (httpx.HTTPError, DomainError):
        pass
    return view()


def sync(state: dict | None = None, http: httpx.Client | None = None) -> dict:
    """Download the latest bundle if it changed and check its signature before keeping it."""
    state = state or load()
    if not state:
        raise DomainError("no_license", "Ключ не введён: hrs login <ключ>")
    with _network(state["server"]), _client(state["server"], http) as client:
        if not state.get("token") or parse(state["token_expires_at"]) <= now() + timedelta(minutes=5):
            _activate(state, client)
        resp = client.get("v1/bundle/latest", headers=_headers(state["token"]))
        if resp.status_code != 200:
            exc = _error(resp)
            _remember_refusal(state, exc)
            raise exc
    data = resp.content
    signature = resp.headers.get("X-Bundle-Signature", "")
    verify(data, signature)
    target = bundle_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_bytes(encrypt(data, state))
    tmp.replace(target)
    legacy_bundle_file().unlink(missing_ok=True)
    state["bundle"] = {"version": resp.headers.get("X-Bundle-Version"), "sha256": hashlib.sha256(data).hexdigest(),
                       "signature": signature, "synced_at": iso(now())}
    save(state)
    return view()


def update(accept: bool = False, http: httpx.Client | None = None) -> dict:
    """A newer methodology is installed only after the person agrees: without accept, show what changed
    (and mark an automatic update as told); with accept, move the key to the offered version and download it."""
    state = load()
    if not state:
        raise DomainError("no_license", "Ключ не введён: hrs login <ключ>")
    refresh(force=True, http=http)
    state = load()
    offer = (state.get("status") or {}).get("update_available")
    if not accept:
        news = (state.get("status") or {}).get("auto_updated")
        if news and state.get("news_seen") != news["to"]:
            state["news_seen"] = news["to"]
            save(state)
        return view() | ({} if offer or news else {"message": "Новых версий методики нет."})
    if not offer:
        raise DomainError("no_update", "Новых версий методики нет: обновлять нечего")
    with _network(state["server"]), _client(state["server"], http) as client:
        resp = client.post("v1/bundle/accept", json={"version": offer["version"]}, headers=_headers(state["token"]))
        if resp.status_code != 200:
            raise _error(resp)
        state.update(status=resp.json(), checked_at=iso(now()))
        save(state)
        sync(state, client)
    return view() | {"message": f"Методика обновлена до версии {offer['version']}."}


def _changes(items: list[dict]) -> str:
    return "\n".join(f"- {c['version']}: {c.get('note') or 'без описания'}" for c in items)


def verify(data: bytes, signature: str) -> None:
    keys = public_keys()
    if not keys:
        raise DomainError("no_public_key", "В hrs не задан ключ проверки методики (HRS_BUNDLE_PUBKEYS)")
    try:
        sig = base64.b64decode(signature)
    except ValueError as exc:
        raise DomainError("bad_signature", "Подпись методики повреждена") from exc
    for key in keys:
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(key)).verify(sig, data)
            return
        except (InvalidSignature, ValueError):
            continue
    raise DomainError("bad_signature", f"Подпись методики не сходится: файл не от сервера {PRODUCT_NAME}")


def view() -> dict:
    """Key state for people and agents. methodology=True means the stages can be read now."""
    state = load()
    if not state:
        return {"state": "none", "methodology": False,
                "message": "Ключ не введён. Попросите у человека ключ из письма и выполните hrs login <ключ>."}
    status = state.get("status") or {}
    renew = status.get("renew_url")
    store = keystore.store()
    out = {"key_hint": status.get("key_hint"), "tariff": (status.get("tariff") or {}).get("title"),
           "expires_at": status.get("expires_at"), "renew_url": renew, "checked_at": state.get("checked_at"),
           "bundle_version": (state.get("bundle") or {}).get("version"), "server": state.get("server"),
           "key_storage": store.name, **({"storage_warning": store.warning} if store.warning else {})}
    if not state.get("key"):
        return out | {"state": "none", "methodology": False,
                      "message": "Ключ не найден в хранилище ключей. Попросите у человека ключ из письма и "
                                 "выполните hrs login <ключ>."}
    refused = state.get("refused")
    expires = parse(status["expires_at"]) if status.get("expires_at") else None
    if refused and refused["code"] == "key_revoked":
        return out | {"state": "revoked", "methodology": False, "message": refused["message"]}
    if (refused and refused["code"] == "key_expired") or (expires and expires <= now()):
        return out | {"state": "expired", "methodology": False,
                      "message": f"Срок ключа закончился, методика отключена. Наработки кандидата на месте и "
                                 f"выгружаются. Продлить: {renew}"}
    if refused:
        return out | {"state": "invalid", "methodology": False, "message": refused["message"]}
    checked = parse(state["checked_at"]) if state.get("checked_at") else None
    if checked is None or now() - checked > OFFLINE_GRACE:
        return out | {"state": "offline", "methodology": False,
                      "message": "Ключ давно не проверялся. Подключите интернет и выполните hrs sync."}
    has_bundle = bundle_file().is_file()
    out |= {"days_left": max((expires - now()).days, 0) if expires else None, "methodology": has_bundle}
    if status.get("hrs_update"):
        out["hrs_update"] = status["hrs_update"]
    offer, news = status.get("update_available"), status.get("auto_updated")
    if offer:
        out["methodology_update"] = offer | {
            "message": f"Вышла новая версия методики {offer['version']}. Что изменилось:\n{_changes(offer['changes'])}\n"
                       "Обновить? После «да»: hrs update --yes (агенту: methodology_update с accept=true). "
                       "До согласия работа идёт по текущей версии."}
    if news and state.get("news_seen") != news["to"]:
        out["methodology_updated"] = news | {
            "message": f"Методика обновлена до версии {news['to']} без вопроса: в ней важное исправление. "
                       f"Что изменилось:\n{_changes(news['changes'])}"}
    if not has_bundle:
        out["message"] = "Методика не скачана: выполните hrs sync."
    if expires and expires - now() <= WARN_BEFORE:
        return out | {"state": "expiring", "warning": f"Ключ действует до {status['expires_at'][:10]}. "
                                                      f"Продлите его, чтобы методика не отключилась: {renew}"}
    return out | {"state": "active"}


class BundleLibrary:
    """The methodology as the MCP server reads it: decrypted in memory, only while the key is valid.
    Every Markdown text it gives out carries the watermark of the key."""

    def _zip(self) -> zipfile.ZipFile:
        refresh()
        state = view()
        if not state["methodology"]:
            raise DomainError(f"license_{state['state']}", state.get("message") or "Методика недоступна")
        self.state = load()
        data = decrypt(bundle_file().read_bytes(), self.state)
        verify(data, self.state["bundle"]["signature"])
        return zipfile.ZipFile(io.BytesIO(data))

    def names(self) -> list[str]:
        with self._zip() as z:
            return z.namelist()

    def read(self, name: str) -> str:
        with self._zip() as z:
            try:
                text = z.read(name).decode("utf-8")
            except KeyError as exc:
                raise DomainError("not_found", f"В методике нет {name}") from exc
        wm = (self.state.get("status") or {}).get("watermark")
        return watermark.mark(text, wm) if wm and name.endswith(".md") else text  # data files stay parseable
