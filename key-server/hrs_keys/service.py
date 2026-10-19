"""Key server rules: issue, activate, check, extend, revoke, publish and hand out the signed bundle."""
import base64
import contextlib
import hashlib
import hmac
import io
import json
import os
import secrets
import time
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from hrshturman_core import DomainError, methodology

from . import bundle as bundles_mod
from . import library, watermark
from .db import DB

TOKEN_TTL = timedelta(hours=24)
WARN_BEFORE = timedelta(days=1)
KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I
CHANNELS = ("stable", "test")
CONSENT_HRS = "0.4.0"
NOTE_MAX = 1000
HRS_UPDATE = ("Вышла новая версия методики, но её читает только новый JobShturman. Обновите программу: "
              "curl -fsSL https://raw.githubusercontent.com/ZhdanDesign/jobshturman/main/install.sh | sh")


def version_tuple(v: str | None) -> tuple:
    out = []
    for part in (v or "0").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits or 0))
    return tuple(out)


def consents(client: str | None) -> bool:
    """hrs from CONSENT_HRS on keeps the version it has and asks the person before taking a newer one."""
    return client is not None and version_tuple(client) >= version_tuple(CONSENT_HRS)


def readable(min_hrs: str | None, client: str | None) -> bool:
    """Can an hrs of version `client` (None: older than the header) hand out a bundle needing min_hrs."""
    return not min_hrs or (client is not None and version_tuple(client) >= version_tuple(min_hrs))


class KeyServerError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


@dataclass(frozen=True)
class Settings:
    data: Path
    secret: bytes
    admin_token: str | None
    signing_key: Path
    renew_url: str
    methodologist_token: str | None = None  # comma separated; edits and publishes the methodology only
    license_url: str = "https://tools.owai.ru/jobshturman/oferta/"

    @classmethod
    def from_env(cls):
        data = Path(os.environ.get("HRS_KEYS_DATA", "data")).expanduser()
        secret = os.environ.get("HRS_KEYS_SECRET")
        if not secret or len(secret) < 32:
            raise SystemExit("HRS_KEYS_SECRET: нужна случайная строка от 32 символов")
        return cls(data=data, secret=secret.encode(), admin_token=os.environ.get("HRS_KEYS_ADMIN_TOKEN") or None,
                   signing_key=Path(os.environ.get("HRS_KEYS_SIGNING_KEY") or data / "signing_key.pem"),
                   renew_url=os.environ.get("HRS_KEYS_RENEW_URL", "https://tools.owai.ru/jobshturman/"),
                   methodologist_token=os.environ.get("HRS_KEYS_METHODOLOGIST_TOKEN") or None,
                   license_url=os.environ.get("HRS_KEYS_LICENSE_URL", "https://tools.owai.ru/jobshturman/oferta/"))


def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def add_months(dt: datetime, months: int) -> datetime:
    month = dt.month - 1 + months
    year, month = dt.year + month // 12, month % 12 + 1
    for day in (dt.day, 30, 29, 28):
        try:
            return dt.replace(year=year, month=month, day=day)
        except ValueError:
            continue
    raise AssertionError("unreachable")


KEY_PREFIX = "JBS"
OLD_KEY_PREFIXES = ("HRS",)  # keys issued before the rename stay valid


def normalize_key(key: str) -> str:
    """The 20 characters of the key, without the prefix: JBS-… and the old HRS-… hash alike."""
    plain = "".join(ch for ch in key.upper() if ch.isalnum())
    if len(plain) == 23 and plain[:3] in (KEY_PREFIX, *OLD_KEY_PREFIXES):
        return plain[3:]
    return plain


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def generate_signing_key(path: Path) -> str:
    """Create the Ed25519 key that signs bundles. Returns the public key for hrs (base64)."""
    key = Ed25519PrivateKey.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    path.chmod(0o600)
    return public_key_b64(key)


