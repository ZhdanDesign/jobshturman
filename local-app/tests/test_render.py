"""Render of the approved master CV on a synthetic candidate."""
import tempfile
import unittest
from pathlib import Path

from hrshturman_core import DomainError, Store
from hrshturman_core import resume
from hrshturman_core.workspace import create_candidate
from hrshturman_local import render


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.store = Store(self.ws / "workspace.sqlite3").init()
        create_candidate(self.store, "me", "Europe/Moscow")
        fact = resume.add_fact(self.store, "me", "Рост выручки на 25% за 2023 год", "Альфа", {}, [])
        resume.set_fact_status(self.store, "me", fact["object_id"], "confirmed", fact["version"])
        self.content = {
            "track_id": "main", "language": "ru", "full_name": "Иван Учебный", "contacts": "ivan@example.invalid",
            "headline": "Коммерческий директор, B2B-дистрибуция",
            "experience": [{"employer": "Альфа", "title": "Коммерческий директор", "start": "2021", "end": "2024",
                            "bullets": [{"text": "Рост выручки на 25% за 2023 год", "fact_ids": [fact["object_id"]]}]}],
            "competencies": [{"text": "Рост выручки", "fact_ids": [fact["object_id"]]}],
            "languages": ["Английский B2"],
        }
        self.saved = resume.save_resume(self.store, "me", self.content)

    def tearDown(self):
        self.tmp.cleanup()

    def test_draft_is_not_rendered(self):
        with self.assertRaises(DomainError) as ctx:
            render.render_resume(self.store, "me", self.ws)
        self.assertEqual(ctx.exception.code, "stale_inputs")

    def test_approved_resume_renders_with_extractable_text(self):
        resume.approve_resume(self.store, "me", "main", self.saved["version"])
        manifest = render.render_resume(self.store, "me", self.ws)
        self.assertTrue(manifest["text_extraction"]["docx"]["ok"], manifest["text_extraction"]["docx"])
        self.assertTrue(manifest["text_extraction"]["pdf"]["ok"], manifest["text_extraction"]["pdf"])
        self.assertEqual(manifest["layout"], "not_verified")
        for f in manifest["files"]:
            self.assertTrue(Path(f["path"]).is_file())
        self.assertEqual(resume.get_resume(self.store, "me")["render"]["files"][1]["format"], "pdf")

    def test_extraction_check_catches_lost_line(self):
        lines = render.blocks(self.content)
        result = render.extraction_check(lines, "Иван Учебный")
        self.assertFalse(result["ok"])
        self.assertIn("Коммерческий директор, B2B-дистрибуция", result["missing"])

    def test_render_of_old_version_is_refused(self):
        approved = resume.approve_resume(self.store, "me", "main", self.saved["version"])
        manifest = {"content_hash": "0" * 64, "files": [], "text_extraction": {}}
        with self.assertRaises(DomainError) as ctx:
            resume.record_render(self.store, "me", "main", manifest)
        self.assertEqual(ctx.exception.code, "stale_inputs")
        self.assertEqual(approved["status"], "approved")


if __name__ == "__main__":
    unittest.main()
