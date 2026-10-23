"""Render of the approved master CV on a synthetic candidate."""
import re
import tempfile
import unittest
import zipfile
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from hrshturman_core import DomainError, Store, resume
from hrshturman_core.workspace import create_candidate
from hrshturman_local import render
from pypdf import PdfReader


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

    def test_saved_resume_with_claim_dicts_renders(self):  # bug 0.9.4.5: scope/contacts as dicts crashed blocks()
        content = dict(self.content, contacts={"email": "ivan@example.invalid", "telegram": "@ivan"},
                       summary=[{"text": "Рост выручки на 25% за 2023 год", "fact_ids": self.content["competencies"][0]["fact_ids"]}],
                       education=[{"text": "МГУ, экономика, 2010", "fact_ids": []}],
                       languages=[{"text": "Английский B2", "fact_ids": []}])
        content["experience"] = [dict(self.content["experience"][0], scope={"text": "Команда 40 человек", "fact_ids": []})]
        saved = resume.save_resume(self.store, "me", content, expected_version=self.saved["version"])
        self.assertTrue(saved["validation"]["ok"], saved["validation"])
        resume.approve_resume(self.store, "me", "main", saved["version"])
        manifest = render.render_resume(self.store, "me", self.ws)
        self.assertTrue(manifest["text_extraction"]["pdf"]["ok"], manifest["text_extraction"]["pdf"])
        lines = [(k, t.replace("\u00a0", " ")) for k, t in render.blocks(content)]
        self.assertIn(("scope", "Команда 40 человек"), lines)
        self.assertIn(("contacts", "ivan@example.invalid · @ivan"), lines)

    def test_layout_of_a_real_shaped_resume(self):  # owner's report on 0.9.4.7: leaked id, name, «н.в.», A4
        fid = self.content["competencies"][0]["fact_ids"]

        def claim(text, employer):
            fact = resume.add_fact(self.store, "me", text, employer, {}, [])
            resume.set_fact_status(self.store, "me", fact["object_id"], "confirmed", fact["version"])
            return {"text": text, "fact_ids": [fact["object_id"]]}

        ip = {"employer": "ИП Учебный", "title": "Основатель, архитектор ИИ-решений", "start": "2019", "end": None}
        experience = [dict(ip, scope=claim(f"Проект {i}: автоматизация заявок", "ИП Учебный"),
                           bullets=[claim(f"Сократил время обработки заявок в проекте {i} с двух дней до двух часов "
                                          "за счёт ИИ-поиска по каталогам", "ИП Учебный") for _ in range(3)])
                      for i in range(5)]
        experience.append(dict(self.content["experience"][0], end="2019", start="2012"))
        content = dict(self.content, full_name=None, experience=experience,
                       contacts={"email": "ivan@example.invalid", "fact_ids": fid, "name": "Иван Учебный",
                                 "phone": "+7 900 000-00-00"},
                       education=["МГУ, 1991–1997"])
        content.pop("full_name")
        saved = resume.save_resume(self.store, "me", content, expected_version=self.saved["version"])
        self.assertTrue(saved["validation"]["ok"], saved["validation"])
        resume.approve_resume(self.store, "me", "main", saved["version"])
        manifest = render.render_resume(self.store, "me", self.ws)
        self.assertTrue(manifest["text_extraction"]["docx"]["ok"], manifest["text_extraction"]["docx"])
        self.assertTrue(manifest["text_extraction"]["pdf"]["ok"], manifest["text_extraction"]["pdf"])
        self.assertTrue(manifest["checks"]["a4"])
        self.assertEqual(manifest["checks"]["pdf_pages"], 2)  # one page break to check
        docx_path, pdf_path = (Path(f["path"]) for f in manifest["files"])

        doc = Document(docx_path)
        paras = [p for p in doc.paragraphs if p.text.strip()]
        for par in paras:
            par.text_plain = par.text.replace("\u00a0", " ")
        self.assertEqual((paras[0].text_plain, paras[0].style.name), ("Иван Учебный", "Title"))
        self.assertEqual(paras[2].text_plain, "+7 900 000-00-00 · ivan@example.invalid")
        text = "\n".join(p.text_plain for p in paras)
        self.assertNotIn("fact_", text)
        self.assertEqual(text.count("ИП Учебный"), 1)  # five projects under one entry
        self.assertIn("2019 — н.в.", text)
        self.assertIn("1991 — 1997", text)
        self.assertEqual(sum(p.style.name == "Heading 3" for p in paras), 5)
        self.assertEqual(round(doc.sections[0].page_width.mm), 210)
        self.assertEqual(round(doc.sections[0].left_margin.mm), render.MARGIN_MM)
        self.assertEqual(doc.core_properties.author, "Иван Учебный")
        self.assertEqual(doc.core_properties.language, "ru-RU")
        with zipfile.ZipFile(docx_path) as z:  # the font travels inside the DOCX
            fonts = [n for n in z.namelist() if n.startswith("word/fonts/")]
            self.assertEqual(len(fonts), 3)
            self.assertIn(f'w:name="{render.FONT}"', z.read("word/fontTable.xml").decode())
            key = re.search(r'w:fontKey="([^"]+)"', z.read("word/fontTable.xml").decode())[1]
            self.assertEqual(render._obfuscated(z.read(fonts[0]), key)[:4], b"\x00\x01\x00\x00")  # TrueType again
        for name in ("Title", "Heading 1", "Heading 2", "Heading 3"):
            style = doc.styles[name]
            self.assertEqual((style.font.name, str(style.font.color.rgb)), (render.DOCX_FONT, "000000"), name)
            fonts = style.element.rPr.find(qn("w:rFonts"))
            self.assertIsNone(fonts.get(qn("w:asciiTheme")), name)

        reader = PdfReader(pdf_path)
        self.assertEqual(reader.metadata.title, "Иван Учебный\u00a0— резюме")
        self.assertEqual(reader.metadata.author, "Иван Учебный")
        headings = {t for k, t in render.blocks(content) if k in render.KEEP_WITH_NEXT}
        for page in reader.pages[:-1]:  # no heading is the last line of a page
            last = [s for s in (page.extract_text() or "").splitlines() if s.strip()][-1]
            self.assertFalse(any(last.strip() and last.strip() in h for h in headings), last)

    def test_extraction_check_catches_lost_line(self):
        lines = render.blocks(self.content)
        result = render.extraction_check(lines, "Иван Учебный")
        self.assertFalse(result["ok"])
        self.assertIn("Коммерческий директор, B2B-дистрибуция", [m.replace("\u00a0", " ") for m in result["missing"]])

    def test_render_of_old_version_is_refused(self):
        approved = resume.approve_resume(self.store, "me", "main", self.saved["version"])
        manifest = {"content_hash": "0" * 64, "files": [], "text_extraction": {}}
        with self.assertRaises(DomainError) as ctx:
            resume.record_render(self.store, "me", "main", manifest)
        self.assertEqual(ctx.exception.code, "stale_inputs")
        self.assertEqual(approved["status"], "approved")


if __name__ == "__main__":
    unittest.main()