def public_key_b64(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


class KeyServer:
    def __init__(self, settings: Settings):
        self.s = settings
        self.db = DB(settings.data / "keys.sqlite3").init()
        self.bundles = settings.data / "bundles"
        self.bundles.mkdir(parents=True, exist_ok=True)
        self.library = library.Library(self.db, settings.data / "library")
        self._pin_existing()

    def _pin_existing(self):
        """Keys in use before versions were pinned keep the version they last downloaded, so a version
        published after the update reaches nobody silently."""
        with self._tx() as c:
            for r in c.execute("SELECT k.id, (SELECT e.detail FROM events e WHERE e.key_id = k.id AND e.kind = 'download'"
                               " ORDER BY e.at DESC, e.id DESC LIMIT 1) AS got FROM keys k"
                               " WHERE k.pinned_version IS NULL").fetchall():
                if r["got"] and self._bundle_row(c, r["got"]):
                    self._pin(c, r["id"], r["got"])

    def _tx(self):
        return self.db.tx(keep_on=KeyServerError)

    # --- hashing and tokens ---

    def _hash(self, purpose: str, value: str) -> str:
        return hmac.new(self.s.secret, f"{purpose}:{value}".encode(), hashlib.sha256).hexdigest()

    def _token(self, key_id: str, install_hash: str, expires: datetime) -> str:
        body = b64(json.dumps({"k": key_id, "i": install_hash, "exp": int(expires.timestamp())}).encode())
        return f"{body}.{self._hash('token', body)}"

    def _read_token(self, token: str) -> dict:
        body, _, sig = (token or "").partition(".")
        if not body or not hmac.compare_digest(sig, self._hash("token", body)):
            raise KeyServerError("invalid_token", "Токен не подходит: войдите заново через jbs login", 401)
        data = json.loads(unb64(body))
        if data["exp"] < time.time():
            raise KeyServerError("token_expired", "Токен устарел: hrs обновит его сам", 401)
        return data

    def _log(self, c, key_id, kind, detail=None):
        c.execute("INSERT INTO events (key_id, kind, detail, at) VALUES (?, ?, ?, ?)", (key_id, kind, detail, iso(now())))

    # --- key state ---

    def _check_usable(self, c, row):
        if row["revoked_at"]:
            self._log(c, row["id"], "denied", "revoked")
            raise KeyServerError("key_revoked", "Ключ заблокирован. Напишите в поддержку.", 403)
        if parse(row["expires_at"]) <= now():
            self._log(c, row["id"], "denied", "expired")
            raise KeyServerError("key_expired", f"Срок ключа закончился. Продлить: {self.s.renew_url}", 403)

    def _pick(self, c, channel: str, client: str | None):
        """The bundle a key of this channel gets with this hrs: the channel's current version, else the
        newest one; if this hrs cannot read it, the newest it can. Returns (row, hrs_update_needed)."""
        row = None
        for ch in ([channel] if channel == "stable" else [channel, "stable"]):
            row = c.execute("SELECT b.* FROM current_bundle cur JOIN bundles b ON b.version = cur.version"
                            " WHERE cur.channel = ?", (ch,)).fetchone()
            if row:
                break
        row = row or c.execute("SELECT * FROM bundles ORDER BY published_at DESC, rowid DESC LIMIT 1").fetchone()
        if row is None or readable(row["min_hrs"], client):
            return row, False
        for older in c.execute("SELECT * FROM bundles WHERE published_at <= ? ORDER BY published_at DESC, rowid DESC",
                               (row["published_at"],)):
            if readable(older["min_hrs"], client):
                return older, True
        return None, True

    def _status(self, c, row, client: str | None = None) -> dict:
        expires = parse(row["expires_at"])
        left = expires - now()
        tariff = c.execute("SELECT id, title FROM tariffs WHERE id = ?", (row["tariff_id"],)).fetchone()
        picked, update, offer = self._serve(c, row, client)
        latest = {"version": picked["version"], "sha256": picked["sha256"]} if picked else None
        expiring = left <= WARN_BEFORE
        return {
            "key_hint": row["key_hint"], "tariff": dict(tariff), "expires_at": row["expires_at"],
            "days_left": max(left.days, 0), "state": "expiring" if expiring else "active",
            "renew_url": self.s.renew_url,
            "license_url": self.s.license_url,
            "warning": (f"Ключ действует до {row['expires_at'][:10]}. Продлите его, чтобы методика не отключилась: "
                        f"{self.s.renew_url}") if expiring else None,
            "bundle": latest,
            "hrs_update": HRS_UPDATE if update else None,
            "watermark": watermark.key_id(row["key_hash"]),
        } | offer

    def _bundle_row(self, c, version: str | None):
        return c.execute("SELECT rowid AS pos, * FROM bundles WHERE version = ?", (version,)).fetchone() \
            if version else None

    def _pin(self, c, key_id: str, version: str, auto: dict | None = None):
        c.execute("UPDATE keys SET pinned_version = ?, pin_auto = ? WHERE id = ?",
                  (version, json.dumps(auto, ensure_ascii=False) if auto else None, key_id))

    def _changes(self, c, after, upto) -> list[dict]:
        """What changed in the versions published after `after` up to `upto`, oldest first."""
        return [{"version": r["version"], "published_at": r["published_at"], "note": r["note"],
                 "important": bool(r["important"])}
                for r in c.execute("SELECT * FROM bundles WHERE (published_at, rowid) > (?, ?)"
                                   " AND (published_at, rowid) <= (?, ?) ORDER BY published_at, rowid",
                                   (after["published_at"], after["pos"], upto["published_at"], upto["pos"]))]

    def _serve(self, c, row, client: str | None):
        """The bundle this key gets now: (row, hrs_update_needed, what to tell the person).
        Old hrs gets the channel's version as before, and the key is pinned to it. hrs 0.4.0+ keeps the
        pinned version: a newer one is offered with its changes, an important one is installed and
        explained, and a rollback of the channel to an older version takes the key back too."""
        offered, update = self._pick(c, row["channel"], client)
        if not consents(client):
            if offered and offered["version"] != row["pinned_version"]:
                self._pin(c, row["id"], offered["version"])
            return offered, update, {}
        pinned = self._bundle_row(c, row["pinned_version"])
        if offered is None:
            return (pinned if pinned and readable(pinned["min_hrs"], client) else None), update, {}
        if pinned is None or not readable(pinned["min_hrs"], client):
            self._pin(c, row["id"], offered["version"])
            return offered, update, {}
        if pinned["version"] == offered["version"]:
            return pinned, update, ({"auto_updated": json.loads(row["pin_auto"])} if row["pin_auto"] else {})
        new = self._bundle_row(c, offered["version"])
        if (new["published_at"], new["pos"]) < (pinned["published_at"], pinned["pos"]):
            self._pin(c, row["id"], offered["version"])
            return offered, update, {}
        changes = self._changes(c, pinned, new)
        if any(ch["important"] for ch in changes):
            auto = {"from": pinned["version"], "to": new["version"], "at": iso(now()), "changes": changes}
            self._pin(c, row["id"], new["version"], auto)
            return offered, update, {"auto_updated": auto}
        return pinned, update, {"update_available": {"version": new["version"], "changes": changes}}

    def _row_by_token(self, c, token):
        data = self._read_token(token)
        row = c.execute("SELECT * FROM keys WHERE id = ?", (data["k"],)).fetchone()
        seen = c.execute("SELECT 1 FROM activations WHERE key_id = ? AND install_hash = ?", (data["k"], data["i"])).fetchone()
        if row is None or seen is None:
            raise KeyServerError("invalid_token", "Токен не подходит: войдите заново через jbs login", 401)
        return row, data["i"]

    # --- public API ---

    def activate(self, key: str, install_id: str, client: str | None = None) -> dict:
        if not install_id or len(install_id) > 100:
            raise KeyServerError("invalid_request", "install_id обязателен")
        with self._tx() as c:
            row = c.execute("SELECT * FROM keys WHERE key_hash = ?", (self._hash("key", normalize_key(key)),)).fetchone()
            if row is None:
                raise KeyServerError("invalid_key", "Ключ не найден. Проверьте, что он скопирован целиком.", 404)
            self._check_usable(c, row)
            install_hash = self._hash("install", install_id)
            known = c.execute("SELECT 1 FROM activations WHERE key_id = ? AND install_hash = ?",
                              (row["id"], install_hash)).fetchone()
            if not known:
                used = c.execute("SELECT count(*) FROM activations WHERE key_id = ?", (row["id"],)).fetchone()[0]
                if used >= row["device_limit"]:
                    self._log(c, row["id"], "denied", "device_limit")
                    raise KeyServerError("device_limit", f"Ключ уже подключён на {used} компьютерах. "
                                         "Чтобы перенести его, напишите в поддержку.", 403)
                c.execute("INSERT INTO activations VALUES (?, ?, ?, ?)", (row["id"], install_hash, iso(now()), iso(now())))
            else:
                c.execute("UPDATE activations SET last_seen = ? WHERE key_id = ? AND install_hash = ?",
                          (iso(now()), row["id"], install_hash))
            self._log(c, row["id"], "activate")
            expires = min(now() + TOKEN_TTL, parse(row["expires_at"]))
            return {"token": self._token(row["id"], install_hash, expires), "token_expires_at": iso(expires),
                    **self._status(c, row, client)}

    def status(self, token: str, client: str | None = None) -> dict:
        with self._tx() as c:
            row, install_hash = self._row_by_token(c, token)
            self._check_usable(c, row)
            c.execute("UPDATE activations SET last_seen = ? WHERE key_id = ? AND install_hash = ?",
                      (iso(now()), row["id"], install_hash))
            self._log(c, row["id"], "check")
            return self._status(c, row, client)

    def bundle(self, token: str, client: str | None = None) -> tuple[dict, bytes]:
        with self._tx() as c:
            row, _ = self._row_by_token(c, token)
            self._check_usable(c, row)
            latest, _, _ = self._serve(c, row, client)
            if latest is None:
                raise KeyServerError("no_bundle", "Методика ещё не опубликована", 404)
            self._log(c, row["id"], "download", latest["version"])
            return dict(latest), (self.bundles / f"{latest['version']}.zip").read_bytes()

    def export(self, token: str | None) -> None:
        """A request for the whole methodology. It is always refused: the licence allows working with it inside
        JobShturman only. With a valid token the attempt is kept in the key's history."""
        if token:
            with self._tx() as c, contextlib.suppress(KeyServerError):
                row, _ = self._row_by_token(c, token)
                self._log(c, row["id"], "denied", "export")
        raise KeyServerError("license_forbidden", "Скачивать, выгружать и копировать методику JobShturman запрещено "
                                                  "лицензионным соглашением: она работает только внутри JobShturman "
                                                  f"и его агента. Соглашение: {self.s.license_url}", 403)

    def accept(self, token: str, version: str, client: str | None = None) -> dict:
        """The person agreed to the offered version: the key moves to it, hrs downloads it next."""
        with self._tx() as c:
            row, _ = self._row_by_token(c, token)
            self._check_usable(c, row)
            offered, _ = self._pick(c, row["channel"], client)
            if offered is None or offered["version"] != version:
                raise KeyServerError("not_offered", "Эта версия методики сейчас не предлагается: проверьте ключ "
                                     "ещё раз (jbs sync)", 409)
            self._pin(c, row["id"], version)
            self._log(c, row["id"], "check", f"accept {version}")
            row = c.execute("SELECT * FROM keys WHERE id = ?", (row["id"],)).fetchone()
            return self._status(c, row, client)

    # --- admin ---

    def tariffs(self) -> list[dict]:
        with self._tx() as c:
            return [dict(r) for r in c.execute("SELECT * FROM tariffs ORDER BY price_rub_from")]

    def update_tariff(self, tariff_id: str, **fields) -> dict:
        allowed = {"title", "price_rub_from", "price_rub_to", "months", "device_limit", "active"}
        fields = {k: v for k, v in fields.items() if v is not None}
        if set(fields) - allowed:
            raise KeyServerError("invalid_request", f"можно менять только {sorted(allowed)}")
        with self._tx() as c:
            if not c.execute("SELECT 1 FROM tariffs WHERE id = ?", (tariff_id,)).fetchone():
                raise KeyServerError("not_found", "Нет такого тарифа", 404)
            for k, v in fields.items():
                c.execute(f"UPDATE tariffs SET {k} = ? WHERE id = ?", (v, tariff_id))
            self._log(c, None, "admin", f"tariff {tariff_id} {sorted(fields)}")
            return dict(c.execute("SELECT * FROM tariffs WHERE id = ?", (tariff_id,)).fetchone())

    def issue(self, tariff_id: str, months: int | None = None, expires_at: str | None = None,
              order_ref: str | None = None, device_limit: int | None = None) -> dict:
        """New key. The plain key is returned only here; the server keeps its hash."""
        raw = "".join(secrets.choice(KEY_ALPHABET) for _ in range(20))
        key = f"{KEY_PREFIX}-" + "-".join(raw[i:i + 4] for i in range(0, 20, 4))
        with self._tx() as c:
            tariff = c.execute("SELECT * FROM tariffs WHERE id = ? AND active = 1", (tariff_id,)).fetchone()
            if tariff is None:
                raise KeyServerError("not_found", "Нет такого действующего тарифа", 404)
            expires = parse(expires_at) if expires_at else add_months(now(), months or tariff["months"])
            key_id = uuid.uuid4().hex[:12]
            c.execute("INSERT INTO keys (id, key_hash, key_hint, tariff_id, issued_at, expires_at, device_limit, order_ref)"
                      " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      (key_id, self._hash("key", normalize_key(key)), raw[:4], tariff_id, iso(now()), iso(expires),
                       device_limit or tariff["device_limit"], order_ref))
            self._log(c, key_id, "admin", "issue")
            return {"key": key, **self._key_view(c, key_id)}

    def _key_view(self, c, key_id) -> dict:
        row = c.execute("SELECT * FROM keys WHERE id = ?", (key_id,)).fetchone()
        if row is None:
            raise KeyServerError("not_found", "Нет такого ключа", 404)
        acts = [dict(r) for r in c.execute("SELECT install_hash, first_seen, last_seen FROM activations WHERE key_id = ?"
                                           " ORDER BY first_seen", (key_id,))]
        last = c.execute("SELECT max(at) FROM events WHERE key_id = ? AND kind IN ('check', 'download', 'activate')",
                         (key_id,)).fetchone()[0]
        counts = dict(c.execute("SELECT kind, count(*) FROM events WHERE key_id = ? GROUP BY kind", (key_id,)).fetchall())
        view = {k: row[k] for k in row.keys() if k not in ("key_hash", "pin_auto")}  # noqa: SIM118 (sqlite3.Row)
        if row["revoked_at"]:
            state = "revoked"
        elif parse(row["expires_at"]) <= now():
            state = "expired"
        elif parse(row["expires_at"]) - now() <= WARN_BEFORE:
            state = "expiring"
        else:
            state = "active"
        return view | {"state": state, "watermark": watermark.key_id(row["key_hash"]), "devices": [{"install": a["install_hash"][:8], "first_seen": a["first_seen"],
                                    "last_seen": a["last_seen"]} for a in acts],
                       "last_seen": last, "downloads": counts.get("download", 0), "denied": counts.get("denied", 0)}

    def keys(self, state: str | None = None) -> list[dict]:
        with self._tx() as c:
            ids = [r[0] for r in c.execute("SELECT id FROM keys ORDER BY issued_at DESC")]
            views = [self._key_view(c, i) for i in ids]
        return [v for v in views if state is None or v["state"] == state]

    def key(self, key_id: str) -> dict:
        with self._tx() as c:
            return self._key_view(c, key_id)

    def extend(self, key_id: str, months: int | None = None, until: str | None = None) -> dict:
        """Renew: months are added to the later of today and the current end date."""
        if bool(months) == bool(until):
            raise KeyServerError("invalid_request", "Укажите months или until")
        with self._tx() as c:
            view = self._key_view(c, key_id)
            new = parse(until) if until else add_months(max(parse(view["expires_at"]), now()), months)
            c.execute("UPDATE keys SET expires_at = ? WHERE id = ?", (iso(new), key_id))
            self._log(c, key_id, "admin", f"extend to {iso(new)}")
            return self._key_view(c, key_id)

    def revoke(self, key_id: str, reason: str) -> dict:
        with self._tx() as c:
            self._key_view(c, key_id)
            try:
                c.execute("UPDATE keys SET revoked_at = ?, revoke_reason = ? WHERE id = ?", (iso(now()), reason, key_id))
            except Exception as exc:
                raise KeyServerError("invalid_request", "reason: refund, violation, reissue или other") from exc
            self._log(c, key_id, "admin", f"revoke {reason}")
            return self._key_view(c, key_id)

    def restore(self, key_id: str) -> dict:
        with self._tx() as c:
            self._key_view(c, key_id)
            c.execute("UPDATE keys SET revoked_at = NULL, revoke_reason = NULL WHERE id = ?", (key_id,))
            self._log(c, key_id, "admin", "restore")
            return self._key_view(c, key_id)

    def reset_devices(self, key_id: str) -> dict:
        with self._tx() as c:
            self._key_view(c, key_id)
            c.execute("DELETE FROM activations WHERE key_id = ?", (key_id,))
            self._log(c, key_id, "admin", "reset devices")
            return self._key_view(c, key_id)

    def find_watermark(self, text: str) -> dict:
        """Which keys the watermark ids in a copied methodology text belong to."""
        ids = watermark.decode(text)
        with self._tx() as c:
            rows = {watermark.key_id(r["key_hash"]): r["id"] for r in c.execute("SELECT id, key_hash FROM keys")}
            return {"ids": ids, "keys": [self._key_view(c, rows[i]) for i in ids if i in rows],
                    "unknown": [i for i in ids if i not in rows]}

    def unbind_device(self, key_id: str, install: str) -> dict:
        """Free one device slot; `install` is the 8-char prefix shown in devices[].install."""
        with self._tx() as c:
            self._key_view(c, key_id)
            found = [r[0] for r in c.execute("SELECT install_hash FROM activations WHERE key_id = ?"
                                             " AND substr(install_hash, 1, 8) = ?", (key_id, install))]
            if not found:
                raise KeyServerError("not_found", "Нет такого устройства у ключа", 404)
            if len(found) > 1:
                raise KeyServerError("ambiguous_device", "Префикс подходит к нескольким устройствам: "
                                     "сбросьте все устройства", 409)
            c.execute("DELETE FROM activations WHERE key_id = ? AND install_hash = ?", (key_id, found[0]))
            self._log(c, key_id, "admin", f"unbind device {install}")
            return self._key_view(c, key_id)

    def events(self, key_id: str, limit: int = 100) -> list[dict]:
        """Latest events of a key, newest first."""
        with self._tx() as c:
            self._key_view(c, key_id)
            return [dict(r) for r in c.execute("SELECT kind, detail, at FROM events WHERE key_id = ?"
                                               " ORDER BY at DESC, id DESC LIMIT ?", (key_id, limit))]

    # --- bundles ---

    def _signer(self) -> Ed25519PrivateKey:
        if not self.s.signing_key.is_file():
            raise KeyServerError("no_signing_key", "Нет ключа подписи: hrs-keys signing-key", 500)
        return serialization.load_pem_private_key(self.s.signing_key.read_bytes(), password=None)

    def public_key(self) -> str:
        return public_key_b64(self._signer())

    @staticmethod
    def _note(note: str | None) -> str:
        note = (note or "").strip()
        if not note:
            raise KeyServerError("note_required", "Напишите коротко, что изменилось в этой версии: "
                                 "это покажут людям, когда им предложат обновиться")
        if len(note) > NOTE_MAX:
            raise KeyServerError("invalid_request", f"«Что изменилось»: не длиннее {NOTE_MAX} знаков")
        return note

    def publish(self, data: bytes, channel: str | None = "stable", revision: int | None = None,
                note: str | None = None, important: bool = False) -> dict:
        """Publish a bundle built by `hrs-keys bundle build` or from the draft; its version comes from
        bundle.json inside. channel: who gets it now (stable: everyone, test: test keys); None: nobody yet.
        note (required): what changed, shown to people; important: installed without asking (hrs 0.4.0+)."""
        note = self._note(note)
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                meta = json.loads(z.read("bundle.json"))
        except (zipfile.BadZipFile, KeyError, ValueError) as exc:
            raise KeyServerError("invalid_bundle", "Нужен zip с bundle.json внутри") from exc
        version = str(meta.get("version", ""))
        if not version or "/" in version or version.startswith("."):
            raise KeyServerError("invalid_bundle", "В bundle.json нет корректной version")
        if channel not in (None, *CHANNELS):
            raise KeyServerError("invalid_request", "channel: stable или test")
        signature = base64.b64encode(self._signer().sign(data)).decode()
        with self._tx() as c:
            if c.execute("SELECT 1 FROM bundles WHERE version = ?", (version,)).fetchone():
                raise KeyServerError("bundle_exists", f"Версия {version} уже опубликована", 409)
            (self.bundles / f"{version}.zip").write_bytes(data)
            c.execute("INSERT INTO bundles (version, sha256, size, signature, published_at, format, min_hrs, revision,"
                      " note, important) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (version, hashlib.sha256(data).hexdigest(), len(data), signature, iso(now()),
                       int(meta.get("format") or 1), meta.get("min_hrs"), revision, note, int(bool(important))))
            self._log(c, None, "admin", f"publish {version}" + (f" to {channel}" if channel else ""))
            if channel:
                self._set_current(c, version, channel)
            return self._bundle_view(c, version)

    def _set_current(self, c, version: str, channel: str):
        """stable moves both channels: test keys never get an older version than everyone."""
        for ch in (CHANNELS if channel == "stable" else (channel,)):
            c.execute("INSERT INTO current_bundle (channel, version) VALUES (?, ?)"
                      " ON CONFLICT(channel) DO UPDATE SET version = excluded.version", (ch, version))

    def set_current(self, version: str, channel: str) -> dict:
        """Give a published version to a channel: promote a tested one to everyone, or roll back."""
        if channel not in CHANNELS:
            raise KeyServerError("invalid_request", "channel: stable или test")
        with self._tx() as c:
            if not c.execute("SELECT 1 FROM bundles WHERE version = ?", (version,)).fetchone():
                raise KeyServerError("not_found", f"Нет версии {version}", 404)
            self._set_current(c, version, channel)
            self._log(c, None, "admin", f"current {channel} = {version}")
            return self._bundle_view(c, version)

    def _bundle_view(self, c, version: str) -> dict:
        row = dict(c.execute("SELECT * FROM bundles WHERE version = ?", (version,)).fetchone())
        row["channels"] = [r[0] for r in c.execute("SELECT channel FROM current_bundle WHERE version = ?"
                                                    " ORDER BY channel", (version,))]
        row["important"] = bool(row["important"])
        row["keys"] = c.execute("SELECT count(*) FROM keys WHERE pinned_version = ?", (version,)).fetchone()[0]
        return row

    def bundle_list(self) -> list[dict]:
        with self._tx() as c:
            versions = [r[0] for r in c.execute("SELECT version FROM bundles ORDER BY published_at DESC, rowid DESC")]
            return [self._bundle_view(c, v) for v in versions]

    def set_channel(self, key_id: str, channel: str) -> dict:
        """test: this key gets versions published for checking before everyone else."""
        if channel not in CHANNELS:
            raise KeyServerError("invalid_request", "channel: stable или test")
        with self._tx() as c:
            self._key_view(c, key_id)
            c.execute("UPDATE keys SET channel = ? WHERE id = ?", (channel, key_id))
            self._log(c, key_id, "admin", f"channel {channel}")
            return self._key_view(c, key_id)

    # --- the methodology draft (the editor in the admin page) ---

    def _lib(self, fn):
        try:
            with self._tx() as c:
                return fn(c)
        except library.LibraryError as exc:
            raise KeyServerError(exc.code, str(exc), exc.status) from exc

    def _draft_manifest(self, c) -> dict:
        tree = self.library.tree(c)
        if methodology.MANIFEST not in tree:
            return {}
        try:
            return json.loads(self.library.blob(tree[methodology.MANIFEST]))
        except ValueError:
            return {}

    def draft(self) -> dict:
        """The draft as the editor shows it: files, the route (steps, stages with numbers) and the check."""
        def go(c):
            tree = self.library.tree(c)
            files = {p: self.library.blob(d) for p, d in tree.items()}
            lib = bundles_mod._Files(files)
            try:
                route = methodology.route(lib).public() if files else None
            except (DomainError, KeyError, ValueError) as exc:  # broken manifest: the check names it
                route = {"error": str(exc)}
            manifest = self._draft_manifest(c)
            stages = []
            for i, st in enumerate(manifest.get("stages") or []):
                d = (st.get("dir") or "").rstrip("/")
                stages.append(st | {"files": sorted(p for p in tree if p.startswith(d + "/")) if d else [],
                                    "position": i})
            current = {r["channel"]: r["version"] for r in c.execute("SELECT * FROM current_bundle")}
            return {"head": self.library.head(c), "files": [{"path": p, "sha": d, "size": len(files[p])}
                                                             for p, d in sorted(tree.items())],
                    "route": route, "steps": manifest.get("steps") or [], "stages": stages,
                    "manifest_version": manifest.get("version"), "current": current,
                    "check": self._check(files) if files else None,
                    "genesis": methodology.GENESIS}
        return self._lib(go)

    @classmethod
    def _check(cls, files: dict[str, bytes]) -> dict:
        """The methodology check plus ours: the main prompt must be the author's text, not the placeholder."""
        report = methodology.validate(cls._texts(files))
        if library.is_placeholder(files.get(methodology.GENESIS)):
            report["errors"].append({"code": "genesis_placeholder", "stage": None,
                                     "detail": f"{methodology.GENESIS}: вместо главных правил заглушка"})
            report["ok"] = False
        return report

    def _published_genesis(self) -> bytes | None:
        """The main prompt of the version everyone gets now, if it has one."""
        with self._tx() as c:
            row, _ = self._pick(c, "stable", "999")
        if row is None:
            return None
        try:
            return library.unzip((self.bundles / f"{row['version']}.zip").read_bytes()).get(methodology.GENESIS)
        except (OSError, library.LibraryError):
            return None

    @staticmethod
    def _texts(files: dict[str, bytes]) -> dict[str, str]:
        return {p: (d.decode("utf-8", "replace") if p.endswith((".md", ".json")) else "") for p, d in files.items()}

    def draft_file(self, path: str, revision: int | None = None) -> dict:
        def go(c):
            tree = self.library.tree(c, revision)
            p = library.clean_path(path)
            if p not in tree:
                raise library.LibraryError("not_found", f"Нет файла {p}", 404)
            data = self.library.blob(tree[p])
            return {"path": p, "sha": tree[p], "text": data.decode("utf-8", "replace"), "size": len(data)}
        return self._lib(go)

    def draft_save(self, path: str, text: str, base_sha: str | None, by: str, message: str | None = None) -> dict:
        """Save one file. base_sha: the version the editor opened (None for a new file)."""
        return self._lib(lambda c: self.library.change(
            c, {path: text.encode("utf-8")}, by, message or f"Правка {path}", iso(now()), expected={path: base_sha}))

    def draft_delete(self, path: str, base_sha: str | None, by: str) -> dict:
        return self._lib(lambda c: self.library.change(c, {path: None}, by, f"Удалён {path}", iso(now()),
                                                       expected={path: base_sha}))

    def draft_upload(self, filename: str, data: bytes, by: str, path: str | None = None) -> dict:
        """A zip replaces the whole draft (a library folder or a published bundle); any other file is
        added at `path` (or its own name). Either way it is one revision and can be rolled back."""
        is_zip = (filename or "").lower().endswith(".zip")
        genesis = self._published_genesis() if is_zip else None

        def go(c):
            if is_zip:
                files = library.prepare(library.unzip(data), genesis)
                return self.library.change(c, files, by, f"Загружен архив {filename}", iso(now()), replace=True)
            return self.library.change(c, {path or filename: data}, by, f"Загружен {path or filename}", iso(now()))
        return self._lib(go)

    def draft_import_current(self, by: str) -> dict:
        """Start the draft from the version everyone gets now."""
        with self._tx() as c:
            row, _ = self._pick(c, "stable", "999")
        if row is None:
            raise KeyServerError("no_bundle", "Методика ещё не опубликована: загрузите архив библиотеки", 404)
        data = (self.bundles / f"{row['version']}.zip").read_bytes()
        return self._lib(lambda c: self.library.change(
            c, library.prepare(library.unzip(data)), by, f"Взята опубликованная версия {row['version']}", iso(now()),
            replace=True))

    def draft_route(self, steps: list[dict], stages: list[dict], by: str, version: str | None = None) -> dict:
        """Save the route: steps and stages in order. A new stage gets SKILL.md and procedure.md from a
        template; files of removed stages stay until deleted (a stage can also be switched off)."""
        def go(c):
            manifest = self._draft_manifest(c) or {"version": "0.1.0"}
            tree = self.library.tree(c)
            changes = {}
            out = []
            for st in stages:
                sid = (st.get("id") or "").strip()
                item = {k: st[k] for k in ("id", "title", "step", "dir", "progress", "tools", "status") if k in st}
                item["id"] = sid
                item["dir"] = (st.get("dir") or f"skills/{sid}").rstrip("/")
                if st.get("status") in (None, "", "active"):
                    item.pop("status", None)
                if not item.get("progress"):
                    item.pop("progress", None)
                item["tools"] = [t for t in (st.get("tools") or []) if t]
                skill = f"{item['dir']}/SKILL.md"
                if methodology.ID.match(sid) and skill not in tree:
                    title = st.get("title") or sid
                    changes[skill] = library.STAGE_SKILL.format(id=sid, title=title).encode()
                    changes[f"{item['dir']}/references/procedure.md"] = \
                        library.STAGE_PROCEDURE.format(title=title).encode()
                out.append(item)
            manifest = manifest | {"format": methodology.FORMAT, "steps": steps, "stages": out}
            if version:
                manifest["version"] = version
            changes[methodology.MANIFEST] = library.dump(manifest)
            return self.library.change(c, changes, by, "Маршрут: этапы и шаги", iso(now()))
        return self._lib(go)

    def draft_revisions(self, limit: int = 100) -> list[dict]:
        return self._lib(lambda c: self.library.revisions(c, limit))

    def draft_diff(self, revision: int, against: int | None = None) -> list[dict]:
        return self._lib(lambda c: self.library.diff(c, revision, against))

    def draft_restore(self, revision: int, by: str) -> dict:
        def go(c):
            tree = self.library.tree(c, revision)
            return self.library.commit(c, tree, by, f"Возврат к версии черновика {revision}", iso(now()))
        return self._lib(go)

    def draft_check(self) -> dict:
        return self._lib(lambda c: self._check(self.library.files(c)))

    def draft_publish(self, channel: str, by: str, note: str | None = None, important: bool = False) -> dict:
        """Check the draft, build the bundle from its newest revision, sign and publish it."""
        note = self._note(note)
        def snapshot(c):
            head = self.library.head(c)
            if head is None:
                raise library.LibraryError("empty_draft", "Черновик пуст: загрузите библиотеку", 400)
            return head, self.library.files(c, head["id"])
        head, files = self._lib(snapshot)
        report = self._check(files)
        if not report["ok"]:
            raise KeyServerError("check_failed", "Проверка не пройдена: " + "; ".join(
                e["detail"] for e in report["errors"][:5]), 422)
        manifest = json.loads(files.get(methodology.MANIFEST, b"{}") or b"{}")
        version = f"{manifest.get('version') or '0'}.r{head['id']}"
        with self._tx() as c:
            if c.execute("SELECT 1 FROM bundles WHERE version = ?", (version,)).fetchone():
                raise KeyServerError("bundle_exists", f"Этот черновик уже опубликован как {version}: сделайте его "
                                     "текущим для нужного канала", 409)
        _, data = bundles_mod.build_files(files, version)
        out = self.publish(data, channel=channel, revision=head["id"], note=note, important=important)
        return out | {"check": report}

    def draft_export(self, revision: int | None = None) -> bytes:
        """The draft as a zip, to keep a copy or to move it into the methodology repository."""
        files = self._lib(lambda c: self.library.files(c, revision))
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for path, data in sorted(files.items()):
                z.writestr(path, data)
        return out.getvalue()

    def stats(self) -> dict:
        keys = self.keys()
        by_state = {}
        for k in keys:
            by_state[k["state"]] = by_state.get(k["state"], 0) + 1
        with self._tx() as c:
            current = {r["channel"]: r["version"] for r in c.execute("SELECT * FROM current_bundle")}
        latest = current.get("stable") or next(iter(b["version"] for b in self.bundle_list()), None)
        return {"keys": len(keys), "by_state": by_state, "devices": sum(len(k["devices"]) for k in keys),
                "latest_bundle": latest, "test_bundle": current.get("test")}
