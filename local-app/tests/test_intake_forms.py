"""Stage 00 questionnaire asked three ways: an MCP form, the agent's own choice, a numbered list in the chat.

Whatever way the person answers, the request map ends up the same.
"""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from test_mcp_server import payload

ROOT = Path(__file__).resolve().parents[2]
SITUATION = {"target_roles": "Финансовый директор", "employment_status": "работаю", "deadlines": "до весны"}


def form_filler(answers_by_block, seen):
    """A client that shows MCP forms; the person fills them like this."""
    async def callback(context, params):
        props = params.requested_schema["properties"]
        seen.append(props)
        block = "pains" if "pain_1" in props else next((b for b, a in answers_by_block.items() if set(a) <= set(props)), None)
        answer = answers_by_block.get(block)
        if answer is None:
            return types.ElicitResult(action="decline")
        return types.ElicitResult(action="accept", content=answer)
    return callback


class IntakeFormTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        env = dict(os.environ, HRSHTURMAN_WORKSPACE=self.tmp.name, HRSHTURMAN_CANDIDATE="synthetic-1",
                   PYTHONPATH=os.pathsep.join([str(ROOT / "core"), str(ROOT / "local-app")]))
        self.params = StdioServerParameters(command=sys.executable, args=["-m", "hrshturman_local.mcp_server"], env=env)

    def tearDown(self):
        self.tmp.cleanup()

    def run_session(self, scenario, elicitation_callback=None):
        async def go():
            async with stdio_client(self.params) as (read, write):
                async with ClientSession(read, write, elicitation_callback=elicitation_callback) as session:
                    await session.initialize()
                    return await scenario(session)
        return asyncio.run(go())

    def card_after(self, scenario, callback=None):
        async def wrapped(s):
            await scenario(s)
            return payload(await s.call_tool("intake_get", {}))["intake"]
        card = self.run_session(wrapped, callback)
        return {k: card[k] for k in ("pain_ids", "custom_pain", "skipped", "situation")}

    def test_form_chat_and_choice_give_the_same_card(self):
        seen = []
        form = {"pains": {"pain_2": True, "pain_5": True, "own": "устал от откликов"}, "situation_goal": SITUATION}

        async def by_form(s):
            for block in ("pains", "situation_goal"):
                out = payload(await s.call_tool("intake_ask", {"block": block}))
                self.assertEqual(out["asked_by"], "form")
        via_form = self.card_after(by_form, form_filler(form, seen))
        self.assertEqual(len(seen[0]), 10)  # 8 checkboxes, own answer, skip
        self.assertTrue(all(p["type"] in ("boolean", "string") for p in seen[0].values()))

        async def by_chat(s):
            out = payload(await s.call_tool("intake_ask", {"block": "pains"}))
            self.assertEqual((out["asked_by"], out["reason"]), ("agent", "client_has_no_forms"))
            self.assertEqual(len(out["options"]), 8)
            await s.call_tool("intake_answer", {"block": "pains", "answer": "2, 5, свой: устал от откликов"})
            await s.call_tool("intake_answer", {"block": "situation_goal", "answer": SITUATION})
        self.tmp.cleanup()
        self.tmp = tempfile.TemporaryDirectory()
        self.params.env["HRSHTURMAN_WORKSPACE"] = self.tmp.name
        via_chat = self.card_after(by_chat)

        async def by_choice(s):  # the agent's own multi-choice returns the option texts
            await s.call_tool("intake_answer", {"block": "pains", "answer": [
                "Не понимаю, на какие должности могу претендовать.",
                "Трачу много времени на вакансии и отклики, а продвижения не вижу.",
                "свой: устал от откликов"]})
            await s.call_tool("intake_answer", {"block": "situation_goal", "answer": SITUATION})
        self.tmp.cleanup()
        self.tmp = tempfile.TemporaryDirectory()
        self.params.env["HRSHTURMAN_WORKSPACE"] = self.tmp.name
        via_choice = self.card_after(by_choice)

        expected = {"pain_ids": [2, 5], "custom_pain": "устал от откликов", "skipped": False, "situation": SITUATION}
        self.assertEqual(via_form, expected)
        self.assertEqual(via_chat, expected)
        self.assertEqual(via_choice, expected)

    def test_closed_form_falls_back_to_the_chat_and_keeps_earlier_answers(self):
        async def scenario(s):
            payload(await s.call_tool("intake_ask", {"block": "pains"}))
            out = payload(await s.call_tool("intake_ask", {"block": "situation_search"}))
            self.assertEqual((out["asked_by"], out["reason"]), ("agent", "form_decline"))
            self.assertEqual([q["field"] for q in out["questions"]], ["search_history", "funnel", "ready_materials"])
            await s.call_tool("intake_answer", {"block": "situation_search", "answer": {"funnel": "ответов нет"}})
        card = self.card_after(scenario, form_filler({"pains": {"pain_8": True}}, []))
        self.assertEqual(card["pain_ids"], [8])
        self.assertEqual(card["situation"], {"funnel": "ответов нет"})

    def test_form_rides_an_input_required_result_on_the_2026_protocol(self):
        async def never_called(context, params):
            raise AssertionError("2026-07-28 has no mid-call elicitation")

        async def go():
            async with stdio_client(self.params) as (read, write):
                async with ClientSession(read, write, elicitation_callback=never_called) as s:
                    await s.discover()
                    self.assertGreaterEqual(s.protocol_version, "2026-07-28")
                    first = await s.call_tool("intake_ask", {"block": "pains"}, allow_input_required=True)
                    self.assertIsInstance(first, types.InputRequiredResult)
                    (key, request), = first.input_requests.items()
                    self.assertIn("pain_8", request.params.requested_schema["properties"])
                    answer = types.ElicitResult(action="accept", content={"pain_3": True, "pain_8": True})
                    done = await s.call_tool("intake_ask", {"block": "pains"}, input_responses={key: answer},
                                             request_state=first.request_state)
                    return payload(done)
        out = asyncio.run(go())
        self.assertEqual((out["asked_by"], out["intake"]["pain_ids"]), ("form", [3, 8]))

    def test_answered_block_is_not_asked_again(self):
        seen = []

        async def scenario(s):
            before = payload(await s.call_tool("intake_get", {}))
            self.assertEqual(before["missing_blocks"], ["pains", "situation_goal", "situation_search", "situation_limits"])
            await s.call_tool("intake_answer", {"block": "pains", "answer": "5, 8"})
            await s.call_tool("intake_answer", {"block": "situation_limits", "answer": {"help_wanted": "эффективность"}})
            after = payload(await s.call_tool("intake_get", {}))
            self.assertEqual(after["missing_blocks"], ["situation_goal", "situation_search"])
            out = payload(await s.call_tool("intake_ask", {"block": "pains"}))
            self.assertEqual((out["asked_by"], out["reason"]), ("none", "already_answered"))
            self.assertEqual(out["missing_blocks"], ["situation_goal", "situation_search"])
            again = payload(await s.call_tool("intake_ask", {"block": "pains", "again": True}))
            self.assertEqual(again["asked_by"], "form")
        card = self.card_after(scenario, form_filler({"pains": {"pain_1": True}}, seen))
        self.assertEqual(len(seen), 1)  # only the explicit «again» showed a form
        self.assertEqual(card["pain_ids"], [1])

    def test_skip_in_the_form(self):
        async def scenario(s):
            payload(await s.call_tool("intake_ask", {"block": "pains"}))
        card = self.card_after(scenario, form_filler({"pains": {"pain_1": True, "skip": True}}, []))
        self.assertEqual((card["pain_ids"], card["skipped"]), ([], True))


if __name__ == "__main__":
    unittest.main()
