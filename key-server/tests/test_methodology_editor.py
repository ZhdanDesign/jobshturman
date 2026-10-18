"""The methodology editor: the author edits the draft, every save is a version, the check guards
publishing, test keys get a version first, old hrs keeps a version it can read."""
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from hrs_keys import bundle, library
from hrs_keys.app import create_app
from hrs_keys.service import Settings, generate_signing_key

ADMIN = {"Authorization": "Bearer admin-token"}
AUTHOR = {"Authorization": "Bearer author-token"}
M = "/v1/admin/methodology"
NEW_HRS = {"X-Hrs-Version": "0.3.0"}
OLD_HRS = {"X-Hrs-Version": "0.2.1"}


@pytest.fixture
def server(tmp_path):
    settings = Settings(data=tmp_path / "data", secret=b"s" * 40, admin_token="admin-token",
                        signing_key=tmp_path / "data" / "signing.pem", renew_url="https://example.test/renew",
                        methodologist_token="author-token")
    generate_signing_key(settings.signing_key)
    return TestClient(create_app(settings))


def old_library_zip() -> bytes:
    """A format-1 library as it is today: stage folders, an old manifest, an acceptance file, no main prompt."""
    files = {"Библиотека навыков/library_manifest.json": json.dumps({"version": "0.4.0", "stages": [
        {"stage_id": "01", "entrypoint": "skills/stage-01-candidate-intake/SKILL.md"}],
        "intake_diagnostic": {"entrypoint": "skills/stage-00-intake-diagnostic/SKILL.md"}})}
    for folder in ("stage-00-intake-diagnostic", "stage-01-candidate-intake"):
        base = f"Библиотека навыков/skills/{folder}"
        files[f"{base}/SKILL.md"] = f"---\nname: {folder}\ndescription: этап\n---\n# {folder}\n"
        files[f"{base}/references/procedure.md"] = f"Процедура {folder}"
        files[f"{base}/references/acceptance.md"] = "только для нас"
    files["__MACOSX/._junk"] = "x"
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return out.getvalue()


RULES = "# Главные правила\n\nВеди человека без пауз.\n"


def write_rules(server):
    """The author writes the main prompt over the placeholder, so the draft can be published."""
    opened = server.get(M + "/file", params={"path": "agent/genesis.md"}, headers=AUTHOR).json()
    server.put(M + "/file", json={"path": "agent/genesis.md", "text": RULES, "base_sha": opened["sha"]},
               headers=AUTHOR).raise_for_status()


