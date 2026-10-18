"""Methodology updates by consent: each key stays on its version, hrs 0.4.0+ is offered a newer one with
what changed and switches only after the person agrees; an important version is installed with an
explanation; a rollback takes keys back; old hrs gets the channel's version as before."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from hrs_keys import bundle
from hrs_keys.app import create_app
from hrs_keys.service import KeyServer, Settings, generate_signing_key

ADMIN = {"Authorization": "Bearer admin-token"}
NEW = "0.4.0"
OLD = "0.3.0"


@pytest.fixture
def settings(tmp_path):
    s = Settings(data=tmp_path / "data", secret=b"s" * 40, admin_token="admin-token",
                 signing_key=tmp_path / "data" / "signing.pem", renew_url="https://example.test/renew")
    generate_signing_key(s.signing_key)
    return s


@pytest.fixture
def server(settings):
    return TestClient(create_app(settings))


@pytest.fixture
def library(tmp_path):
    lib = tmp_path / "library"
    (lib / "skills" / "stage-00-intake-diagnostic").mkdir(parents=True)
    (lib / "skills" / "stage-00-intake-diagnostic" / "SKILL.md").write_text("# Этап 00\n", encoding="utf-8")
    (lib / "library_manifest.json").write_text(json.dumps({"version": "0.4.0"}), encoding="utf-8")
    return lib


def publish(server, library, version, note="правка", channel="stable", important=False, back_to=None):
    """An uploaded archive goes to everyone; for a test-only version everyone is set back to `back_to`."""
    _, data = bundle.build(library, version=version)
    r = server.post("/v1/admin/bundles", files={"file": ("b.zip", data)},
                    data={"note": note, "important": str(important).lower()}, headers=ADMIN)
    assert r.status_code == 201, r.text
    if channel != "stable":
        server.post(f"/v1/admin/bundles/{back_to}/current", json={"channel": "stable"}, headers=ADMIN)
        r = server.post(f"/v1/admin/bundles/{version}/current", json={"channel": channel}, headers=ADMIN)
    return r.json()


def key(server, channel="stable", install="pc-1", hrs=NEW):
    k = server.post("/v1/admin/keys", json={"tariff_id": "self"}, headers=ADMIN).json()
    if channel != "stable":
        server.post(f"/v1/admin/keys/{k['id']}/channel", json={"channel": channel}, headers=ADMIN).raise_for_status()
    return k | token(server, k["key"], install, hrs)


def token(server, plain, install="pc-1", hrs=NEW):
    r = server.post("/v1/activate", json={"key": plain, "install_id": install}, headers={"X-Hrs-Version": hrs})
    assert r.status_code == 200, r.text
    return {"token": r.json()["token"], "first": r.json()}


def status(server, k, hrs=NEW):
    return server.get("/v1/status", headers={"Authorization": f"Bearer {k['token']}", "X-Hrs-Version": hrs}).json()


def got(server, k, hrs=NEW):
    return server.get("/v1/bundle/latest", headers={"Authorization": f"Bearer {k['token']}",
                                                    "X-Hrs-Version": hrs}).headers["X-Bundle-Version"]


def accept(server, k, version, hrs=NEW):
    return server.post("/v1/bundle/accept", json={"version": version},
                       headers={"Authorization": f"Bearer {k['token']}", "X-Hrs-Version": hrs})


def test_no_version_is_published_without_what_changed(server, library):
    _, data = bundle.build(library, version="1")
    r = server.post("/v1/admin/bundles", files={"file": ("b.zip", data)}, data={"note": "  "}, headers=ADMIN)
    assert r.status_code == 400 and r.json()["error"] == "note_required"
    assert server.get("/v1/admin/bundles", headers=ADMIN).json() == []


def test_a_new_version_is_offered_and_installed_only_after_consent(server, library):
    publish(server, library, "1", "первая")
    k = key(server)
    assert got(server, k) == "1"
    publish(server, library, "2", "Новый этап про переговоры")
    publish(server, library, "3", "Поправлены письма")
    s = status(server, k)
    assert s["bundle"]["version"] == "1" and got(server, k) == "1"  # stays on its version
    offer = s["update_available"]
    assert offer["version"] == "3"
    assert [c["note"] for c in offer["changes"]] == ["Новый этап про переговоры", "Поправлены письма"]
    assert accept(server, k, "2").status_code == 409  # only the offered version
    after = accept(server, k, "3").json()
    assert after["bundle"]["version"] == "3" and "update_available" not in after
    assert got(server, k) == "3"
    versions = {b["version"]: b["keys"] for b in server.get("/v1/admin/bundles", headers=ADMIN).json()}
    assert versions == {"1": 0, "2": 0, "3": 1}


def test_a_new_key_starts_on_the_newest_version(server, library):
    publish(server, library, "1")
    publish(server, library, "2")
    k = key(server)
    assert k["first"]["bundle"]["version"] == "2" and "update_available" not in k["first"]


def test_an_important_version_installs_itself_and_says_why(server, library):
    publish(server, library, "1")
    k = key(server)
    publish(server, library, "2", "мелочь")
    publish(server, library, "3", "Исправлена ошибка в расчёте зарплаты", important=True)
    s = status(server, k)
    assert s["bundle"]["version"] == "3" and "update_available" not in s
    told = s["auto_updated"]
    assert told["from"] == "1" and told["to"] == "3"
    assert [c["note"] for c in told["changes"]] == ["мелочь", "Исправлена ошибка в расчёте зарплаты"]
    assert got(server, k) == "3"
    assert status(server, k)["auto_updated"]["to"] == "3"  # hrs shows it once; the server keeps it till the next


def test_a_rollback_takes_keys_back(server, library):
    publish(server, library, "1")
    k = key(server)
    publish(server, library, "2")
    accept(server, k, "2").raise_for_status()
    server.post("/v1/admin/bundles/1/current", json={"channel": "stable"}, headers=ADMIN).raise_for_status()
    s = status(server, k)
    assert s["bundle"]["version"] == "1" and "update_available" not in s


def test_test_keys_are_offered_the_test_version_first(server, library):
    publish(server, library, "1")
    tester, everyone = key(server, "test"), key(server)
    publish(server, library, "2", "на проверку", channel="test", back_to="1")
    assert status(server, tester)["update_available"]["version"] == "2"
    assert "update_available" not in status(server, everyone)
    server.post("/v1/admin/bundles/2/current", json={"channel": "stable"}, headers=ADMIN).raise_for_status()
    assert status(server, everyone)["update_available"]["version"] == "2"  # «всем» = available, not installed
    assert got(server, everyone) == "1"


def test_old_hrs_gets_the_channel_version_and_the_key_follows_it(server, library):
    publish(server, library, "1")
    k = key(server, hrs=OLD)
    publish(server, library, "2")
    assert "update_available" not in status(server, k, OLD)
    assert got(server, k, OLD) == "2"  # as before: old hrs cannot ask
    s = status(server, k)  # the person updates hrs: no step back, nothing to offer
    assert s["bundle"]["version"] == "2" and "update_available" not in s


def test_keys_in_use_before_the_update_keep_the_version_they_downloaded(settings, library):
    ks = KeyServer(settings)
    ks.publish(bundle.build(library, version="1")[1], note="первая")
    plain = ks.issue("self")
    t = ks.activate(plain["key"], "pc-1", OLD)["token"]
    ks.bundle(t, OLD)
    unused = ks.issue("self")
    db = sqlite3.connect(settings.data / "keys.sqlite3")
    db.execute("UPDATE keys SET pinned_version = NULL")  # as on the server before this release
    db.commit()
    ks.publish(bundle.build(library, version="2")[1], note="вторая")
    db.execute("UPDATE keys SET pinned_version = NULL")
    db.commit()
    db.close()
    ks = KeyServer(settings)  # restart after the deploy
    assert ks.key(plain["id"])["pinned_version"] == "1"
    assert ks.key(unused["id"])["pinned_version"] is None
    s = ks.status(t, NEW)
    assert s["bundle"]["version"] == "1" and s["update_available"]["version"] == "2"
