import unittest

from hrshturman_core.render_layout import blocks as typeset_blocks
from hrshturman_core.render_layout import extraction_check
from hrshturman_core.render_layout import period as real_period
from hrshturman_core.typography import NBSP, fix_text, keep_phrases, typeset

D = " — "


def blocks(content):
    """Structure tests read plain spaces; typography has its own tests below."""
    return [(kind, text.replace(NBSP, " ")) for kind, text in typeset_blocks(content)]


def period(*args):
    return real_period(*args).replace(NBSP, " ")

CONTENT = {"full_name": "Иван Учебный", "headline": "Коммерческий директор",
           "experience": [{"employer": "Альфа", "title": "Директор", "start": "2021", "end": "2024",
                           "bullets": [{"text": "Рост выручки на 25%", "fact_ids": ["f1"]}]}],
           "languages": ["Английский B2"]}


class RenderLayoutTests(unittest.TestCase):
    def test_lines_in_reading_order(self):
        self.assertEqual(blocks(CONTENT), [
            ("name", "Иван Учебный"), ("title", "Коммерческий директор"), ("section", "Опыт работы"),
            ("role", f"Альфа{D}Директор"), ("dates", f"2021{D}2024"), ("bullet", "Рост выручки на 25%"),
            ("section", "Языки"), ("line", "Английский B2")])

    def test_claim_dicts_in_every_text_field(self):  # agents send {text, fact_ids} anywhere
        content = {"full_name": {"text": "Иван Учебный"}, "headline": {"text": "Коммерческий директор", "fact_ids": []},
                   "summary": [{"text": "Про себя", "fact_ids": ["f1"]}],
                   "experience": [{"employer": "Альфа", "title": {"text": "Директор"}, "start": "2021", "end": "2024",
                                   "scope": {"text": "Команда 40 человек", "fact_ids": ["f1"]},
                                   "bullets": [{"text": "Рост выручки на 25%", "fact_ids": ["f1"]}]}],
                   "education": [{"text": "МГУ, 2010", "fact_ids": []}], "languages": [{"text": "Английский B2"}]}
        self.assertEqual(blocks(content), [
            ("name", "Иван Учебный"), ("title", "Коммерческий директор"),
            ("section", "О себе"), ("bullet", "Про себя"), ("section", "Опыт работы"), ("role", f"Альфа{D}Директор"),
            ("dates", f"2021{D}2024"), ("scope", "Команда 40 человек"), ("bullet", "Рост выручки на 25%"),
            ("section", "Образование"), ("line", "МГУ, 2010"), ("section", "Языки"), ("line", "Английский B2")])

    def test_contacts_print_only_contact_fields(self):  # bug 0.9.4.7: fact_ids leaked into the contact line
        content = {"contacts": {"email": "ivan@example.invalid", "fact_ids": ["fact_6a08bde16ca44357"],
                                "name": "Иван Учебный", "phone": "+7 900 000-00-00", "note_id": "x1"}}
        self.assertEqual(blocks(content), [("name", "Иван Учебный"),
                                           ("contacts", "+7 900 000-00-00 · ivan@example.invalid")])
        self.assertEqual(blocks({"full_name": "Иван", "contacts": "a@b.c · +7"})[1], ("contacts", "a@b.c · +7"))

    def test_current_job_reads_present(self):  # bug: end=None printed as «2019»
        self.assertEqual(period("2019", None), f"2019{D}н.в.")
        self.assertEqual(period("2019", None, "en"), f"2019{D}Present")
        self.assertEqual(period("2019", "2020"), f"2019{D}2020")
        self.assertEqual(period(None, None), "")
        content = {"experience": [{"employer": "ИП", "title": "Основатель", "start": "2019", "end": None}]}
        self.assertIn(("dates", f"2019{D}н.в."), blocks(content))

    def test_same_neighbour_entries_group_into_projects(self):
        role = {"employer": "ИП", "title": "Основатель", "start": "2019", "end": None}
        content = {"experience": [dict(role, scope="Проект А", bullets=["а1"]), dict(role, scope="Проект Б", bullets=["б1"]),
                                  {"employer": "Альфа", "title": "Директор", "start": "2012", "end": "2019",
                                   "scope": "Дистрибьютор", "bullets": ["в1"]}]}
        self.assertEqual(blocks(content), [
            ("section", "Опыт работы"), ("role", f"ИП{D}Основатель"), ("dates", f"2019{D}н.в."),
            ("project", "Проект А"), ("bullet", "а1"), ("project", "Проект Б"), ("bullet", "б1"),
            ("role", f"Альфа{D}Директор"), ("dates", f"2012{D}2019"), ("scope", "Дистрибьютор"), ("bullet", "в1")])

    def test_education_and_languages_are_lines_with_one_dash(self):
        content = {"education": ["МГУ, 1991–1997", "Курс, 2022 - н.в.",
                                 {"institution": "МГУ", "specialty": "экономика", "start": "1991", "end": "1997"}],
                   "languages": [{"language": "Немецкий", "level": "A2", "fact_ids": ["f1"]}]}
        self.assertEqual(blocks(content)[1:4] + blocks(content)[5:], [
            ("line", f"МГУ, 1991{D}1997"), ("line", f"Курс, 2022{D}н.в."),
            ("line", f"МГУ{D}экономика · 1991{D}1997"), ("line", f"Немецкий{D}A2")])

    def test_check_ignores_wrapping_and_reports_lost_lines(self):
        extracted = "Иван Учебный\nКоммерческий\nдиректор\nОпыт работы\nАльфа — Директор\n2021 — 2024\n• Рост выручки\nна 25%\nЯзыки"
        result = extraction_check(typeset_blocks(CONTENT), extracted)
        self.assertEqual([m.replace(NBSP, " ") for m in result["missing"]], ["Английский B2"])
        self.assertTrue(result["name_first"])

    def test_check_fails_on_internal_ids_and_name_not_first(self):
        lines = [("name", "Иван"), ("contacts", "a@b.c")]
        result = extraction_check(lines, "a@b.c · fact_6a08bde16ca44357\nИван")
        self.assertFalse(result["ok"])
        self.assertEqual(result["leaked_ids"], ["fact_6a08bde16ca44357"])
        self.assertFalse(result["name_first"])
        self.assertTrue(extraction_check(lines, "Иван\na@b.c")["ok"])



