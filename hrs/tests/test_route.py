"""The whole route through the MCP tools of `hrs serve`, as an agent would call them, without a model.

A synthetic candidate goes from the key to an employer's invitation: key, stage 0 questionnaire,
document, facts, CV and its files, action plan, three vacancies (a match, a conflict, missing data),
limits, score, decision, letter check, package, permission, sending, employer reply, funnel and the
route screen; then the key expires and the methodology switches off while the candidate's work stays.
It catches what unit tests do not: a state that does not move on, a tool that does not fit the next one.
Run before a release: `uv run --with pytest --with respx pytest tests/test_route.py`.
"""
import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from hrs_keys import bundle
from hrs_keys.app import create_app
from hrs_keys.service import Settings, generate_signing_key, iso, now
from mcp import Client
from test_hrs import resume_docx
from test_license import ADMIN, issue, plain
from test_selftest import CHANNELS, LIBRARY

from hrs_cli import license
from hrs_cli.server import build

pytestmark = pytest.mark.skipif(not LIBRARY.is_dir(), reason="нет библиотеки методики рядом с кодом")
POLICY = {"salary_min": 300000, "formats": ["REMOTE", "HYBRID"], "locations": ["Москва"]}
VACANCIES = [
    {"source": "hh", "external_id": "match", "title": "Коммерческий директор", "company": "Бета",
     "location": "Москва", "work_format": ["REMOTE"], "text": "Полное описание: рост продаж B2B.",
     "salary": {"min": 350000, "max": 450000, "currency": "RUR", "gross": False, "period": None}},
    {"source": "hh", "external_id": "conflict", "title": "Директор по продажам", "company": "Гамма",
     "location": "Новосибирск", "work_format": ["ON_SITE"], "text": "Офис, переезд обязателен.",
     "salary": {"min": 150000, "max": 200000, "currency": "RUR", "gross": False, "period": None}},
    {"source": "telegram", "external_id": "chan:7", "title": "CCO", "location": None, "salary": None},
]


@pytest.fixture
def keys(tmp_path, monkeypatch):
    """A local key server with the real library published; hrs in a throwaway home and keychain."""
    monkeypatch.setenv("HRSHTURMAN_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HRSHTURMAN_WORKSPACE", raising=False)
    settings = Settings(data=tmp_path / "server", secret=b"r" * 40, admin_token="admin",
                        signing_key=tmp_path / "server" / "signing.pem", renew_url="https://example.test/renew")
    monkeypatch.setenv("HRS_BUNDLE_PUBKEYS", generate_signing_key(settings.signing_key))
    client = TestClient(create_app(settings))
    _, data = bundle.build(LIBRARY, {"telegram_channels.csv": CHANNELS}, version="route.1")
    client.post("/v1/admin/bundles", files={"file": ("b.zip", data)}, headers=ADMIN).raise_for_status()
    if sys.platform == "darwin":
        chain = tmp_path / "route.keychain-db"
        subprocess.run(["security", "create-keychain", "-p", "route", str(chain)], check=True)
        monkeypatch.delenv("HRS_KEYSTORE")
        monkeypatch.setenv("HRS_KEYCHAIN", str(chain))
        yield client
        subprocess.run(["security", "delete-keychain", str(chain)], check=False)
    else:
        yield client


