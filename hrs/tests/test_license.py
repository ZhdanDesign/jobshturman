"""S4 from the candidate's side: hrs login and sync against the real key server, run in-process.

An expired or revoked key switches the methodology off for the agent, the candidate's data stays.
"""
import asyncio
import json
import re
import zipfile
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from hrs_cli import keystore, license, main
from hrs_cli.server import build
from hrs_keys import bundle
from hrs_keys import cli as keys_cli
from hrs_keys.app import create_app
from hrs_keys.service import Settings, generate_signing_key, iso, now
from hrshturman_core import DomainError
from mcp import Client, MCPError

ADMIN = {"Authorization": "Bearer admin"}
STAGE_00 = ("---\nname: stage-00\ndescription: этап 0\n---\n# Диагностика\n\nСначала выясни, что болит у кандидата сейчас.\n\n"
            "Потом сверь желаемую роль с опытом и рынком.\n\n- Пункт со списком трудностей\n\n"
            "```\nhrs paths\n```\n")
RULES = "# Главные правила\n\nВеди человека без пауз: после ответа сразу следующий шаг.\n"
ZERO_WIDTH = re.compile("[\u200b\u200c\u200d\u2060\u2062\ufeff]")


def plain(text: str) -> str:
    """The text without the watermark and the licence note."""
    text, note = ZERO_WIDTH.sub("", text).rstrip("\n"), license.license_note(license.license_url(license.load())).rstrip("\n")
    assert text.endswith(note)
    return text[:-len(note)]


@pytest.fixture
def keys(tmp_path, monkeypatch):
    monkeypatch.setenv("HRSHTURMAN_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HRSHTURMAN_WORKSPACE", raising=False)
    settings = Settings(data=tmp_path / "server", secret=b"k" * 40, admin_token="admin",
                        signing_key=tmp_path / "server" / "signing.pem", renew_url="https://example.test/renew")
    monkeypatch.setenv("HRS_BUNDLE_PUBKEYS", generate_signing_key(settings.signing_key))
    client = TestClient(create_app(settings))
    lib = tmp_path / "library"
    (lib / "skills" / "stage-07-master-cv" / "references").mkdir(parents=True)
    (lib / "skills" / "stage-07-master-cv" / "SKILL.md").write_text("# 07", encoding="utf-8")
    (lib / "skills" / "stage-07-master-cv" / "references" / "procedure.md").write_text("Мастер-резюме", encoding="utf-8")
    (lib / "skills" / "stage-07-master-cv" / "references" / "acceptance.md").write_text("для нас", encoding="utf-8")
    (lib / "skills" / "stage-00-diagnostic" / "references").mkdir(parents=True)
    (lib / "skills" / "stage-00-diagnostic" / "references" / "procedure.md").write_text(STAGE_00, encoding="utf-8")
    monkeypatch.setenv("HRS_KEYS_DATA", str(settings.data))
    monkeypatch.setenv("HRS_KEYS_SECRET", settings.secret.decode())
    (lib / "agent").mkdir()
    (lib / "agent" / "genesis.md").write_text(RULES, encoding="utf-8")
    (lib / "library_manifest.json").write_text(json.dumps({"version": "0.4.0"}), encoding="utf-8")
    _, data = bundle.build(lib, version="0.4.0.t1")
    client.post("/v1/admin/bundles", files={"file": ("b.zip", data)}, data={"note": "тест"}, headers=ADMIN).raise_for_status()
    return client


def issue(client, **body):
    r = client.post("/v1/admin/keys", json={"tariff_id": "self", **body}, headers=ADMIN)
    r.raise_for_status()
    return r.json()


def stage_text(settings=None):
    async def go():
        async with Client(build(settings)) as c:
            status = json.loads((await c.call_tool("license_status", {})).content[0].text)
            try:
                text = (await c.read_resource("hrshturman://procedure/7")).contents[0].text
            except MCPError as exc:  # the agent gets the reason, not a stack trace
                text = f"error: {exc}"
            return status, text
    return asyncio.run(go())


