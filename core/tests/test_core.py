"""Acceptance-style tests on synthetic candidates. No network, no real data."""
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

from hrshturman_core import DomainError, Store, intake, report, resume
from hrshturman_core import applications as apps
from hrshturman_core.common import canonical
from hrshturman_core.workspace import create_candidate

PAST = "2026-01-01T10:00:00+00:00"


def future(days=7):
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "ws.sqlite3").init()
        create_candidate(self.store, "cand-a", "Europe/Moscow")

    def tearDown(self):
        self.tmp.cleanup()

    def assertCode(self, code, fn, *args, **kwargs):
        with self.assertRaises(DomainError) as ctx:
            fn(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code, str(ctx.exception))


class IntakeAnswerTests(Base):
    """S3: one normalizer for answers from a form, the agent's built-in choice or a numbered list."""

    def test_pain_answers_from_any_channel(self):
        cases = {
            "1, 3": ([1, 3], None, False),
            "1 и 7": ([1, 7], None, False),
            "Пропустить": ([], None, True),
            "1, 3, свой: устал от откликов": ([1, 3], "устал от откликов", False),
            "2, 5, и ещё своё: устал от откликов": ([2, 5], "устал от откликов", False),
            "Свой вариант — переезд в другой город": ([], "переезд в другой город", False),
            "Мне сложно уйти с текущей работы": ([], "Мне сложно уйти с текущей работы", False),
        }
        for answer, expected in cases.items():
            self.assertEqual(intake.parse_pains(answer), expected, answer)
        # Option texts as a built-in multi-choice returns them, numbers as a form returns them.
        self.assertEqual(intake.parse_pains([intake.PAINS[2], "Поиск выматывает, всё труднее продолжать."]),
                         ([2, 8], None, False))
        self.assertEqual(intake.parse_pains([5, "5", 2]), ([5, 2], None, False))
        self.assertCode("invalid_schema", intake.parse_pains, "9")

    def test_blocks_fill_one_draft_without_losing_earlier_answers(self):  # ДИАГ-09
        intake.answer_block(self.store, "cand-a", "pains", "2, 5")
        intake.answer_block(self.store, "cand-a", "situation_goal",
                            {"target_roles": "Финансовый директор", "employment_status": "работаю", "deadlines": ""})
        card = intake.answer_block(self.store, "cand-a", "situation_search", {"funnel": "откликов много, встреч нет"})
        self.assertEqual(card["pain_ids"], [2, 5])
        self.assertEqual(card["situation"], {"target_roles": "Финансовый директор", "employment_status": "работаю",
                                             "funnel": "откликов много, встреч нет"})
        self.assertIn("deadlines", card["request_map"]["unknown_fields"])
        self.assertEqual(card["status"], "draft")
        with self.store.read() as tx:
            self.assertEqual(len(tx.list("intake", "cand-a")), 1)
        self.assertEqual(intake.confirm(self.store, "cand-a", card["version"])["status"], "confirmed")

    def test_every_block_can_be_asked(self):
        for block in intake.BLOCKS:
            q = intake.questions(block)
            self.assertTrue(q.get("options") or q.get("questions"), block)
        self.assertEqual(len(intake.questions("pains")["options"]), 8)
        asked = [f for b in intake.SITUATION_BLOCKS.values() for f, _ in b]
        self.assertEqual(set(asked), intake.SITUATION_FIELDS)
        self.assertTrue(all(len(b) <= 4 for b in intake.SITUATION_BLOCKS.values()))
        self.assertCode("invalid_schema", intake.answer_block, self.store, "cand-a", "situation_goal",
                        {"funnel": "x"})


class IntakeTests(Base):
    def test_all_pains_custom_and_skip_allowed(self):  # ДИАГ-01
        obj = intake.save_draft(self.store, "cand-a", [1, 2, 3, 4], custom_pain="Свой ответ")
        self.assertEqual(len(intake.request_map(obj)["pains"]), 4)
        self.assertEqual(intake.request_map(obj)["first_focus"], 1)
        obj = intake.save_draft(self.store, "cand-a", [], skipped=True, expected_version=obj["version"])
        self.assertTrue(obj["skipped"])

    def test_not_confirmed_until_explicit_and_card_unchanged(self):  # ДИАГ-02
        obj = intake.save_draft(self.store, "cand-a", [5])
        self.assertEqual(obj["status"], "draft")
        changed = intake.save_draft(self.store, "cand-a", [5, 6], expected_version=obj["version"])
        self.assertCode("state_version_conflict", intake.confirm, self.store, "cand-a", obj["version"])
        self.assertEqual(intake.confirm(self.store, "cand-a", changed["version"])["status"], "confirmed")

    def test_repeat_submit_creates_no_duplicate(self):  # ДИАГ-09
        obj = intake.save_draft(self.store, "cand-a", [3])
        first = intake.confirm(self.store, "cand-a", obj["version"])
        again = intake.confirm(self.store, "cand-a", obj["version"])
        self.assertEqual(first["object_id"], again["object_id"])
        self.assertEqual(first["version"], again["version"])
        self.assertCode("state_version_conflict", intake.save_draft, self.store, "cand-a", [1])
        kinds = [e["kind"] for e in report.events(self.store, "cand-a")]
        self.assertEqual(kinds.count("intake_confirmed"), 1)
        help1 = intake.request_help(self.store, "cand-a", "career_consultant", "email", "карта запроса")
        help2 = intake.request_help(self.store, "cand-a", "career_consultant", "email", "карта запроса")
        self.assertEqual(help1["object_id"], help2["object_id"])

    def test_secrets_rejected(self):
        self.assertCode("restricted_disclosure", intake.save_draft, self.store, "cand-a", [1],
                        situation={"constraints": "password=hunter2"})


