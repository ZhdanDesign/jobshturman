"""Renderer-neutral CV lines and extraction check."""
import unittest

from hrshturman_core.render_layout import blocks, extraction_check

CONTENT = {"full_name": "Иван Учебный", "headline": "Коммерческий директор",
           "experience": [{"employer": "Альфа", "title": "Директор", "start": "2021", "end": "2024",
                           "bullets": [{"text": "Рост выручки на 25%", "fact_ids": ["f1"]}]}],
           "languages": ["Английский B2"]}


class RenderLayoutTests(unittest.TestCase):
    def test_claim_dicts_in_every_text_field(self):  # agents send {text, fact_ids} and dict contacts
        content = {"full_name": {"text": "Иван Учебный"}, "contacts": {"email": "ivan@example.invalid", "phone": "+7 900"},
                   "headline": {"text": "Коммерческий директор", "fact_ids": []},
                   "summary": [{"text": "Про себя", "fact_ids": ["f1"]}],
                   "experience": [{"employer": "Альфа", "title": {"text": "Директор"}, "start": "2021", "end": "2024",
                                   "scope": {"text": "Команда 40 человек", "fact_ids": ["f1"]},
                                   "bullets": [{"text": "Рост выручки на 25%", "fact_ids": ["f1"]}]}],
                   "education": [{"text": "МГУ, 2010", "fact_ids": []}], "languages": [{"text": "Английский B2"}]}
        self.assertEqual(blocks(content), [
            ("name", "Иван Учебный"), ("meta", "ivan@example.invalid · +7 900"), ("title", "Коммерческий директор"),
            ("section", "О себе"), ("para", "Про себя"), ("section", "Опыт работы"), ("role", "Директор, Альфа"),
            ("meta", "2021 – 2024"), ("para", "Команда 40 человек"), ("bullet", "Рост выручки на 25%"),
            ("section", "Образование"), ("bullet", "МГУ, 2010"), ("section", "Языки"), ("bullet", "Английский B2")])

    def test_lines_in_reading_order(self):
        self.assertEqual(blocks(CONTENT), [
            ("name", "Иван Учебный"), ("title", "Коммерческий директор"), ("section", "Опыт работы"),
            ("role", "Директор, Альфа"), ("meta", "2021 – 2024"), ("bullet", "Рост выручки на 25%"),
            ("section", "Языки"), ("bullet", "Английский B2")])

    def test_check_ignores_wrapping_and_reports_lost_lines(self):
        extracted = "Иван Учебный\nКоммерческий\nдиректор\nОпыт работы\nДиректор, Альфа\n2021 – 2024\n• Рост выручки\nна 25%\nЯзыки"
        result = extraction_check(blocks(CONTENT), extracted)
        self.assertEqual(result["missing"], ["Английский B2"])


if __name__ == "__main__":
    unittest.main()
