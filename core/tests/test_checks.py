"""Checks moved from the methodology prose into code: stage 15 constraints, texts for employers, stage 06 plan."""
import unittest
from typing import ClassVar

from hrshturman_core import DomainError, checks, resume
from test_core import Base

POLICY = {"salary_min": 400000, "currency": "RUB", "formats": ["REMOTE", "HYBRID"], "locations": ["Москва"]}


def vacancy(**kw):
    return {"title": "Коммерческий директор", "location": "Москва", "work_format": ["HYBRID"],
            "salary": {"min": 350000, "max": 450000, "currency": "RUR", "gross": True, "period": None}} | kw


class VacancyConstraintsTests(unittest.TestCase):
    def test_match(self):
        out = checks.vacancy_constraints(vacancy(salary={"min": 450000, "currency": "RUB"}), POLICY)
        self.assertEqual((out["salary"], out["format"], out["location"], out["hard_conflicts"]),
                         ("ok", "ok", "ok", []))

    def test_unknown_salary_is_not_zero_and_not_a_match(self):
        for salary in (None, {"min": None, "max": None, "currency": "RUB"}):
            out = checks.vacancy_constraints(vacancy(salary=salary), POLICY)
            self.assertEqual(out["salary"], "unknown")
            self.assertEqual(out["hard_conflicts"], [])
            self.assertIn("salary", out["unknown"])

    def test_below_minimum_and_wrong_format_are_hard_conflicts(self):
        out = checks.vacancy_constraints(vacancy(salary={"max": 250000, "currency": "RUB"}, work_format=["ON_SITE"]),
                                         POLICY)
        self.assertEqual(out["hard_conflicts"], ["salary", "format"])

    def test_salary_only_clearly_below_is_a_conflict(self):
        asked = POLICY | {"salary_min": 1200000}
        cases = [({"min": 100000, "max": 100000, "currency": "RUB"}, "below"),   # явно ниже
                 ({"min": 700000, "max": 1000000, "currency": "RUB"}, "near"),  # вилка близко к запросу
                 ({"min": 800000, "currency": "RUB"}, "near"),                  # «от 800 000», верх открыт
                 ({"min": 1300000, "currency": "RUB"}, "ok"),
                 (None, "unknown")]                                              # «по итогам собеседования»
        for salary, expected in cases:
            out = checks.vacancy_constraints(vacancy(salary=salary), asked)
            self.assertEqual(out["salary"], expected, salary)
            self.assertEqual("salary" in out["hard_conflicts"], expected == "below", salary)
        strict = checks.vacancy_constraints(vacancy(salary={"max": 1000000, "currency": "RUB"}),
                                            asked | {"salary_tolerance": 0.1})
        self.assertEqual(strict["salary"], "below")

    def test_other_currency_or_period_is_not_converted(self):
        out = checks.vacancy_constraints(vacancy(salary={"max": 9000, "currency": "USD"}), POLICY)
        self.assertEqual(out["salary"], "unknown")
        out = checks.vacancy_constraints(vacancy(salary={"max": 900000, "currency": "RUB", "period": "year"}), POLICY)
        self.assertEqual(out["salary"], "unknown")

    def test_location(self):
        self.assertEqual(checks.vacancy_constraints(vacancy(location="Казань"), POLICY)["location"], "conflict")
        self.assertEqual(checks.vacancy_constraints(vacancy(location="Казань", work_format=["REMOTE"]),
                                                    POLICY)["location"], "ok")
        moved = checks.vacancy_constraints(vacancy(location="Казань"), POLICY | {"relocation": True})
        self.assertEqual(moved["location"], "ok")
        self.assertIn("нужен переезд", moved["notes"])
        self.assertEqual(checks.vacancy_constraints(vacancy(location=None), POLICY)["location"], "unknown")

    def test_bad_policy(self):
        with self.assertRaises(DomainError):
            checks.vacancy_constraints(vacancy(), {"salary": 1})
        with self.assertRaises(DomainError):
            checks.vacancy_constraints(vacancy(), {"formats": ["office"]})