def test_login_downloads_a_signed_bundle_and_the_agent_reads_one_stage(keys):
    key = issue(keys)
    out = license.login(key["key"], http=keys)
    assert (out["state"], out["methodology"], out["tariff"], out["bundle_version"]) == (
        "active", True, "Самостоятельно", "0.4.0.t1")
    assert license.bundle_file().is_file()
    assert not (license.bundle_file().parent / "skills").exists()  # never unpacked
    assert (license.state_file().stat().st_mode & 0o777) == 0o600
    status, text = stage_text()
    assert status["state"] == "active" and plain(text) == "Мастер-резюме" and text != "Мастер-резюме"


def test_the_cache_on_disk_is_only_ciphertext(keys, tmp_path):
    license.login(issue(keys)["key"], http=keys)
    assert "Мастер-резюме" in plain(license.BundleLibrary().read("skills/stage-07-master-cv/references/procedure.md"))
    files = [p for p in (tmp_path / "home").rglob("*") if p.is_file()]
    assert license.bundle_file() in files
    for path in files:
        raw = path.read_bytes()
        for phrase in ("Мастер-резюме", "Сначала выясни", "stage-07-master-cv", "bundle.json"):
            assert phrase.encode() not in raw, (path, phrase)
        assert not zipfile.is_zipfile(path), path


def test_a_wrong_key_does_not_decrypt_the_cache(keys):
    license.login(issue(keys)["key"], http=keys)
    state = license.load()
    blob = license.bundle_file().read_bytes()
    for wrong in ({**state, "key": "HRS-AAAA-BBBB-CCCC-DDDD-EEEE"}, {**state, "install_id": "another-computer"}):
        with pytest.raises(DomainError) as exc:
            license.decrypt(blob, wrong)
        assert exc.value.code == "bad_bundle"
    license.save({**state, "key": "HRS-AAAA-BBBB-CCCC-DDDD-EEEE"})
    with pytest.raises(DomainError) as exc:
        license.BundleLibrary().read("skills/stage-07-master-cv/references/procedure.md")
    assert exc.value.code == "bad_bundle"


def test_sync_replaces_an_old_open_zip(keys):
    old = license.legacy_bundle_file()
    old.parent.mkdir(parents=True)
    old.write_bytes(b"PK old open bundle")
    license.login(issue(keys)["key"], http=keys)
    assert not old.exists() and license.bundle_file().is_file()


def test_a_copied_paragraph_names_its_key(keys, tmp_path, capsys):
    first, second = issue(keys), issue(keys)
    license.login(second["key"], http=keys)
    text = license.BundleLibrary().read("skills/stage-00-diagnostic/references/procedure.md")
    assert plain(text) == STAGE_00
    assert "hrs paths\n" in text and text.startswith("---\nname: stage-00\n")  # code and front matter stay clean
    body = text[:-len(license.license_note(license.LICENSE_URL))]
    paragraphs = [p for p in body.split("---\n", 2)[2].split("\n\n") if "```" not in p]
    assert len(paragraphs) == 4
    for paragraph in paragraphs:
        copied = tmp_path / "copied.txt"
        copied.write_text(f"Нашёл в сети: {paragraph}", encoding="utf-8")
        capsys.readouterr()
        assert keys_cli.main(["watermark", "decode", str(copied)]) == 0
        found = json.loads(capsys.readouterr().out)
        assert [k["id"] for k in found["keys"]] == [second["id"]] and first["id"] not in json.dumps(found)
    copied.write_text(plain(text), encoding="utf-8")
    assert keys_cli.main(["watermark", "decode", str(copied)]) == 1


def test_the_agent_gets_one_stage_at_a_time(keys, capsys):
    license.login(issue(keys)["key"], http=keys)

    async def go():
        async with Client(build()) as c:
            tools = {t.name for t in (await c.list_tools()).tools}
            zero = (await c.call_tool("stage_procedure", {"stage_id": 0})).content[0].text
            seven = (await c.call_tool("stage_procedure", {"stage_id": 7})).content[0].text
            return tools, zero, seven
    tools, zero, seven = asyncio.run(go())
    assert "stage_procedure" in tools
    assert not {t for t in tools if re.search("bundle|library|methodology_(all|export|read)", t)}
    assert plain(zero) == STAGE_00 and "Мастер-резюме" not in zero
    assert plain(seven) == "Мастер-резюме" and "Сначала выясни" not in seven
    assert main.main(["stage", "0"]) == 0
    out = capsys.readouterr().out
    assert "Сначала выясни" in plain(out) and "Мастер-резюме" not in plain(out) and plain(out) != out


