"""Key server rules: issue, activate, check, extend, revoke, publish and hand out the signed bundle."""
import base64
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

from . import watermark
from .db import DB

TOKEN_TTL = timedelta(hours=24)
WARN_BEFORE = timedelta(days=1)
KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I


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

    @classmethod
    def from_env(cls):
        data = Path(os.environ.get("HRS_KEYS_DATA", "data")).expanduser()
        secret = os.environ.get("HRS_KEYS_SECRET")
        if not secret or len(secret) < 32:
            raise SystemExit("HRS_KEYS_SECRET: нужна случайная строка от 32 символов")
        return cls(data=data, secret=secret.encode(), admin_token=os.environ.get("HRS_KEYS_ADMIN_TOKEN") or None,
                   signing_key=Path(os.environ.get("HRS_KEYS_SIGNING_KEY") or data / "signing_key.pem"),
                   renew_url=os.environ.get("HRS_KEYS_RENEW_URL", "https://tools.owai.ru/jobshturman/"))


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


def normalize_key(key: str) -> str:
    return "".join(ch for ch in key.upper() if ch.isalnum()).removeprefix("HRS")


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
            raise KeyServerError("invalid_token", "Токен не подходит: войдите заново через hrs login", 401)
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

    def _status(self, c, row) -> dict:
        expires = parse(row["expires_at"])
        left = expires - now()
        tariff = c.execute("SELECT id, title FROM tariffs WHERE id = ?", (row["tariff_id"],)).fetchone()
        latest = c.execute("SELECT version, sha256 FROM bundles ORDER BY published_at DESC, rowid DESC LIMIT 1").fetchone()
        expiring = left <= WARN_BEFORE
        return {
            "key_hint": row["key_hint"], "tariff": dict(tariff), "expires_at": row["expires_at"],
            "days_left": max(left.days, 0), "state": "expiring" if expiring else "active",
            "renew_url": self.s.renew_url,
            "warning": (f"Ключ действует до {row['expires_at'][:10]}. Продлите его, чтобы методика не отключилась: "
                        f"{self.s.renew_url}") if expiring else None,
            "bundle": dict(latest) if latest else None,
            "watermark": watermark.key_id(row["key_hash"]),
        }

    def _row_by_token(self, c, token):
        data = self._read_token(token)
        row = c.execute("SELECT * FROM keys WHERE id = ?", (data["k"],)).fetchone()
        seen = c.execute("SELECT 1 FROM activations WHERE key_id = ? AND install_hash = ?", (data["k"], data["i"])).fetchone()
        if row is None or seen is None:
            raise KeyServerError("invalid_token", "Токен не подходит: войдите заново через hrs login", 401)
        return row, data["i"]

    # --- public API ---

    def activate(self, key: str, install_id: str) -> dict:
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
                    **self._status(c, row)}

    def status(self, token: str) -> dict:
        with self._tx() as c:
            row, install_hash = self._row_by_token(c, token)
            self._check_usable(c, row)
            c.execute("UPDATE activations SET last_seen = ? WHERE key_id = ? AND install_hash = ?",
                      (iso(now()), row["id"], install_hash))
            self._log(c, row["id"], "check")
            return self._status(c, row)

    def bundle(self, token: str) -> tuple[dict, bytes]:
        with self._tx() as c:
            row, _ = self._row_by_token(c, token)
            self._check_usable(c, row)
            latest = c.execute("SELECT * FROM bundles ORDER BY published_at DESC, rowid DESC LIMIT 1").fetchone()
            if latest is None:
                raise KeyServerError("no_bundle", "Методика ещё не опубликована", 404)
            self._log(c, row["id"], "download", latest["version"])
            return dict(latest), (self.bundles / f"{latest['version']}.zip").read_bytes()

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
        key = "HRS-" + "-".join(raw[i:i + 4] for i in range(0, 20, 4))
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
        view = {k: row[k] for k in row.keys() if k != "key_hash"}  # noqa: SIM118 (sqlite3.Row)
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

    def publish(self, data: bytes) -> dict:
        """Publish a bundle built by `hrs-keys bundle build`; its version comes from bundle.json inside."""
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                meta = json.loads(z.read("bundle.json"))
        except (zipfile.BadZipFile, KeyError, ValueError) as exc:
            raise KeyServerError("invalid_bundle", "Нужен zip с bundle.json внутри") from exc
        version = str(meta.get("version", ""))
        if not version or "/" in version or version.startswith("."):
            raise KeyServerError("invalid_bundle", "В bundle.json нет корректной version")
        signature = base64.b64encode(self._signer().sign(data)).decode()
        with self._tx() as c:
            if c.execute("SELECT 1 FROM bundles WHERE version = ?", (version,)).fetchone():
                raise KeyServerError("bundle_exists", f"Версия {version} уже опубликована", 409)
            (self.bundles / f"{version}.zip").write_bytes(data)
            c.execute("INSERT INTO bundles VALUES (?, ?, ?, ?, ?)",
                      (version, hashlib.sha256(data).hexdigest(), len(data), signature, iso(now())))
            self._log(c, None, "admin", f"publish {version}")
            return dict(c.execute("SELECT * FROM bundles WHERE version = ?", (version,)).fetchone())

    def bundle_list(self) -> list[dict]:
        with self._tx() as c:
            return [dict(r) for r in c.execute("SELECT * FROM bundles ORDER BY published_at DESC, rowid DESC")]

    def stats(self) -> dict:
        keys = self.keys()
        by_state = {}
        for k in keys:
            by_state[k["state"]] = by_state.get(k["state"], 0) + 1
        latest = self.bundle_list()[:1]
        return {"keys": len(keys), "by_state": by_state, "devices": sum(len(k["devices"]) for k in keys),
                "latest_bundle": latest[0]["version"] if latest else None}