class ResumeTests(Base):
    def facts(self):
        a = resume.add_fact(self.store, "cand-a", "Рост выручки на 25% за 2023 год", "Альфа",
                            {"start": "2021", "end": "2024"}, [{"source_id": "src-1", "locator": "p.1"}],
                            metric={"value": 25, "unit": "%"})
        b = resume.add_fact(self.store, "cand-a", "Руководил командой 40 человек", "Бета",
                            {"start": "2018", "end": "2021"}, [{"source_id": "src-1", "locator": "p.2"}])
        return a, b

    def cv(self, bullet_fact, text="Рост выручки на 25%", employer="Альфа"):
        return {"track_id": "main", "language": "ru", "headline": "Коммерческий директор",
                "experience": [{"employer": employer, "title": "Директор", "start": "2021", "end": "2024",
                                "bullets": [{"text": text, "fact_ids": [bullet_fact]}]}]}

    def test_metric_moved_to_other_company_is_blocked(self):  # stage 07 scenario
        a, _ = self.facts()
        result = resume.save_resume(self.store, "cand-a", self.cv(a["object_id"], employer="Бета"))
        self.assertFalse(result["validation"]["ok"])
        self.assertEqual(result["validation"]["errors"][0]["code"], "fact_conflict")

    def test_unconfirmed_number_is_blocked(self):
        a, _ = self.facts()
        result = resume.save_resume(self.store, "cand-a", self.cv(a["object_id"], text="Рост выручки на 35%"))
        self.assertIn("missing_fact", [e["code"] for e in result["validation"]["errors"]])

    def test_nda_and_placeholder_blocked(self):
        a, _ = self.facts()
        nda = resume.add_fact(self.store, "cand-a", "Закрытый клиент", "Альфа", {}, [], disclosure="nda")
        cv = self.cv(a["object_id"], text="Рост выручки на [X]%")
        cv["experience"][0]["bullets"].append({"text": "Проект для клиента", "fact_ids": [nda["object_id"]]})
        codes = [e["code"] for e in resume.save_resume(self.store, "cand-a", cv)["validation"]["errors"]]
        self.assertIn("invalid_material", codes)
        self.assertIn("restricted_disclosure", codes)

    def test_empty_resume_is_not_approved_or_rendered(self):
        for content in ({"track_id": "main", "headline": "Коммерческий директор"},
                        {"track_id": "main", "headline": "CCO", "summary": [{"text": "  ", "fact_ids": []}],
                         "experience": [{"employer": "Альфа", "bullets": []}]}):
            current = resume.get_resume(self.store, "cand-a") if content.get("summary") else None
            saved = resume.save_resume(self.store, "cand-a", content,
                                       expected_version=current and current["version"])
            self.assertCode("empty_resume", resume.approve_resume, self.store, "cand-a", "main", saved["version"])
        self.assertCode("empty_resume", resume.record_render, self.store, "cand-a", "main",
                        {"content_hash": "x", "files": [], "text_extraction": {}})

    def test_approve_requires_confirmed_facts(self):
        a, _ = self.facts()
        saved = resume.save_resume(self.store, "cand-a", self.cv(a["object_id"]))
        self.assertTrue(saved["validation"]["ok"])
        self.assertCode("missing_fact", resume.approve_resume, self.store, "cand-a", "main", saved["version"])
        resume.set_fact_status(self.store, "cand-a", a["object_id"], "confirmed", a["version"])
        approved = resume.approve_resume(self.store, "cand-a", "main", saved["version"])
        self.assertEqual(approved["status"], "approved")
        edited = resume.save_resume(self.store, "cand-a", self.cv(a["object_id"]), expected_version=approved["version"])
        self.assertEqual(edited["status"], "draft")