def test_the_whole_route(keys, tmp_path, monkeypatch):
    key = issue(keys)
    real_login = license.login
    monkeypatch.setattr(license, "login", lambda k: real_login(k, http=keys))
    real_refresh = license.refresh
    monkeypatch.setattr(license, "refresh", lambda force=False, http=None: real_refresh(force, http or keys))
    cv_file = resume_docx(tmp_path / "cv.docx")

    async def go():
        async with Client(build()) as c:
            async def call(name, **args):
                result = await c.call_tool(name, args)
                text = result.content[0].text if result.content else ""
                assert not result.is_error, f"{name}: {text}"
                data = result.structured_content
                if data is not None:  # a list comes back as {"result": [...]}
                    return data["result"] if set(data) == {"result"} else data
                return json.loads(text) if text[:1] in "[{" else text

            seen = {}
            assert (await call("license_status"))["state"] == "none"
            assert (await call("license_login", key=key["key"]))["state"] == "active"
            await call("ui_mode", mode="builtin_browser")
            seen["stages"] = [await call("stage_procedure", stage_id=n) for n in range(26)]

            # stage 0: four blocks, the request map, confirmation
            await call("intake_answer", block="pains", answer="1, 5")
            await call("intake_answer", block="situation_goal", answer={"target_roles": "Коммерческий директор"})
            await call("intake_answer", block="situation_search", answer={"search_history": "Три месяца на hh"})
            form = await call("intake_answer", block="situation_limits",
                              answer={"constraints": "Москва или удалённо, от 300 000"})
            await call("intake_confirm", expected_version=form["saved_version"])

            # stages 03, 07, 11: document, facts, CV and its files
            doc = await call("document_import", path=str(cv_file))
            fact = await call("fact_add", assertion="Рост выручки на 25% в 2023 году", employer="Альфа",
                              period={"start": "2021", "end": "2024"},
                              source_refs=[{"source_id": doc["source_id"], "locator": "абзац 2"}])
            fact = await call("fact_set_status", fact_id=fact["object_id"], status="confirmed",
                              expected_version=fact["version"])
            cv = {"track_id": "main", "language": "ru", "headline": "Коммерческий директор",
                  "experience": [{"employer": "Альфа", "title": "CCO", "start": "2021", "end": "2024",
                                  "bullets": [{"text": "Рост выручки на 25% в 2023 году",
                                               "fact_ids": [fact["object_id"]]}]}]}
            saved = await call("resume_save", content=cv)
            assert saved["validation"]["ok"], saved["validation"]
            await call("resume_approve", expected_version=saved["version"])
            seen["render"] = await call("resume_render")

            # stage 06: the action plan
            seen["plan"] = await call("plan_check", tasks=[
                {"id": "cv", "title": "Резюме", "owner": "кандидат", "criterion": "утверждено",
                 "start": "2026-10-01", "due": "2026-10-05"},
                {"id": "apply", "title": "Отклики", "owner": "агент", "criterion": "10 откликов",
                 "start": "2026-10-06", "due": "2026-10-20", "depends_on": ["cv"]}])

            # stages 14-16: vacancies, limits, score, decision
            await call("vacancies_import", items=VACANCIES)
            cards = {a["vacancy"]["external_id"]: a for a in await call("applications_list")}
            seen["limits"] = {k: await call("vacancy_constraints", application_id=a["object_id"], policy=POLICY)
                              for k, a in cards.items()}
            match = cards["match"]
            match = await call("application_score", application_id=match["object_id"],
                               expected_version=match["version"], preliminary=5, final=5,
                               why="Опыт роста выручки B2B", fact_ids=[fact["object_id"]])
            match = await call("application_decide", application_id=match["object_id"],
                               expected_version=match["version"], decision="approve", reason="подходит")
            bad = cards["conflict"]
            bad = await call("application_score", application_id=bad["object_id"], expected_version=bad["version"],
                             preliminary=2, why="Офис в другом городе", fact_ids=[])
            await call("application_decide", application_id=bad["object_id"], expected_version=bad["version"],
                       decision="reject", reason="формат и город")
            thin = cards["chan:7"]
            seen["no_final_without_text"] = (await c.call_tool("application_score", {
                "application_id": thin["object_id"], "expected_version": thin["version"], "preliminary": 3,
                "final": 4, "why": "мало данных", "fact_ids": []})).is_error

            # stages 17-18: letter, package, permission, sending
            letter = "Добрый день! Вырастил выручку на 40% в [компания]."
            seen["letter_bad"] = await call("text_check", text=letter, fact_ids=[fact["object_id"]])
            letter = "Добрый день! Вырастил выручку на 25% в 2023 году."
            seen["letter_ok"] = await call("text_check", text=letter, fact_ids=[fact["object_id"]])
            match = await call("application_prepare", application_id=match["object_id"],
                               expected_version=match["version"], material={"cv": "v1", "letter": letter},
                               channel="hh-main")
            expires = (datetime.now(UTC) + timedelta(days=3)).isoformat()
            match = await call("application_authorize", application_id=match["object_id"],
                               expected_version=match["version"], material_hash=match["material"]["hash"],
                               channel="hh-main", expires_at=expires)
            match = await call("application_record_sent", application_id=match["object_id"],
                               expected_version=match["version"], proof_ref="hh-otklik-101")

            # stage 22: the employer answers
            match = await call("application_record_employer_event", application_id=match["object_id"],
                               expected_version=match["version"], kind="invited",
                               occurred_at=datetime.now(UTC).isoformat(), source="чат hh")
            seen["status"] = await call("status_report")
            seen["screen"] = await call("ui_state")
            seen["sent"] = match

            # the key ends: methodology off, the candidate's work stays
            keys.post(f"/v1/admin/keys/{key['id']}/extend", json={"until": iso(now() - timedelta(minutes=1))},
                      headers=ADMIN).raise_for_status()
            seen["keystore"] = (await call("license_status"))["key_storage"]
            seen["expired"] = await call("license_status", refresh=True)
            seen["stage_after"] = (await c.call_tool("stage_procedure", {"stage_id": 0})).content[0].text
            seen["cards_after"] = await call("applications_list")
            return seen

    seen = asyncio.run(go())

    assert len(seen["stages"]) == 26 and all(plain(t) != t and len(plain(t)) > 500 for t in seen["stages"])
    assert len({plain(t) for t in seen["stages"]}) == 26  # 26 different stages, none empty or doubled
    assert seen["keystore"] == ("keychain" if sys.platform == "darwin" else "file")
    assert seen["render"]["text_extraction"]["pdf"]["ok"]
    assert seen["plan"]["ok"]
    assert seen["limits"]["match"]["hard_conflicts"] == []
    assert seen["limits"]["conflict"]["hard_conflicts"] == ["salary", "format", "location"]
    assert seen["limits"]["chan:7"]["unknown"] == ["salary", "format", "location"]
    assert seen["no_final_without_text"] is True
    assert not seen["letter_bad"]["ok"] and seen["letter_ok"]["ok"]
    assert seen["sent"]["evidence"]["level"] == "self_reported"
    assert seen["status"]["funnel"]["counts"]["sent_self_reported"] == 1
    stages = {s["stage"]: s["status"] for s in seen["screen"]["progress"]}
    assert (stages[0], stages[7], stages[16], stages[18], stages[22]) == ("done", "done", "active", "active", "active")
    assert seen["expired"]["state"] == "expired" and not seen["expired"]["methodology"]
    assert "license_expired" in seen["stage_after"]
    assert len(seen["cards_after"]) == 3