def test_the_agent_reads_the_main_prompt_and_the_route(keys):
    license.login(issue(keys)["key"], http=keys)

    async def go():
        async with Client(build()) as c:
            rules = (await c.call_tool("agent_rules", {})).content[0].text
            route = json.loads((await c.call_tool("methodology_route", {})).content[0].text)
            by_id = (await c.call_tool("stage_procedure", {"stage_id": "master-cv"})).content[0].text
            return rules, route, by_id
    rules, route, by_id = asyncio.run(go())
    assert plain(rules) == RULES and rules != RULES  # watermarked like the stages
    assert [(s["number"], s["id"]) for s in route["stages"]] == [(0, "diagnostic"), (7, "master-cv")]
    assert plain(by_id) == "Мастер-резюме"
    assert license.view().get("hrs_update") is None


def test_expired_key_switches_the_methodology_off_and_renewal_brings_it_back(keys):
    key = issue(keys, expires_at=iso(now() + timedelta(hours=10)))
    out = license.login(key["key"], http=keys)
    assert out["state"] == "expiring" and "https://example.test/renew" in out["warning"]

    keys.post(f"/v1/admin/keys/{key['id']}/extend", json={"until": iso(now() - timedelta(minutes=1))},
              headers=ADMIN).raise_for_status()
    out = license.refresh(force=True, http=keys)
    assert (out["state"], out["methodology"]) == ("expired", False)
    with pytest.raises(DomainError) as exc:
        license.BundleLibrary().read("skills/stage-07-master-cv/references/procedure.md")
    assert exc.value.code == "license_expired" and "https://example.test/renew" in str(exc.value)
    status, text = stage_text()
    assert status["state"] == "expired" and text.startswith("error: license_expired") and "example.test/renew" in text

    keys.post(f"/v1/admin/keys/{key['id']}/extend", json={"months": 3}, headers=ADMIN).raise_for_status()
    out = license.refresh(force=True, http=keys)
    assert (out["state"], out["methodology"]) == ("active", True)


def test_agent_enters_the_key_through_the_mcp_tool(keys, monkeypatch):
    key = issue(keys)
    real_login = license.login
    monkeypatch.setattr(license, "login", lambda k: real_login(k, http=keys))

    async def go():
        async with Client(build()) as c:
            before = json.loads((await c.call_tool("license_status", {})).content[0].text)
            after = json.loads((await c.call_tool("license_login", {"key": key["key"]})).content[0].text)
            text = (await c.read_resource("hrshturman://procedure/7")).contents[0].text
            return before, after, text
    before, after, text = asyncio.run(go())
    assert before["state"] == "none" and after["state"] == "active" and plain(text) == "Мастер-резюме"


def test_revoked_key_cannot_sync(keys):
    key = issue(keys)
    license.login(key["key"], http=keys)
    keys.post(f"/v1/admin/keys/{key['id']}/revoke", json={"reason": "violation"}, headers=ADMIN).raise_for_status()
    with pytest.raises(DomainError) as exc:
        license.sync(http=keys)
    assert exc.value.code == "key_revoked"
    assert license.view()["state"] == "revoked" and license.view()["methodology"] is False