class ScreenTests(unittest.TestCase):
    VACANCIES: ClassVar[list] = [
        {"id": "a", "vacancy": vacancy(salary={"min": 100000, "max": 150000, "currency": "RUB"})},  # явно ниже
        {"id": "b", "vacancy": vacancy(salary=None)},                                               # зарплаты нет
        {"id": "c", "vacancy": vacancy(salary={"max": 330000, "currency": "RUB"})},                 # на 17% ниже
        {"id": "d", "vacancy": vacancy(work_format=["ON_SITE"], salary=None)},                     # не тот формат
    ]

    def ids(self, level):
        out = checks.screen(self.VACANCIES, POLICY, level)
        return out["keep"], {d["id"] for d in out["dropped"]}

    def test_levels(self):
        self.assertEqual(self.ids("aggressive"), (["a", "b", "c", "d"], set()))
        self.assertEqual(self.ids("moderate"), (["b", "c"], {"a", "d"}))
        self.assertEqual(self.ids("specialized"), (["b"], {"a", "c", "d"}))

    def test_default_is_moderate_and_unknown_level_fails(self):
        self.assertEqual(checks.screen(self.VACANCIES, POLICY)["level"], "moderate")
        with self.assertRaises(DomainError):
            checks.screen(self.VACANCIES, POLICY, "light")


class TextCheckTests(Base):
    def setUp(self):
        super().setUp()
        self.fact = resume.add_fact(self.store, "cand-a", "Рост выручки на 25% за 2023 год", "Альфа",
                                    {"start": "2021", "end": "2024"}, [{"source_id": "cv", "locator": "p1"}])
        self.nda = resume.add_fact(self.store, "cand-a", "Закрытый клиент", "Альфа", {}, [], disclosure="nda")
        self.facts = {f["object_id"]: f for f in resume.list_facts(self.store, "cand-a")}

    def test_supported_text_passes_with_a_warning_for_unconfirmed_fact(self):
        out = checks.check_text("Вырастил выручку на 25% в 2023 году.", [self.fact["object_id"]], self.facts)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["warnings"][0]["code"], "needs_confirmation")

    def test_unsupported_number_placeholder_and_nda_are_errors(self):
        out = checks.check_text("Вырастил выручку на 40% в [компания].", [self.fact["object_id"], self.nda["object_id"]],
                                self.facts)
        codes = sorted(e["code"] for e in out["errors"])
        self.assertEqual(codes, ["invalid_material", "missing_fact", "restricted_disclosure"])
        self.assertFalse(out["ok"])

    def test_without_facts_any_number_is_an_error(self):
        out = checks.check_text("Добрый день! Готов обсудить роль, опыт 15 лет.", [], self.facts)
        self.assertEqual([e["code"] for e in out["errors"]], ["missing_fact"])
        self.assertTrue(checks.check_text("Добрый день! Готов обсудить роль.", [], self.facts)["ok"])


class PlanCheckTests(unittest.TestCase):
    def task(self, id, **kw):
        return {"id": id, "title": id, "owner": "кандидат", "criterion": "готово", "start": "2026-10-01",
                "due": "2026-10-10", "depends_on": []} | kw

    def test_good_plan(self):
        plan = [self.task("a"), self.task("b", start="2026-10-11", due="2026-10-20", depends_on=["a"])]
        self.assertEqual(checks.check_plan(plan), {"ok": True, "errors": [], "tasks": 2})

    def test_every_rule(self):
        plan = [self.task("a", owner="", due="2026-09-01"),
                self.task("b", criterion=None, depends_on=["a", "zzz"]),
                self.task("c", due=None, depends_on=["d"]), self.task("d", start="2026-10-20", due="2026-10-25",
                                                                      depends_on=["c"])]
        codes = {(e["code"], e["task_id"]) for e in checks.check_plan(plan)["errors"]}
        self.assertEqual(codes, {("missing_owner", "a"), ("due_before_start", "a"), ("missing_criterion", "b"),
                                 ("unknown_dependency", "b"), ("missing_due", "c"), ("starts_before_dependency", "c"),
                                 ("cycle", "d")})

    def test_bad_dates(self):
        with self.assertRaises(DomainError):
            checks.check_plan([self.task("a", due="10.10.2026")])


if __name__ == "__main__":
    unittest.main()
