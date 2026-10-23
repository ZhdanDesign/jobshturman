"""End-to-end: a real MCP client talks to the server over stdio on a synthetic workspace."""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hrshturman_core import report
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[2]
LIBRARY = ROOT / "MetaProject_Job_Search_—_Specifications_and_Library" / "ФИНАЛЬНОЕ" / "MD библиотека" / "Библиотека навыков"


def payload(result):
    if result.is_error:
        raise AssertionError(result.content[0].text)
    if result.structured_content is not None:
        data = result.structured_content
        return data["result"] if set(data) == {"result"} else data
    return json.loads(result.content[0].text)


class McpServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        env = dict(os.environ, HRSHTURMAN_WORKSPACE=self.tmp.name, HRSHTURMAN_CANDIDATE="synthetic-1",
                   PYTHONPATH=os.pathsep.join([str(ROOT / "core"), str(ROOT / "local-app")]))
        self.params = StdioServerParameters(command=sys.executable, args=["-m", "hrshturman_local.mcp_server"], env=env)

    def tearDown(self):
        self.tmp.cleanup()

    def run_session(self, scenario):
        async def go():
            async with stdio_client(self.params) as (read, write), ClientSession(read, write) as session:
                    await session.initialize()
                    return await scenario(session)
        return asyncio.run(go())

    def test_full_local_route(self):
        async def scenario(s):
            call = lambda name, **kw: s.call_tool(name, kw)
            tools = {t.name for t in (await s.list_tools()).tools}
            self.assertTrue({"status_report", "intake_save_draft", "resume_save", "application_record_sent"} <= tools)

            draft = payload(await call("intake_save_draft", pain_ids=[3, 5], situation={"target_roles": "CCO"}))
            self.assertEqual(draft["request_map"]["pains"][0]["pain_id"], 3)
            self.assertEqual(payload(await call("intake_confirm", expected_version=draft["version"]))["status"], "confirmed")

            fact = payload(await call("fact_add", assertion="Рост выручки на 25%", employer="Альфа",
                                      source_refs=[{"source_id": "cv-old", "locator": "p.1"}]))
            payload(await call("fact_set_status", fact_id=fact["object_id"], status="confirmed",
                               expected_version=fact["version"]))
            cv = {"track_id": "main", "language": "ru", "headline": "Коммерческий директор",
                  "experience": [{"employer": "Альфа", "title": "CCO", "start": "2021", "end": "2024",
                                  "bullets": [{"text": "Рост выручки на 25%", "fact_ids": [fact["object_id"]]}]}]}
            saved = payload(await call("resume_save", content=cv))
            self.assertTrue(saved["validation"]["ok"], saved["validation"])
            payload(await call("resume_approve", expected_version=saved["version"]))
            rendered = payload(await call("resume_render"))
            self.assertTrue(rendered["text_extraction"]["pdf"]["ok"], rendered["text_extraction"])

            payload(await call("vacancies_import", items=[{"source": "hh", "external_id": "1", "title": "CCO",
                                                           "text": "Полный текст"}]))
            app = payload(await call("applications_list"))[0]
            app = payload(await call("application_score", application_id=app["object_id"],
                                     expected_version=app["version"], preliminary=4, final=4,
                                     why="Опыт совпадает", fact_ids=[fact["object_id"]]))
            app = payload(await call("application_decide", application_id=app["object_id"],
                                     expected_version=app["version"], decision="approve", reason="интересно"))
            app = payload(await call("application_prepare", application_id=app["object_id"],
                                     expected_version=app["version"], material={"cv": "v1"}, channel="hh-main"))
            expires = (datetime.now(UTC) + timedelta(days=3)).isoformat()
            app = payload(await call("application_authorize", application_id=app["object_id"],
                                     expected_version=app["version"], material_hash=app["material"]["hash"],
                                     channel="hh-main", expires_at=expires))
            app = payload(await call("application_record_sent", application_id=app["object_id"],
                                     expected_version=app["version"], proof_ref="hh-response-777"))
            self.assertEqual(app["evidence"]["level"], "self_reported")

            # The agent applies through the browser: attempt first, then the platform's proof.
            payload(await call("vacancies_import", items=[{"source": "hh", "external_id": "2", "title": "COO",
                                                           "text": "Полный текст"}]))
            other = next(a for a in payload(await call("applications_list")) if a["vacancy"]["external_id"] == "2")
            for name, kw in (("application_score", {"preliminary": 5, "final": 5, "why": "Совпадает",
                                                    "fact_ids": [fact["object_id"]]}),
                             ("application_decide", {"decision": "approve", "reason": "подходит"}),
                             ("application_prepare", {"material": {"cv": "v2"}, "channel": "hh-main"})):
                other = payload(await call(name, application_id=other["object_id"], expected_version=other["version"], **kw))
            other = payload(await call("application_authorize", application_id=other["object_id"],
                                       expected_version=other["version"], material_hash=other["material"]["hash"],
                                       channel="hh-main", expires_at=expires))
            other = payload(await call("application_send_start", application_id=other["object_id"],
                                       expected_version=other["version"]))
            self.assertEqual(other["state"], "sending")
            again = await call("application_send_start", application_id=other["object_id"], expected_version=other["version"])
            self.assertTrue(again.is_error)
            missing = await call("application_send_result", application_id=other["object_id"],
                                 expected_version=other["version"], sent=True, proof_ref="Вы откликнулись")
            self.assertTrue(missing.is_error)  # a sent application needs the platform's id
            other = payload(await call("application_send_result", application_id=other["object_id"],
                                       expected_version=other["version"], sent=True, proof_ref="Вы откликнулись",
                                       platform_id="https://hh.ru/applicant/negotiations/item?topicId=1"))
            self.assertEqual((other["state"], other["evidence"]["level"]), ("sent_confirmed", "caller_supplied"))

            status = payload(await call("status_report"))
            self.assertEqual(status["funnel"]["counts"]["sent_self_reported"], 1)
            self.assertEqual(status["funnel"]["counts"]["sent_confirmed"], 2)
            self.assertIn(report.MANUAL_SEND_NOTE, status["limitations"])
            self.assertEqual(status["resume"], [{"track_id": "main", "status": "approved"}])

            stale = await call("application_revoke", application_id=app["object_id"], expected_version=1)
            self.assertTrue(stale.is_error)
            self.assertIn("state_version_conflict", stale.content[0].text)

            if LIBRARY.is_dir():  # the methodology is in the private repository
                text = (await s.read_resource("hrshturman://procedure/7")).contents[0].text
                self.assertIn("Мастер-резюме", text)
        self.run_session(scenario)

    def test_checks_run_as_tools(self):  # S5: stage 06, 09-19 and 15 checks are code, not prose
        async def scenario(s):
            call = lambda name, **kw: s.call_tool(name, kw)
            payload(await call("vacancies_import", items=[{
                "source": "hh", "external_id": "1", "title": "CCO", "location": "Казань", "work_format": ["ON_SITE"],
                "salary": {"min": 150000, "max": 200000, "currency": "RUR", "gross": True, "period": None}}]))
            app = payload(await call("applications_list"))[0]
            limits = payload(await call("vacancy_constraints", application_id=app["object_id"],
                                        policy={"salary_min": 400000, "formats": ["REMOTE"], "locations": ["Москва"]}))
            fact = payload(await call("fact_add", assertion="Рост выручки на 25%", employer="Альфа",
                                      source_refs=[{"source_id": "cv", "locator": "p1"}]))
            letter = payload(await call("text_check", text="Рост выручки на 30% в [компания]",
                                        fact_ids=[fact["object_id"]]))
            plan = payload(await call("plan_check", tasks=[{"id": "a", "title": "CV", "owner": "кандидат",
                                                             "criterion": "утверждено", "due": "2026-10-20",
                                                             "depends_on": ["b"]}]))
            return limits, letter, plan
        limits, letter, plan = self.run_session(scenario)
        self.assertEqual(limits["hard_conflicts"], ["salary", "format", "location"])
        self.assertEqual(sorted(e["code"] for e in letter["errors"]), ["invalid_material", "missing_fact"])
        self.assertEqual([e["code"] for e in plan["errors"]], ["unknown_dependency"])

    def test_screen_and_ranked_list(self):  # every vacancy shown to the person carries a 1-5 rank
        async def scenario(s):
            call = lambda name, **kw: s.call_tool(name, kw)
            payload(await call("vacancies_import", items=[
                {"source": "hh", "external_id": str(i), "title": t, "text": "описание", "salary": sal}
                for i, (t, sal) in enumerate([
                    ("CFO", None),
                    ("Финдиректор", {"min": 900000, "max": None, "currency": "RUR", "period": None}),
                    ("Бухгалтер", {"min": 90000, "max": 100000, "currency": "RUR", "period": None}),
                    ("Директор по финансам", {"min": 1300000, "max": None, "currency": "RUR", "period": None})])]))
            ids = {a["vacancy"]["title"]: a for a in payload(await call("applications_list"))}
            screen = payload(await call("vacancies_screen", policy={"salary_min": 1200000}))
            for title, rank in (("CFO", 3), ("Финдиректор", 5)):
                a = ids[title]
                payload(await call("application_score", application_id=a["object_id"], expected_version=a["version"],
                                   preliminary=rank, why="по профилю", fact_ids=[]))
            return ids, screen, payload(await call("vacancies_ranked"))
        ids, screen, ranked = self.run_session(scenario)
        self.assertEqual(screen["level"], "moderate")
        self.assertEqual([d["id"] for d in screen["dropped"]], [ids["Бухгалтер"]["object_id"]])
        self.assertEqual([r["title"] for r in ranked["ranked"]], ["Финдиректор", "CFO"])
        self.assertEqual([r["rank"] for r in ranked["ranked"]], [5, 3])
        self.assertEqual({u["title"] for u in ranked["unscored"]}, {"Бухгалтер", "Директор по финансам"})

    def test_browser_sends_stop_at_daily_limit(self):
        self.params.env["HRS_SEND_PAUSE"] = "0"

        async def scenario(s):
            call = lambda name, **kw: s.call_tool(name, kw)
            fact = payload(await call("fact_add", assertion="Рост выручки на 25%", employer="Альфа",
                                      source_refs=[{"source_id": "cv", "locator": "p1"}]))
            payload(await call("vacancies_import", items=[{"source": "hh", "external_id": str(i), "title": "CCO",
                                                           "text": "Полный текст"} for i in range(41)]))
            expires = (datetime.now(UTC) + timedelta(days=3)).isoformat()
            results = []
            for app in payload(await call("applications_list")):
                for name, kw in (("application_score", {"preliminary": 4, "final": 4, "why": "Совпадает",
                                                        "fact_ids": [fact["object_id"]]}),
                                 ("application_decide", {"decision": "approve", "reason": "подходит"}),
                                 ("application_prepare", {"material": {"cv": "v1"}, "channel": "hh-main"})):
                    app = payload(await call(name, application_id=app["object_id"], expected_version=app["version"], **kw))
                app = payload(await call("application_authorize", application_id=app["object_id"],
                                         expected_version=app["version"], material_hash=app["material"]["hash"],
                                         channel="hh-main", expires_at=expires))
                results.append(await call("application_send_start", application_id=app["object_id"],
                                          expected_version=app["version"]))
            return results
        results = self.run_session(scenario)
        self.assertFalse(any(r.is_error for r in results[:40]))
        self.assertTrue(results[40].is_error)
        self.assertIn("rate_limited", results[40].content[0].text)

    def test_automatic_mode_takes_only_high_ranks_and_keeps_a_pause(self):
        async def scenario(s):
            call = lambda name, **kw: s.call_tool(name, kw)
            fact = payload(await call("fact_add", assertion="Рост выручки на 25%", employer="Альфа",
                                      source_refs=[{"source_id": "cv", "locator": "p1"}]))
            payload(await call("vacancies_import", items=[{"source": "hh", "external_id": str(i), "title": t,
                                                           "text": "Полный текст"}
                                                          for i, t in enumerate(["CCO", "COO", "CFO"])]))
            cards = {a["vacancy"]["title"]: a for a in payload(await call("applications_list"))}
            for title, rank in (("CCO", 5), ("COO", 4), ("CFO", 3)):
                a = cards[title]
                cards[title] = payload(await call("application_score", application_id=a["object_id"],
                                                  expected_version=a["version"], preliminary=rank, final=rank,
                                                  why="по профилю", fact_ids=[fact["object_id"]]))
            out = {"off": await call("auto_apply_approve", application_id=cards["CCO"]["object_id"],
                                     expected_version=cards["CCO"]["version"])}
            out["status"] = payload(await call("auto_apply_set", enabled=True))
            out["low"] = await call("auto_apply_approve", application_id=cards["CFO"]["object_id"],
                                    expected_version=cards["CFO"]["version"])
            started = []
            for title in ("CCO", "COO"):
                a = payload(await call("auto_apply_approve", application_id=cards[title]["object_id"],
                                       expected_version=cards[title]["version"]))
                a = payload(await call("application_prepare", application_id=a["object_id"],
                                       expected_version=a["version"], material={"cv": title}, channel="hh-main"))
                a = payload(await call("auto_apply_authorize", application_id=a["object_id"], expected_version=a["version"]))
                started.append(await call("application_send_start", application_id=a["object_id"],
                                          expected_version=a["version"]))
            out["started"] = started
            out["after"] = payload(await call("auto_apply_status"))
            return out
        out = self.run_session(scenario)
        self.assertIn("permission_required", out["off"].content[0].text)
        self.assertEqual([q["title"] for q in out["status"]["queue"]], ["CCO", "COO"])
        self.assertEqual(out["status"]["left_today"], 40)
        self.assertIn("permission_required", out["low"].content[0].text)
        first, second = out["started"]
        self.assertEqual(payload(first)["authority"]["by"], "auto_policy")
        self.assertIn("rate_limited", second.content[0].text)  # a minute between two applications
        self.assertEqual(out["after"]["left_today"], 39)

    def test_state_survives_restart(self):  # СЦ-10: a new AI session gets the stored state
        async def first(s):
            payload(await s.call_tool("intake_save_draft", {"pain_ids": [1]}))

        async def second(s):
            return payload(await s.call_tool("intake_get", {}))
        self.run_session(first)
        self.assertEqual(self.run_session(second)["intake"]["pain_ids"], [1])


if __name__ == "__main__":
    unittest.main()
