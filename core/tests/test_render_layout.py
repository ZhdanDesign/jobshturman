"""Renderer-neutral CV lines and extraction check."""
import unittest

from hrshturman_core.render_layout import blocks, extraction_check

CONTENT = {"full_name": "Иван Учебный", "headline": "Коммерческий директор",
           "experience": [{"employer": "Альфа", "title": "Директор", "start": "2021", "end": "2024",
                           "bullets": [{"text": "Рост выручки на 25%", "fact_ids": ["f1"]}]}],
           "languages": ["Английский B2"]}


class RenderLayoutTests(unittest.TestCase):
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
