"""Stage 07 step 6: render the approved master CV to DOCX and PDF and check text extraction.

Only an approved version is rendered, so every line in the files is backed by
confirmed facts. After writing, each file is read back and every expected line
is searched in the extracted text. Visual layout is not judged here: it stays
`not_verified` until a person looks at the file (stage 07 step 7).
"""
import hashlib
from pathlib import Path

from docx import Document
from docx.shared import Pt
from fpdf import FPDF
from hrshturman_core import DomainError
from hrshturman_core import resume as resume_mod
from hrshturman_core.common import digest
from hrshturman_core.render_layout import blocks, extraction_check
from pypdf import PdfReader

FONT_DIR = Path(__file__).resolve().parent / "fonts"
FONTS = {"": FONT_DIR / "NotoSans-Regular.ttf", "B": FONT_DIR / "NotoSans-Bold.ttf"}
RENDERER = "python-docx + fpdf2, Noto Sans (bundled)"


def render_docx(lines, path):
    doc = Document()
    doc.styles["Normal"].font.name = "Arial"
    doc.styles["Normal"].font.size = Pt(10.5)
    for kind, text in lines:
        if kind == "name":
            doc.add_heading(text, level=0)
        elif kind == "section":
            doc.add_heading(text, level=1)
        elif kind == "title":
            doc.add_paragraph().add_run(text).bold = True
        elif kind == "role":
            doc.add_heading(text, level=2)
        elif kind == "bullet":
            doc.add_paragraph(text, style="List Bullet")
        else:
            doc.add_paragraph(text)
    doc.save(path)


def render_pdf(lines, path):
    for style, font in FONTS.items():
        if not font.is_file():
            raise DomainError("unsupported_environment", f"Font not found: {font}")
    pdf = FPDF(format="A4")
    pdf.set_margins(18, 16, 18)
    pdf.set_auto_page_break(True, margin=16)
    for style, font in FONTS.items():
        pdf.add_font("Arial", style, str(font))
    pdf.add_page()
    sizes = {"name": (20, "B"), "title": (13, "B"), "section": (13, "B"), "role": (11.5, "B"),
             "meta": (10, ""), "para": (10.5, ""), "bullet": (10.5, "")}
    for kind, text in lines:
        size, style = sizes[kind]
        if kind == "section":
            pdf.ln(3)
        elif kind == "role":
            pdf.ln(1.5)
        pdf.set_font("Arial", style, size)
        pdf.multi_cell(0, size * 0.5, f"• {text}" if kind == "bullet" else text, align="L",
                       new_x="LMARGIN", new_y="NEXT")
        if kind in ("name", "section"):
            pdf.ln(1.5)
    pdf.output(str(path))


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def render_resume(store, candidate_id, workspace, track_id="main"):
    """Render the approved resume of a track and record the manifest on it."""
    cv = resume_mod.get_resume(store, candidate_id, track_id)
    if cv["status"] != "approved":
        raise DomainError("stale_inputs", "Only an approved resume is rendered; approve it first")
    content_hash = digest(cv["content"])
    if content_hash != cv["approved_hash"]:
        raise DomainError("stale_inputs", "Resume changed after approval")
    lines = blocks(cv["content"])
    out_dir = Path(workspace) / "materials" / candidate_id / track_id
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"master-cv-{content_hash[:8]}"
    docx_path, pdf_path = out_dir / f"{stem}.docx", out_dir / f"{stem}.pdf"
    render_docx(lines, docx_path)
    render_pdf(lines, pdf_path)
    docx_text = "\n".join(p.text for p in Document(docx_path).paragraphs)
    pdf_text = "\n".join(page.extract_text() or "" for page in PdfReader(pdf_path).pages)
    manifest = {
        "content_hash": content_hash,
        "renderer": RENDERER,
        "files": [{"name": p.name, "path": str(p), "sha256": _sha256(p), "format": p.suffix[1:]}
                  for p in (docx_path, pdf_path)],
        "text_extraction": {"docx": extraction_check(lines, docx_text), "pdf": extraction_check(lines, pdf_text)},
        "layout": "not_verified",
    }
    resume_mod.record_render(store, candidate_id, track_id, manifest)
    return manifest
