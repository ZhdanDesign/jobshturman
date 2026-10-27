"""Answers stay small when the base is big: every answer goes into the agent's context and is read again on
every following turn, so a list of the whole base costs the person's model limit many times over."""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_mcp_server import payload

ROOT = Path(__file__).resolve().parents[2]
N = 1500
TEXT = "Руководить продажами B2B, команда 15 человек, P&L, переговоры с C-level. " * 20


class TokenBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        env = dict(os.environ, HRSHTURMAN_WORKSPACE=self.tmp.name, HRSHTURMAN_CANDIDATE="synthetic-1",
                   PYTHONPATH=os.pathsep.join([str(ROOT / "core"), str(ROOT / "local-app")]))
        self.params = StdioServerParameters(command=sys.executable, args=["-m", "hrshturman_local.mcp_server"], env=env)

    def tearDown(self):
        self.tmp.cleanup()

    def test_big_base_gives_short_answers(self):
        async def scenario(s):
            call = lambda name, **kw: s.call_tool(name, kw)
            size = lambda result: sum(len(getattr(b, "text", "")) for b in result.content)
            payload(await call("vacancies_import", items=[
                {"source": "hh", "external_id": str(i), "title": f"Директор по продажам {i}", "company": f"Ком {i}",
                 "text": TEXT} for i in range(N)]))
            out = {"status": await call("status_report"), "list": await call("applications_list"),
                   "ranked": await call("vacancies_ranked")}
            page = payload(out["list"])
            out["page"] = page
            out["second"] = payload(await call("applications_list", offset=len(page)))
            out["full"] = payload(await call("application_get", application_id=page[0]["object_id"]))
            first = page[0]
            out["score"] = await call("application_score", application_id=first["object_id"],
                                      expected_version=first["version"], preliminary=4, why="по профилю", fact_ids=[])
            batch = [{"application_id": a["object_id"], "expected_version": a["version"], "preliminary": 3,
                      "why": "частично", "fact_ids": []} for a in page[1:11]]
            batch.append({"application_id": "app-missing", "expected_version": 1, "preliminary": 3, "why": "-",
                          "fact_ids": []})
            out["batch"] = payload(await call("application_score_batch", items=batch))
            out["ranked_after"] = payload(await call("vacancies_ranked", limit=5))
            out["sizes"] = {k: size(out[k]) for k in ("status", "list", "ranked", "score")}
            return out

        out = asyncio.run(self._session(scenario))
        sizes = out["sizes"]
        # With the whole base these were about 400 000, 2 400 000 and 750 000 characters.
        self.assertLess(sizes["status"], 15_000, sizes)
        self.assertLess(sizes["list"], 40_000, sizes)
        self.assertLess(sizes["ranked"], 30_000, sizes)
        self.assertLess(sizes["score"], 1_500, sizes)

        status = payload(out["status"])
        self.assertEqual(status["funnel"]["counts"]["found"], N)
        self.assertLessEqual(len(status["funnel"]["ids"]["found"]), 20)
        self.assertTrue(any(s.get("count") == N for s in status["next_steps"]), status["next_steps"])

        self.assertEqual(len(out["page"]), 50)
        self.assertNotIn("text", out["page"][0]["vacancy"])
        self.assertNotEqual(out["second"][0]["object_id"], out["page"][0]["object_id"])
        self.assertEqual(out["full"]["vacancy"]["text"], TEXT)

        score = payload(out["score"])
        self.assertEqual(score["state"], "evaluated")
        self.assertNotIn("vacancy", score)

        results = out["batch"]["results"]
        self.assertEqual(sum(r.get("state") == "evaluated" for r in results), 10)
        self.assertIn("not_found", results[-1]["error"])

        after = out["ranked_after"]
        self.assertEqual(after["total_ranked"], 11)
        self.assertEqual(len(after["ranked"]), 5)
        self.assertEqual(after["total_unscored"], N - 11)
        self.assertLessEqual(len(after["unscored"]), 5)

    async def _session(self, scenario):
        async with stdio_client(self.params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            return await scenario(session)


if __name__ == "__main__":
    unittest.main()