class ApplicationTests(Base):
    VAC: ClassVar[list] = [{"source": " HH ", "external_id": "101", "title": "Коммерческий директор", "text": "Полный текст"},
           {"source": "telegram", "external_id": "chan-1:55", "title": "CFO", "text": None}]

    def imported(self):
        apps.import_vacancies(self.store, "cand-a", self.VAC)
        return {a["vacancy"]["external_id"]: a for a in apps.list_applications(self.store, "cand-a")}

    def ready(self):
        app = self.imported()["101"]
        app = apps.score(self.store, "cand-a", app["object_id"], app["version"], 4, "Опыт совпадает", [], final=4)
        app = apps.decide(self.store, "cand-a", app["object_id"], app["version"], "approve", "интересно")
        return apps.prepare(self.store, "cand-a", app["object_id"], app["version"], {"cv": "v1", "letter": "текст"}, "hh-main")

    def test_reimport_keeps_state(self):
        app = self.ready()
        counts = apps.import_vacancies(self.store, "cand-a", self.VAC)
        self.assertEqual(counts, {"added": 0, "updated": 0, "unchanged": 2})
        changed = [dict(self.VAC[0], title="Коммерческий директор (обновлено)")]
        self.assertEqual(apps.import_vacancies(self.store, "cand-a", changed)["updated"], 1)
        after = {a["object_id"]: a for a in apps.list_applications(self.store, "cand-a")}[app["object_id"]]
        self.assertEqual(after["state"], "draft_ready")
        self.assertEqual(after["material"]["hash"], app["material"]["hash"])

    def test_a_vacancy_mentioning_a_secret_does_not_stop_the_import(self):
        """A vacancy is public text: one that shows a key or token example (an AI/LLM lead role did) must not
        fail the whole sync. The secret-like part is hidden, the rest comes in."""
        leaky = {"source": "hh", "external_id": "202", "title": "AI / LLM SDLC Lead",
                 "text": "Настроить доступ: api_key=sk-abcdef1234567890XYZ и Bearer abcdefghijklmnop",
                 "url": "https://example.ru/v/202?access_token=qwerty12345"}
        counts = apps.import_vacancies(self.store, "cand-a", [*self.VAC, leaky])
        self.assertEqual(counts["added"], 3)
        card = {a["vacancy"]["external_id"]: a for a in apps.list_applications(self.store, "cand-a")}["202"]
        stored = canonical(card["vacancy"])
        for secret in ("sk-abcdef1234567890XYZ", "abcdefghijklmnop", "qwerty12345"):
            self.assertNotIn(secret, stored)
        self.assertIn("Настроить доступ", card["vacancy"]["text"])

    def test_no_final_score_without_full_text(self):
        tg = self.imported()["chan-1:55"]
        self.assertEqual(tg["state"], "needs_full_text")
        self.assertCode("incomplete_source", apps.score, self.store, "cand-a", tg["object_id"], tg["version"],
                        3, "по заголовку", [], final=3)
        tg = apps.score(self.store, "cand-a", tg["object_id"], tg["version"], 3, "по заголовку", [])
        self.assertCode("state_version_conflict", apps.decide, self.store, "cand-a", tg["object_id"], tg["version"],
                        "approve", "")

    def test_authority_binds_exact_package(self):
        app = self.ready()
        self.assertCode("invalid_material", apps.authorize, self.store, "cand-a", app["object_id"], app["version"],
                        "0" * 64, "hh-main", future())
        app = apps.authorize(self.store, "cand-a", app["object_id"], app["version"], app["material"]["hash"],
                             "hh-main", future())
        app = apps.prepare(self.store, "cand-a", app["object_id"], app["version"], {"cv": "v2"}, "hh-main")
        self.assertEqual(app["state"], "draft_ready")
        self.assertIsNone(app["authority"])

    def test_revoke_blocks_sending(self):
        app = self.ready()
        app = apps.authorize(self.store, "cand-a", app["object_id"], app["version"], app["material"]["hash"],
                             "hh-main", future())
        app = apps.revoke(self.store, "cand-a", app["object_id"], app["version"])
        self.assertCode("state_version_conflict", apps.record_sent_by_candidate, self.store, "cand-a",
                        app["object_id"], app["version"], {"observed_at": PAST, "proof_ref": "screenshot-1"})

    def test_manual_send_is_marked_self_reported_and_counted_once(self):
        app = self.ready()
        app = apps.authorize(self.store, "cand-a", app["object_id"], app["version"], app["material"]["hash"],
                             "hh-main", future())
        self.assertCode("invalid_schema", apps.record_sent_by_candidate, self.store, "cand-a",
                        app["object_id"], app["version"], {"observed_at": PAST})
        app = apps.record_sent_by_candidate(self.store, "cand-a", app["object_id"], app["version"],
                                            {"observed_at": PAST, "proof_ref": "screenshot-1"})
        self.assertEqual(app["evidence"]["level"], "self_reported")
        self.assertCode("state_version_conflict", apps.prepare, self.store, "cand-a", app["object_id"],
                        app["version"], {"cv": "v3"}, "hh-main")
        app = apps.record_employer_event(self.store, "cand-a", app["object_id"], app["version"], "invited", PAST, "hh")
        counts = report.status(self.store, "cand-a")["funnel"]["counts"]
        self.assertEqual((counts["sent_confirmed"], counts["sent_self_reported"], counts["invited"]), (1, 1, 1))
        self.assertEqual((counts["prepared"], counts["authorized"]), (1, 1))  # cumulative funnel

    def test_unknown_requires_reconciliation(self):  # СЦ-08
        app = self.ready()
        app = apps.authorize(self.store, "cand-a", app["object_id"], app["version"], app["material"]["hash"],
                             "hh-main", future())
        app = apps.reserve(self.store, "cand-a", app["object_id"], app["version"], "worker-1")
        op = app["operation_id"]
        app = apps.mark_unknown(self.store, "cand-a", app["object_id"], app["version"], "timeout")
        for fn, args in ((apps.reserve, ("worker-2",)), (apps.record_sent_by_candidate, ({"observed_at": PAST, "proof_ref": "x"},)),
                         (apps.prepare, ({"cv": "v2"}, "hh-main"))):
            self.assertCode("state_version_conflict", fn, self.store, "cand-a", app["object_id"], app["version"], *args)
        self.assertCode("invalid_schema", apps.reconcile, self.store, "cand-a", app["object_id"], app["version"],
                        "sent", {"observed_at": PAST, "proof_ref": "history"})
        app = apps.reconcile(self.store, "cand-a", app["object_id"], app["version"], "not_sent",
                             {"observed_at": PAST, "proof_ref": "history", "check_reference": "hist-1"})
        self.assertEqual(app["state"], "not_sent_confirmed")
        self.assertEqual(report.status(self.store, "cand-a")["funnel"]["counts"]["sent_confirmed"], 0)
        app = apps.prepare(self.store, "cand-a", app["object_id"], app["version"], {"cv": "v1", "letter": "текст"}, "hh-main")
        app = apps.authorize(self.store, "cand-a", app["object_id"], app["version"], app["material"]["hash"],
                             "hh-main", future())
        self.assertEqual(apps.reserve(self.store, "cand-a", app["object_id"], app["version"], "w")["operation_id"], op)

    def test_pause_blocks_new_external_actions(self):
        app = self.ready()
        app = apps.authorize(self.store, "cand-a", app["object_id"], app["version"], app["material"]["hash"],
                             "hh-main", future())
        intake.set_paused(self.store, "cand-a", True)
        self.assertCode("permission_required", apps.reserve, self.store, "cand-a", app["object_id"], app["version"], "w")

    def test_concurrent_reserve_only_one_wins(self):
        app = self.ready()
        app = apps.authorize(self.store, "cand-a", app["object_id"], app["version"], app["material"]["hash"],
                             "hh-main", future())
        results = []

        def attempt(worker):
            try:
                apps.reserve(self.store, "cand-a", app["object_id"], app["version"], worker)
                results.append("ok")
            except DomainError as exc:
                results.append(exc.code)
        threads = [threading.Thread(target=attempt, args=(f"w{i}",)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count("ok"), 1)


class BranchTests(Base):
    def test_service_branch_notice(self):
        create_candidate(self.store, "cand-s", "Europe/Moscow", branch="service")
        intake.save_draft(self.store, "cand-s", [1])
        self.assertEqual(intake.get_intake(self.store, "cand-s")["request_map"]["stored_data_notice"],
                         intake.STORED_DATA_NOTICE["service"])


class IsolationTests(Base):
    def test_two_candidates_see_only_their_data(self):
        create_candidate(self.store, "cand-b", "Europe/Moscow")
        apps.import_vacancies(self.store, "cand-a", [{"source": "hh", "external_id": "1", "title": "A"}])
        fact = resume.add_fact(self.store, "cand-a", "Факт A", "Альфа", {}, [])
        self.assertEqual(apps.list_applications(self.store, "cand-b"), [])
        self.assertCode("not_found", resume.set_fact_status, self.store, "cand-b", fact["object_id"], "confirmed", 1)
        self.assertEqual(report.status(self.store, "cand-b")["funnel"]["counts"]["found"], 0)
        self.assertCode("not_found", report.status, self.store, "cand-x")


if __name__ == "__main__":
    unittest.main()