def test_tampered_bundle_is_refused(keys, monkeypatch):
    key = issue(keys)
    license.login(key["key"], http=keys)
    path = license.bundle_file()
    path.write_bytes(path.read_bytes() + b"x")
    with pytest.raises(DomainError) as exc:
        license.BundleLibrary().names()
    assert exc.value.code == "bad_bundle"
    monkeypatch.setenv("HRS_BUNDLE_PUBKEYS", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    with pytest.raises(DomainError) as exc:
        license.sync(http=keys)
    assert exc.value.code == "bad_signature"


def test_offline_grace_then_off(keys):
    key = issue(keys)
    license.login(key["key"], http=keys)
    state = license.load()
    state["checked_at"] = iso(now() - timedelta(days=3))
    license.save(state)
    assert license.view()["methodology"] is True  # 3 days offline is fine
    state["checked_at"] = iso(now() - timedelta(days=8))
    license.save(state)
    assert (license.view()["state"], license.view()["methodology"]) == ("offline", False)


def test_cli_without_a_key(keys, capsys):
    assert main.main(["license"]) == 1
    assert json.loads(capsys.readouterr().out)["state"] == "none"
    assert main.main(["login", "HRS-AAAA-BBBB-CCCC-DDDD-EEEE", "--server", "http://127.0.0.1:9"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] in ("server_error", "connection_error", "invalid_key")


def test_an_old_server_address_moves_to_the_new_one(keys):
    license.login(issue(keys)["key"], http=keys)
    state = json.loads(license.state_file().read_text(encoding="utf-8"))
    license.state_file().write_text(json.dumps(state | {"server": "https://tools.owai.ru/hrshturman/keys"}),
                                    encoding="utf-8")
    assert license.load()["server"] == license.DEFAULT_SERVER == "https://tools.owai.ru/jobshturman/keys"
    assert json.loads(license.state_file().read_text(encoding="utf-8"))["server"] == license.DEFAULT_SERVER


def publish_next(keys, tmp_path, version, note, important=False):
    _, data = bundle.build(tmp_path / "library", version=version)
    keys.post("/v1/admin/bundles", files={"file": ("b.zip", data)},
              data={"note": note, "important": str(important).lower()}, headers=ADMIN).raise_for_status()


def test_a_new_methodology_is_installed_only_after_the_person_agrees(keys, tmp_path, capsys):
    license.login(issue(keys)["key"], http=keys)
    publish_next(keys, tmp_path, "0.4.0.t2", "Новый этап про переговоры")
    out = license.refresh(force=True, http=keys)
    assert out["bundle_version"] == "0.4.0.t1"  # sync does not switch
    offer = out["methodology_update"]
    assert offer["version"] == "0.4.0.t2" and "Новый этап про переговоры" in offer["message"]
    assert "Обновить?" in offer["message"]
    assert license.update(http=keys)["bundle_version"] == "0.4.0.t1"  # showing is not agreeing

    assert "methodology_update" in {t.name for t in asyncio.run(build().list_tools())}
    done = license.update(accept=True, http=keys)
    assert done["bundle_version"] == "0.4.0.t2" and "methodology_update" not in done
    assert license.BundleLibrary().names()
    with pytest.raises(DomainError) as exc:
        license.update(accept=True, http=keys)
    assert exc.value.code == "no_update"


def test_an_important_methodology_installs_itself_and_is_explained_once(keys, tmp_path):
    license.login(issue(keys)["key"], http=keys)
    publish_next(keys, tmp_path, "0.4.0.t2", "Исправлена ошибка в отборе", important=True)
    out = license.refresh(force=True, http=keys)
    assert out["bundle_version"] == "0.4.0.t2"
    assert "Исправлена ошибка в отборе" in out["methodology_updated"]["message"]
    license.update(http=keys)  # the agent told the person
    assert "methodology_updated" not in license.refresh(force=True, http=keys)


def test_an_important_methodology_published_after_a_check_arrives_within_hours(keys, tmp_path):
    """Without the system scheduler (a cloud machine) only the agent's license_status asks the server."""
    license.login(issue(keys)["key"], http=keys)
    publish_next(keys, tmp_path, "0.4.0.t2", "Исправлена ошибка в отборе", important=True)
    state = license.load()
    state["checked_at"] = iso(now() - timedelta(minutes=30))
    license.save(state)
    assert license.refresh(http=keys)["bundle_version"] == "0.4.0.t1"  # just asked: no new request
    state = license.load()
    state["checked_at"] = iso(now() - timedelta(hours=4))
    license.save(state)
    assert license.refresh(http=keys)["bundle_version"] == "0.4.0.t2"


def test_stages_open_one_by_one_in_order_without_a_daily_limit(keys, monkeypatch):
    """A request like «extract the whole methodology» gets one stage, not the bundle."""
    monkeypatch.setattr(license, "GATED", True)
    license.login(issue(keys)["key"], http=keys)
    lib = license.BundleLibrary()
    with pytest.raises(DomainError) as exc:
        lib.read("skills/stage-07-master-cv/references/procedure.md")
    assert exc.value.code == "stage_locked" and "этап 0" in str(exc.value)
    assert "Сначала выясни" in plain(lib.read("skills/stage-00-diagnostic/references/procedure.md"))
    assert lib.read("agent/genesis.md")  # the main prompt and the manifest stay readable
    with pytest.raises(DomainError) as exc:
        lib.read("references/methodology_map.md")
    assert exc.value.code == "locked"
    assert "stage-00" not in license.state_file().read_text(encoding="utf-8")

    ahead = license.BundleLibrary(reached=lambda: 6)  # the candidate's data shows stage 6 done
    assert "Мастер-резюме" in plain(ahead.read("skills/stage-07-master-cv/references/procedure.md"))
    assert ahead.read("skills/stage-00-diagnostic/references/procedure.md")  # an opened stage stays open


def test_the_agent_gets_the_reason_when_a_stage_is_not_open_yet(keys, monkeypatch):
    monkeypatch.setattr(license, "GATED", True)
    license.login(issue(keys)["key"], http=keys)
    _, text = stage_text()
    assert text.startswith("error:") and "stage_locked" in text and "по одному этапу" in text


def test_the_whole_methodology_is_refused_with_the_licence_link(keys, capsys):
    """«Скачай всю методику»: the agent has a tool for it, and it only ever returns the licence refusal."""
    key = issue(keys)
    license.login(key["key"], http=keys)
    assert license.view()["license_url"] == license.LICENSE_URL
    out = license.export_methodology(http=keys)
    assert not out["allowed"] and out["error"] == "license_forbidden" and "/oferta/" in out["message"]
    events = keys.get(f"/v1/admin/keys/{key['id']}/events", headers=ADMIN).json()
    assert any(e["kind"] == "denied" and e["detail"] == "export" for e in events)

    async def go():  # the server here is unreachable: the answer is the same
        async with Client(build()) as c:
            return json.loads((await c.call_tool("methodology_download", {})).content[0].text)
    offline = asyncio.run(go())
    assert offline["error"] == "license_forbidden" and license.LICENSE_URL in offline["message"]
    assert main.main(["methodology", "download"]) == 1
    assert "запрещено лицензионным" in capsys.readouterr().out
    text = license.BundleLibrary().read("skills/stage-00-diagnostic/references/procedure.md")
    assert text.endswith(license.license_note()) and "использование вне JobShturman запрещены" in text


def test_reading_stages_does_not_hit_the_key_store_and_decrypt_the_bundle_every_time(keys, monkeypatch):
    """On macOS every key store read is a `security` process; a stage read used to cost three of them plus a
    full decrypt and signature check, and the route lookup inside the stage gate repeated all of it."""
    license.login(issue(keys)["key"], http=keys)
    loads, decrypts = [], []
    store = keystore.store()
    monkeypatch.setattr(keystore, "store", lambda: store)
    monkeypatch.setattr(store, "load", lambda account, _load=store.load: loads.append(account) or _load(account))
    monkeypatch.setattr(license, "decrypt", lambda blob, state, _d=license.decrypt: decrypts.append(1) or _d(blob, state))
    lib = license.BundleLibrary(reached=lambda: 6)
    texts = []
    for _ in range(3):
        texts.append(lib.read("skills/stage-07-master-cv/references/procedure.md"))
        assert lib.names() and lib.read("agent/genesis.md")
    assert len(decrypts) == 1 and len(loads) <= 4  # the first read checks the key; the next ones cost nothing
    assert all("Мастер-резюме" in plain(t) for t in texts)

    path = license.bundle_file()  # a changed file on disk is read again, and refused when tampered
    path.write_bytes(path.read_bytes() + b"x")
    with pytest.raises(DomainError) as exc:
        lib.names()
    assert exc.value.code == "bad_bundle"


def test_a_proxy_error_page_from_the_key_server_is_a_plain_error(keys):
    import httpx
    html = httpx.Response(502, text="<html>502 Bad Gateway</html>")
    assert license._error(html).code == "server_error" and "502" in str(license._error(html))
    odd = httpx.Response(500, json=["not", "an", "object"])
    assert license._error(odd).code == "server_error"
    proper = httpx.Response(403, json={"error": "key_expired", "message": "Срок ключа закончился"})
    assert license._error(proper).code == "key_expired"
