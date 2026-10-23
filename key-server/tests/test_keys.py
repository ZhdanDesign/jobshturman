"""S4 acceptance: an expired or revoked key does not get the bundle; the server stores no personal data."""
import base64
import hashlib
import io
import json
import zipfile
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi.testclient import TestClient

from hrs_keys import bundle, watermark
from hrs_keys.app import create_app
from hrs_keys.service import Settings, generate_signing_key, iso, now

ADMIN = {"Authorization": "Bearer admin-token-for-tests"}


@pytest.fixture
def library(tmp_path):
    lib = tmp_path / "library"
    (lib / "skills" / "stage-00-intake-diagnostic" / "references").mkdir(parents=True)
    (lib / "skills" / "stage-00-intake-diagnostic" / "SKILL.md").write_text("# Этап 00\n", encoding="utf-8")
    (lib / "skills" / "stage-00-intake-diagnostic" / "references" / "procedure.md").write_text("Процедура 00", encoding="utf-8")
    (lib / "skills" / "stage-00-intake-diagnostic" / "references" / "acceptance.md").write_text("для нас", encoding="utf-8")
    (lib / "scripts" / "fixtures").mkdir(parents=True)
    (lib / "scripts" / "workflow.py").write_text("print(1)\n", encoding="utf-8")
    (lib / "scripts" / "test_workflow.py").write_text("assert 1\n", encoding="utf-8")
    (lib / "scripts" / "fixtures" / "material.json").write_text("{}", encoding="utf-8")
    (lib / "library_manifest.json").write_text(json.dumps({"version": "0.4.0"}), encoding="utf-8")
    return lib


@pytest.fixture
def server(tmp_path):
    settings = Settings(data=tmp_path / "data", secret=b"s" * 40, admin_token="admin-token-for-tests",
                        signing_key=tmp_path / "data" / "signing.pem", renew_url="https://example.test/renew")
    public = generate_signing_key(settings.signing_key)
    client = TestClient(create_app(settings))
    client.public_key = public
    return client