class TypographyTests(unittest.TestCase):
    def test_same_rules_as_the_site_tool(self):  # scripts/nbsp.py is the reference
        import importlib.util
        from pathlib import Path
        path = Path(__file__).resolve().parents[2] / "scripts" / "nbsp.py"
        spec = importlib.util.spec_from_file_location("nbsp", path)
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        for text in ("Работал в Москве и в Казани 15 лет", "Рост на 25% за 2023 год — при этом без найма",
                     "Сделал бы, если бы мог", "Не о том, а о главном: 300+ клиентов"):
            self.assertEqual(fix_text(text), tool.fix_text(text), text)

    def test_headline_keeps_short_parts_whole(self):  # owner: «Forward / Deployed Engineer» was split
        text = typeset("title", "Руководитель AI-трансформации и автоматизации процессов | Forward Deployed Engineer")
        self.assertIn(f"процессов{NBSP}| Forward{NBSP}Deployed{NBSP}Engineer", text)
        self.assertIn(f"и{NBSP}автоматизации", text)
        self.assertIn("Руководитель AI-трансформации", text)  # a long part still wraps

    def test_contacts_never_break_inside_a_part(self):
        text = typeset("contacts", "+7 900 000-00-00 · Москва, готов к релокации")
        self.assertEqual(text, f"+7{NBSP}900{NBSP}000-00-00{NBSP}· Москва,{NBSP}готов{NBSP}к{NBSP}релокации")

    def test_numbers_prepositions_and_last_word(self):
        text = typeset("bullet", "Запустил систему в работу за 6 недель")
        self.assertEqual(text, f"Запустил систему в{NBSP}работу за{NBSP}6{NBSP}недель")
        self.assertEqual(typeset("bullet", text), text)  # idempotent

    def test_only_spaces_change(self):
        for kind in ("name", "title", "contacts", "role", "dates", "project", "scope", "bullet", "line"):
            text = "ИП Учебный — Основатель | Forward Deployed Engineer · 2019 — н.в. за 6 недель"
            self.assertEqual(typeset(kind, text).replace(NBSP, " "), text, kind)
        self.assertEqual(keep_phrases("a — b"), f"a{NBSP}— b")


if __name__ == "__main__":
    unittest.main()