def upload(server, data=None, headers=AUTHOR):
    r = server.post(M + "/upload", files={"file": ("library.zip", data or old_library_zip(), "application/zip")},
                    headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def key_for(server, channel=None):
    key = server.post("/v1/admin/keys", json={"tariff_id": "self"}, headers=ADMIN).json()
    if channel:
        server.post(f"/v1/admin/keys/{key['id']}/channel", json={"channel": channel}, headers=ADMIN).raise_for_status()
    return key


def bundle_of(server, key, headers=NEW_HRS):
    token = server.post("/v1/activate", json={"key": key["key"], "install_id": "pc-" + key["id"]},
                        headers=headers).json()["token"]
    r = server.get("/v1/bundle/latest", headers={"Authorization": f"Bearer {token}", **headers})
    return r


def test_the_author_sees_only_the_methodology(server):
    assert server.get("/v1/admin/whoami", headers=AUTHOR).json() == {"role": "methodologist"}
    assert server.get("/v1/admin/whoami", headers=ADMIN).json() == {"role": "admin"}
    assert server.get("/v1/admin/keys", headers=AUTHOR).status_code == 403
    assert server.get("/v1/admin/stats", headers=AUTHOR).status_code == 403
    assert server.get(M, headers=AUTHOR).status_code == 200
    assert server.get(M, headers={"Authorization": "Bearer wrong"}).status_code == 403


def test_an_old_library_enters_the_draft_with_its_route_written_out_and_a_main_prompt(server):
    head = upload(server)
    assert head["id"] == 1 and head["by"] == "methodologist"
    draft = server.get(M, headers=AUTHOR).json()
    paths = [f["path"] for f in draft["files"]]
    assert "agent/genesis.md" in paths and not any(p.startswith(("__MACOSX", "Библиотека")) for p in paths)
    assert [s["id"] for s in draft["stages"]] == ["intake-diagnostic", "candidate-intake"]
    assert [s["number"] for s in draft["route"]["stages"]] == [0, 1]
    manifest = json.loads(server.get(M + "/file", params={"path": "library_manifest.json"}, headers=AUTHOR).json()["text"])
    assert manifest["format"] == 2 and manifest["version"] == "0.4.0"
    rules = server.get(M + "/file", params={"path": "agent/genesis.md"}, headers=AUTHOR).json()["text"]
    assert library.is_placeholder(rules)  # the server keeps no copy of the author's rules
    assert {e["code"] for e in draft["check"]["errors"]} == {"genesis_placeholder"}
    refused = server.post(M + "/publish", json={"channel": "test", "note": "правка"}, headers=AUTHOR)
    assert refused.status_code == 422 and refused.json()["error"] == "check_failed"


def test_a_library_without_rules_takes_them_from_the_published_version(server):
    upload(server)
    write_rules(server)
    server.post(M + "/publish", json={"channel": "stable", "note": "правка"}, headers=AUTHOR).raise_for_status()
    upload(server)  # the same old library again, still without agent/genesis.md
    rules = server.get(M + "/file", params={"path": "agent/genesis.md"}, headers=AUTHOR).json()["text"]
    assert rules == RULES


def test_every_save_is_a_version_and_a_stale_editor_does_not_overwrite(server):
    upload(server)
    opened = server.get(M + "/file", params={"path": "agent/genesis.md"}, headers=AUTHOR).json()
    saved = server.put(M + "/file", json={"path": "agent/genesis.md", "text": "# Правила v2\n",
                                         "base_sha": opened["sha"]}, headers=AUTHOR)
    assert saved.status_code == 200 and saved.json()["id"] == 2
    stale = server.put(M + "/file", json={"path": "agent/genesis.md", "text": "# чужая правка\n",
                                         "base_sha": opened["sha"]}, headers=ADMIN)
    assert stale.status_code == 409 and stale.json()["error"] == "conflict"
    diff = server.get(M + "/revisions/2/diff", headers=AUTHOR).json()
    assert diff == [{"path": "agent/genesis.md", "change": "changed", "before": opened["text"], "after": "# Правила v2\n"}]
    assert server.post(M + "/revisions/1/restore", headers=AUTHOR).json()["id"] == 3
    back = server.get(M + "/file", params={"path": "agent/genesis.md"}, headers=AUTHOR).json()
    assert back["text"] == opened["text"]
    assert [r["id"] for r in server.get(M + "/revisions", headers=AUTHOR).json()] == [3, 2, 1]
    assert server.put(M + "/file", json={"path": "../etc/passwd", "text": "x"}, headers=AUTHOR).status_code == 400


def test_the_author_adds_reorders_and_switches_off_stages(server):
    upload(server)
    write_rules(server)
    draft = server.get(M, headers=AUTHOR).json()
    stages = [{k: v for k, v in s.items() if k not in ("files", "position")} for s in draft["stages"]]
    steps = [{"id": "about", "title": "О вас"}, {"id": "apply", "title": "Отклики"}]
    new = {"id": "salary-talk", "title": "Разговор о деньгах", "step": "apply"}
    stages = [new, stages[1] | {"step": "about"}, stages[0] | {"step": "about", "status": "off"}]
    r = server.put(M + "/route", json={"steps": steps, "stages": stages, "version": "0.5.0"}, headers=AUTHOR)
    assert r.status_code == 200, r.text
    draft = server.get(M, headers=AUTHOR).json()
    assert [(s["number"], s["id"]) for s in draft["route"]["stages"]] == [(0, "salary-talk"), (1, "candidate-intake")]
    assert {"skills/salary-talk/SKILL.md", "skills/salary-talk/references/procedure.md"} <= \
        {f["path"] for f in draft["files"]}
    assert draft["check"]["ok"] and draft["manifest_version"] == "0.5.0"
    bad = server.put(M + "/route", json={"steps": steps, "stages": [new, new | {"step": "nowhere"}]}, headers=AUTHOR)
    assert bad.status_code == 200  # saved as a draft…
    assert {e["code"] for e in server.post(M + "/check", headers=AUTHOR).json()["errors"]} == \
        {"duplicate_id", "unknown_step"}
    refused = server.post(M + "/publish", json={"channel": "test", "note": "правка"}, headers=AUTHOR)
    assert refused.status_code == 422 and refused.json()["error"] == "check_failed"  # …but never published


def test_test_keys_get_a_version_first_then_everyone_and_a_rollback_is_one_call(server):
    upload(server)
    write_rules(server)
    first = server.post(M + "/publish", json={"channel": "stable", "note": "первая"}, headers=AUTHOR)
    assert first.status_code == 201, first.text
    v1 = first.json()["version"]
    assert first.json()["min_hrs"] is None  # stages still in their old folders: old hrs reads it too
    opened = server.get(M + "/file", params={"path": "agent/genesis.md"}, headers=AUTHOR).json()
    server.put(M + "/file", json={"path": "agent/genesis.md", "text": "# Новые правила\n", "base_sha": opened["sha"]},
               headers=AUTHOR).raise_for_status()
    second = server.post(M + "/publish", json={"channel": "test", "note": "правка"}, headers=AUTHOR).json()
    v2 = second["version"]
    assert v2 != v1 and second["channels"] == ["test"]
    tester, everyone = key_for(server, "test"), key_for(server)
    assert bundle_of(server, tester).headers["X-Bundle-Version"] == v2
    assert bundle_of(server, everyone).headers["X-Bundle-Version"] == v1
    server.post(f"/v1/admin/bundles/{v2}/current", json={"channel": "stable"}, headers=AUTHOR).raise_for_status()
    assert bundle_of(server, everyone).headers["X-Bundle-Version"] == v2
    server.post(f"/v1/admin/bundles/{v1}/current", json={"channel": "stable"}, headers=AUTHOR).raise_for_status()
    assert bundle_of(server, everyone).headers["X-Bundle-Version"] == v1  # rolled back
    with zipfile.ZipFile(io.BytesIO(bundle_of(server, everyone).content)) as z:
        assert "agent/genesis.md" in z.namelist()
        assert not [n for n in z.namelist() if n.endswith("acceptance.md")]


def test_old_hrs_keeps_the_last_version_it_can_read(server):
    upload(server)
    write_rules(server)
    old = server.post(M + "/publish", json={"channel": "stable", "note": "правка"}, headers=AUTHOR).json()["version"]
    draft = server.get(M, headers=AUTHOR).json()
    stages = [{k: v for k, v in s.items() if k not in ("files", "position")} for s in draft["stages"]]
    server.put(M + "/route", json={"steps": draft["steps"], "stages": [stages[1], stages[0]]},
               headers=AUTHOR).raise_for_status()  # reordered: stage 0 is no longer in stage-00-…
    new = server.post(M + "/publish", json={"channel": "stable", "note": "правка"}, headers=AUTHOR).json()
    assert new["min_hrs"] == bundle.ROUTE_HRS and new["format"] == 2
    key = key_for(server)
    assert bundle_of(server, key, NEW_HRS).headers["X-Bundle-Version"] == new["version"]
    old_key = key_for(server)
    assert bundle_of(server, old_key, OLD_HRS).headers["X-Bundle-Version"] == old
    token = server.post("/v1/activate", json={"key": old_key["key"], "install_id": "pc-x"}).json()["token"]
    status = server.get("/v1/status", headers={"Authorization": f"Bearer {token}"}).json()  # no header: oldest hrs
    assert status["bundle"]["version"] == old and "install.sh" in status["hrs_update"]


def test_import_the_published_version_and_export_the_draft(server):
    assert server.post(M + "/import-current", headers=AUTHOR).status_code == 404
    _, data = bundle.build_files({"library_manifest.json": b'{"version": "0.4.0"}',
                                  "skills/stage-00-intake-diagnostic/SKILL.md": b"# 00\n"}, version="0.4.0.p")
    server.post("/v1/admin/bundles", files={"file": ("b.zip", data)}, data={"note": "архив"}, headers=ADMIN).raise_for_status()
    head = server.post(M + "/import-current", headers=AUTHOR).json()
    assert head["message"] == "Взята опубликованная версия 0.4.0.p"
    with zipfile.ZipFile(io.BytesIO(server.get(M + "/export", headers=AUTHOR).content)) as z:
        assert {"library_manifest.json", "agent/genesis.md", "skills/stage-00-intake-diagnostic/SKILL.md"} == \
            set(z.namelist())