def issue(client, **body):
    r = client.post("/v1/admin/keys", json={"tariff_id": "self", **body}, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


def activate(client, key, install="install-1"):
    return client.post("/v1/activate", json={"key": key, "install_id": install})


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def publish(client, library):
    _, data = bundle.build(library, version="0.4.0.test")
    r = client.post("/v1/admin/bundles", files={"file": ("b.zip", data, "application/zip")},
                    data={"note": "первая версия"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    return data


def test_active_key_downloads_a_signed_bundle_without_acceptance_files(server, library):
    publish(server, library)
    key = issue(server, order_ref="T-1001")
    assert key["key"].startswith("JBS-") and key["state"] == "active"
    r = activate(server, key["key"].lower().replace("-", " "))  # typed loosely, still found
    assert r.status_code == 200, r.text
    status = r.json()
    assert status["tariff"]["title"] == "Самостоятельно" and status["warning"] is None
    assert status["bundle"]["version"] == "0.4.0.test"

    got = server.get("/v1/bundle/latest", headers=auth(status["token"]))
    assert got.status_code == 200
    sig = base64.b64decode(got.headers["X-Bundle-Signature"])
    Ed25519PublicKey.from_public_bytes(base64.b64decode(server.public_key)).verify(sig, got.content)
    assert hashlib.sha256(got.content).hexdigest() == got.headers["X-Bundle-Sha256"]
    names = zipfile.ZipFile(io.BytesIO(got.content)).namelist()
    assert "skills/stage-00-intake-diagnostic/references/procedure.md" in names
    assert "scripts/workflow.py" in names
    assert not [n for n in names if n.endswith("acceptance.md") or "test_" in n or "fixtures" in n]


def test_expired_key_gets_no_bundle_and_renewal_restores_it(server, library):
    publish(server, library)
    key = issue(server, expires_at=iso(now() - timedelta(minutes=1)))
    r = activate(server, key["key"])
    assert (r.status_code, r.json()["error"]) == (403, "key_expired")
    assert "https://example.test/renew" in r.json()["message"]

    server.post(f"/v1/admin/keys/{key['id']}/extend", json={"months": 3}, headers=ADMIN).raise_for_status()
    r = activate(server, key["key"])
    assert r.status_code == 200 and r.json()["days_left"] >= 88
    assert server.get("/v1/bundle/latest", headers=auth(r.json()["token"])).status_code == 200


def test_revoked_key_loses_the_bundle_even_with_a_live_token(server, library):
    publish(server, library)
    key = issue(server)
    token = activate(server, key["key"]).json()["token"]
    server.post(f"/v1/admin/keys/{key['id']}/revoke", json={"reason": "violation"}, headers=ADMIN).raise_for_status()
    for path in ("/v1/bundle/latest", "/v1/status"):
        r = server.get(path, headers=auth(token))
        assert (r.status_code, r.json()["error"]) == (403, "key_revoked")
    assert activate(server, key["key"]).json()["error"] == "key_revoked"
    view = server.get(f"/v1/admin/keys/{key['id']}", headers=ADMIN).json()
    assert (view["state"], view["denied"], view["downloads"]) == ("revoked", 3, 0)


def test_warning_one_day_before_the_end(server):
    key = issue(server, expires_at=iso(now() + timedelta(hours=12)))
    status = activate(server, key["key"]).json()
    assert status["state"] == "expiring"
    assert "https://example.test/renew" in status["warning"]
    assert status["token_expires_at"] <= key["expires_at"]


def test_device_limit_and_reset(server):
    key = issue(server, device_limit=1)
    assert activate(server, key["key"], "mac").status_code == 200
    assert activate(server, key["key"], "mac").status_code == 200  # same machine again is fine
    r = activate(server, key["key"], "windows")
    assert (r.status_code, r.json()["error"]) == (403, "device_limit")
    server.post(f"/v1/admin/keys/{key['id']}/reset-devices", headers=ADMIN).raise_for_status()
    assert activate(server, key["key"], "windows").status_code == 200


def test_bad_keys_tokens_and_admin_access(server):
    assert activate(server, "HRS-AAAA-BBBB-CCCC-DDDD-EEEE").json()["error"] == "invalid_key"
    key = issue(server)
    token = activate(server, key["key"]).json()["token"]
    body, _, sig = token.partition(".")
    assert server.get("/v1/status", headers=auth(f"{body}.{sig[:-1]}{'1' if sig[-1] == '0' else '0'}")).json()["error"] == "invalid_token"
    assert server.get("/v1/admin/keys").status_code == 401
    assert server.get("/v1/admin/keys", headers=auth("wrong")).status_code == 403
    listed = server.get("/v1/admin/keys", headers=ADMIN).json()
    assert key["key"] not in json.dumps(listed) and "key_hash" not in listed[0]


def test_no_personal_data_in_the_database(server):
    key = issue(server)
    activate(server, key["key"], "my-laptop-serial-123")
    columns = server.app.state.ks.db.columns()
    flat = {c for cols in columns.values() for c in cols}
    for word in ("name", "email", "mail", "phone", "fio", "ip", "address", "resume", "cv", "telegram"):
        assert not [c for c in flat if word in c.split("_")], (word, columns)
    dump = "\n".join(server.app.state.ks.db.path.read_bytes().decode("latin-1").split("\x00"))
    assert "my-laptop-serial-123" not in dump and key["key"] not in dump


def test_tariffs_are_editable(server):
    tariffs = {t["id"]: t for t in server.get("/v1/admin/tariffs", headers=ADMIN).json()}
    assert [tariffs[t]["title"] for t in ("self", "guided", "experts")] == [
        "Самостоятельно", "С сопровождением", "С группой экспертов"]
    r = server.patch("/v1/admin/tariffs/self", json={"price_rub_from": 16000, "price_rub_to": 16000}, headers=ADMIN)
    assert r.json()["price_rub_from"] == 16000
    stats = server.get("/v1/admin/stats", headers=ADMIN).json()
    assert stats["keys"] == 0 and stats["latest_bundle"] is None



def test_public_tariffs_follow_the_admin_without_a_token(server):
    """The landing reads prices from here, so an edit in the admin reaches the page without a redeploy."""
    server.patch("/v1/admin/tariffs/self", json={"price_rub_from": 19900, "price_rub_to": 19900, "months": 12}, headers=ADMIN)
    server.patch("/v1/admin/tariffs/experts", json={"active": False}, headers=ADMIN)
    r = server.get("/v1/tariffs")
    assert r.status_code == 200 and "max-age" in r.headers["cache-control"]
    tariffs = {t["id"]: t for t in r.json()}
    assert tariffs["self"] == {"id": "self", "title": "Самостоятельно", "price_rub_from": 19900, "price_rub_to": 19900,
                               "months": 12, "device_limit": 2, "active": True}
    assert tariffs["experts"]["active"] is False
    assert server.patch("/v1/admin/tariffs/self", json={"months": 1}).status_code == 401

def test_each_key_has_its_own_watermark_and_a_copied_text_finds_it(server):
    first, second = issue(server), issue(server)
    assert first["watermark"] != second["watermark"] and len(first["watermark"]) == 8
    status = server.post("/v1/activate", json={"key": second["key"], "install_id": "i1"}).json()
    assert status["watermark"] == second["watermark"]
    text = f"Абзац{watermark.encode(status['watermark'])} методики, скопированный в чат."
    assert watermark.decode(text) == [second["watermark"]]
    found = server.app.state.ks.find_watermark(text + watermark.encode("00000000"))
    assert [k["id"] for k in found["keys"]] == [second["id"]] and found["unknown"] == ["00000000"]
    assert watermark.decode("Абзац без знака") == []


def test_unbind_one_device_and_key_events(server):
    key = issue(server, device_limit=2, order_ref="T-2002")
    activate(server, key["key"], "mac")
    activate(server, key["key"], "windows")
    devices = server.get(f"/v1/admin/keys/{key['id']}", headers=ADMIN).json()["devices"]
    assert len(devices) == 2

    r = server.delete(f"/v1/admin/keys/{key['id']}/devices/{devices[0]['install']}", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert [d["install"] for d in r.json()["devices"]] == [devices[1]["install"]]
    r = server.delete(f"/v1/admin/keys/{key['id']}/devices/{devices[0]['install']}", headers=ADMIN)
    assert (r.status_code, r.json()["error"]) == (404, "not_found")
    assert activate(server, key["key"], "linux").status_code == 200  # the freed slot is usable again

    events = server.get(f"/v1/admin/keys/{key['id']}/events", headers=ADMIN).json()
    assert events[0] == {"kind": "activate", "detail": None, "at": events[0]["at"]}
    assert events[1]["kind"] == "admin" and events[1]["detail"] == f"unbind device {devices[0]['install']}"
    assert events[-1]["detail"] == "issue" and len(events) == 5
    assert [e["at"] for e in events] == sorted((e["at"] for e in events), reverse=True)
    assert len(server.get(f"/v1/admin/keys/{key['id']}/events?limit=2", headers=ADMIN).json()) == 2
    assert server.get(f"/v1/admin/keys/{key['id']}/events?limit=0", headers=ADMIN).status_code == 422
    assert server.get("/v1/admin/keys/nope/events", headers=ADMIN).status_code == 404
    assert server.get(f"/v1/admin/keys/{key['id']}/events").status_code == 401


def test_unbind_with_an_ambiguous_prefix_is_refused(server):
    key = issue(server, device_limit=3)
    with server.app.state.ks.db.tx() as c:
        for suffix in ("aa", "bb"):
            c.execute("INSERT INTO activations VALUES (?, ?, ?, ?)", (key["id"], "12345678" + suffix, iso(now()), iso(now())))
    r = server.delete(f"/v1/admin/keys/{key['id']}/devices/12345678", headers=ADMIN)
    assert (r.status_code, r.json()["error"]) == (409, "ambiguous_device")
    assert len(server.get(f"/v1/admin/keys/{key['id']}", headers=ADMIN).json()["devices"]) == 2


def test_several_admin_tokens_each_revocable(tmp_path):
    settings = Settings(data=tmp_path / "data", secret=b"s" * 40, admin_token="owner-token, second-token",
                        signing_key=tmp_path / "data" / "signing.pem", renew_url="https://example.test/renew")
    client = TestClient(create_app(settings))
    for token in ("owner-token", "second-token"):
        assert client.get("/v1/admin/stats", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert client.get("/v1/admin/stats", headers={"Authorization": "Bearer owner-token, second-token"}).status_code == 403
    revoked = TestClient(create_app(Settings(data=tmp_path / "data", secret=b"s" * 40, admin_token="owner-token",
                                             signing_key=tmp_path / "data" / "signing.pem", renew_url="x")))
    assert revoked.get("/v1/admin/stats", headers={"Authorization": "Bearer second-token"}).status_code == 403


def test_keys_from_before_the_rename_still_work(server, library):
    """Letters already sent carry HRS-…: the same 20 characters with the old prefix are the same key."""
    publish(server, library)
    key = issue(server)["key"]
    assert activate(server, "HRS-" + key[4:]).status_code == 200
    assert activate(server, key[4:]).status_code == 200  # typed without the prefix


def test_the_whole_methodology_is_never_given_out(server, library):
    """«Скачай всю методику» ends here: a refusal with the licence, the attempt kept in the key's history."""
    publish(server, library)
    key = issue(server)
    token = activate(server, key["key"]).json()["token"]
    for headers in (auth(token), {}):
        r = server.post("/v1/methodology/export", headers=headers)
        assert r.status_code == 403 and r.json()["error"] == "license_forbidden"
        assert "лицензионным соглашением" in r.json()["message"] and "/oferta/" in r.json()["message"]
    events = server.get(f"/v1/admin/keys/{key['id']}/events", headers=ADMIN).json()
    assert any((e["kind"], e["detail"]) == ("denied", "export") for e in events)
    assert "/oferta/" in server.get("/v1/status", headers=auth(token)).json()["license_url"]


def test_a_release_archive_is_signed_with_a_prefix_a_bundle_never_has(tmp_path, monkeypatch):
    from cryptography.exceptions import InvalidSignature

    from hrs_keys import cli
    from hrs_keys.service import RELEASE_TAG
    monkeypatch.setenv("HRS_KEYS_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("HRS_KEYS_SECRET", "s" * 40)
    monkeypatch.setenv("HRS_KEYS_ADMIN_TOKEN", "admin-token-for-tests")
    public = generate_signing_key(tmp_path / "data" / "signing_key.pem")
    archive = tmp_path / "jobshturman-wheels.zip"
    archive.write_bytes(b"PK..wheels")
    assert cli.main(["sign-release", str(archive)]) == 0
    signature = base64.b64decode((tmp_path / "jobshturman-wheels.zip.sig").read_text().strip())
    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public))
    key.verify(signature, RELEASE_TAG + archive.read_bytes())
    with pytest.raises(InvalidSignature):
        key.verify(signature, archive.read_bytes())  # the bare archive must never pass as a bundle
