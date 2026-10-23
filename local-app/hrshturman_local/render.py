"""Stage 07 step 6: render the approved master CV to DOCX and PDF and check text extraction.

Only an approved version is rendered, so every line in the files is backed by
confirmed facts. After writing, each file is read back and every expected line
is searched in the extracted text. Visual layout is not judged here: it stays
`not_verified` until a person looks at the file (stage 07 step 7).
"""
import hashlib
import uuid
from pathlib import Path

from docx import Document
from docx.enum.text import WD_TAB_ALIGNMENT
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.opc.packuri import PackURI
from docx.opc.part import Part
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import qn
from docx.shared import Mm, Pt, RGBColor
from fpdf import FPDF
from hrshturman_core import DomainError
from hrshturman_core import resume as resume_mod
from hrshturman_core.brand import PRODUCT_NAME
from hrshturman_core.common import digest
from hrshturman_core.render_layout import DASH, NBSP, blocks, extraction_check
from lxml import etree
from pypdf import PdfReader

FONT_DIR = Path(__file__).resolve().parent / "fonts"
# One font for both files: drawn into the PDF and embedded into the DOCX, so Word and LibreOffice
# show it without installing anything.
FONT = "IBM Plex Sans"
FONTS = {"": FONT_DIR / "IBMPlexSans-Regular.ttf", "B": FONT_DIR / "IBMPlexSans-Bold.ttf",
         "I": FONT_DIR / "IBMPlexSans-Italic.ttf"}
RENDERER = f"python-docx + fpdf2, {FONT} (bundled, embedded in DOCX), A4"
DOCX_FONT = FONT
MARGIN_MM = 18
PAGE_MM = (210, 297)
TEXT_MM = PAGE_MM[0] - 2 * MARGIN_MM
BULLET_INDENT_MM = 4.5
LINE = 1.2            # line height / font size
MAX_PAGES = 3
GREY = (0x44, 0x44, 0x44)
# One style table for both files: size pt, style (B bold, I italic), space before/after pt.
STYLES = {
    "name": (22, "B", 0, 2), "title": (12.5, "", 0, 2), "contacts": (10, "", 0, 4),
    "section": (12, "B", 11, 4), "role": (11, "B", 7, 1), "dates": (10, "", 0, 2),
    "project": (10.5, "B", 5, 1), "scope": (10.5, "I", 0, 2),
    "bullet": (10.5, "", 0, 2), "line": (10.5, "", 0, 2),
}
KEEP_WITH_NEXT = {"name", "title", "section", "role", "dates", "project", "scope"}
PT_MM = 25.4 / 72


def _language(content):
    return "ru-RU" if (content.get("language") or "ru") == "ru" else "en-US"


def _pdf():
    for font in FONTS.values():
        if not font.is_file():
            raise DomainError("unsupported_environment", f"Font not found: {font}")
    pdf = FPDF(format="A4")
    pdf.set_margins(MARGIN_MM, MARGIN_MM, MARGIN_MM)
    pdf.set_auto_page_break(True, margin=MARGIN_MM)
    for style, font in FONTS.items():
        pdf.add_font(FONT, style, str(font))
    return pdf


def contacts_size(lines):
    """The contact line stays on one line: shrink it (not below 8.5 pt) until it fits the text width."""
    text = next((t for k, t in lines if k == "contacts"), "")
    pdf, size = _pdf(), STYLES["contacts"][0]
    while size > 8.5:
        pdf.set_font(FONT, "", size)
        if pdf.get_string_width(text) <= TEXT_MM:
            break
        size -= 0.5
    return size


def _with_dates(lines):
    """(kind, text, dates): a role line carries its period when both fit on one line (dates on the right).

    A longer role keeps its own lines and the period follows as a separate grey line, so the
    dates never land next to the last wrapped line of the heading.
    """
    pdf = _pdf()
    out, i = [], 0
    while i < len(lines):
        kind, text = lines[i]
        if kind == "role" and i + 1 < len(lines) and lines[i + 1][0] == "dates":
            dates = lines[i + 1][1]
            pdf.set_font(FONT, STYLES["role"][1], STYLES["role"][0])
            role_w = pdf.get_string_width(text)
            pdf.set_font(FONT, "", STYLES["dates"][0])
            if role_w + pdf.get_string_width(dates) + 4 <= TEXT_MM:
                out.append((kind, text, dates))
                i += 2
                continue
        out.append((kind, text, ""))
        i += 1
    return out


# --- DOCX ---

def _obfuscated(font_bytes, key):
    """ECMA-376 Part 1, 17.8.1: the first 32 bytes XOR-ed with the font key read backwards."""
    hexkey = key.strip("{}").replace("-", "")
    xor = bytes(int(hexkey[i:i + 2], 16) for i in range(30, -1, -2))
    head = bytes(b ^ xor[i % 16] for i, b in enumerate(font_bytes[:32]))
    return head + font_bytes[32:]


def _embed_fonts(doc):
    """Put the CV font into the DOCX (word/fonts/*.odttf) the way Word's «embed fonts» does."""
    table = next(r.target_part for r in doc.part.rels.values() if r.reltype == RT.FONT_TABLE)
    root = parse_xml(table.blob)
    for old in root.findall(qn("w:font")):
        if old.get(qn("w:name")) == FONT:
            root.remove(old)
    font = OxmlElement("w:font")
    font.set(qn("w:name"), FONT)
    for tag, val in (("w:charset", "CC"), ("w:family", "swiss"), ("w:pitch", "variable")):
        el = OxmlElement(tag)
        el.set(qn("w:val"), val)
        font.append(el)
    for i, (style, tag) in enumerate((("", "w:embedRegular"), ("B", "w:embedBold"), ("I", "w:embedItalic")), 1):
        key = "{" + str(uuid.uuid4()).upper() + "}"
        part = Part(PackURI(f"/word/fonts/font{i}.odttf"),
                    "application/vnd.openxmlformats-officedocument.obfuscatedFont",
                    _obfuscated(FONTS[style].read_bytes(), key), doc.part.package)
        el = OxmlElement(tag)
        el.set(qn("r:id"), table.relate_to(part, RT.FONT))
        el.set(qn("w:fontKey"), key)
        font.append(el)
    root.append(font)
    table._blob = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    settings = doc.settings.element
    if settings.find(qn("w:embedTrueTypeFonts")) is None:
        settings.insert(0, OxmlElement("w:embedTrueTypeFonts"))


def _plain_style(style, size, bold=False, italic=False, color=(0, 0, 0)):
    """Black text in the one document font: drop the template's theme fonts, colours and borders."""
    font = style.font
    font.name, font.size, font.bold, font.italic = DOCX_FONT, Pt(size), bold, italic
    font.color.rgb = RGBColor(*color)
    rpr = style.element.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
        fonts.attrib.pop(qn(attr), None)
    for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        fonts.set(qn(attr), DOCX_FONT)
    for extra in rpr.findall(qn("w:spacing")) + rpr.findall(qn("w:kern")):  # Subtitle/Title letter spacing
        rpr.remove(extra)
    ppr = style.element.get_or_add_pPr()
    for extra in ppr.findall(qn("w:pBdr")) + ppr.findall(qn("w:contextualSpacing")):  # borders, list spacing
        ppr.remove(extra)
    style.paragraph_format.line_spacing = Pt(size * LINE)  # exact, as in the PDF


def _set_lang(style, lang):
    rpr = style.element.get_or_add_rPr()
    for old in rpr.findall(qn("w:lang")):
        rpr.remove(old)
    el = OxmlElement("w:lang")
    for attr in ("w:val", "w:eastAsia", "w:bidi"):
        el.set(qn(attr), lang)
    rpr.append(el)


def _bottom_rule(style):
    ppr = style.element.get_or_add_pPr()
    bdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    for attr, value in (("w:val", "single"), ("w:sz", "4"), ("w:space", "1"), ("w:color", "000000")):
        bottom.set(qn(attr), value)
    bdr.append(bottom)
    ppr.append(bdr)


def _docx_styles(doc, lang):
    styles = doc.styles
    normal = styles["Normal"]
    _plain_style(normal, STYLES["line"][0])
    _set_lang(normal, lang)
    normal.paragraph_format.widow_control = True
    normal.paragraph_format.space_after = Pt(STYLES["line"][3])
    mapping = {"name": "Title", "title": "Subtitle", "section": "Heading 1", "role": "Heading 2",
               "project": "Heading 3", "bullet": "List Bullet"}
    for kind, name in mapping.items():
        size, style, before, after = STYLES[kind]
        st = styles[name]
        _plain_style(st, size, bold="B" in style, italic="I" in style)
        _set_lang(st, lang)
        fmt = st.paragraph_format
        fmt.space_before, fmt.space_after = Pt(before), Pt(after)
        fmt.keep_with_next = kind in KEEP_WITH_NEXT
        fmt.keep_together = True
    styles["Heading 1"].font.all_caps = True
    _bottom_rule(styles["Heading 1"])
    bullet = styles["List Bullet"].paragraph_format
    bullet.left_indent, bullet.first_line_indent = Mm(BULLET_INDENT_MM), Mm(-BULLET_INDENT_MM)


def render_docx(lines, path, content=None, contacts_pt=None):
    content = content or {}
    doc = Document()
    section = doc.sections[0]
    section.page_width, section.page_height = Mm(PAGE_MM[0]), Mm(PAGE_MM[1])
    for side in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
        setattr(section, side, Mm(MARGIN_MM))
    _docx_styles(doc, _language(content))
    name = next((t for k, t in lines if k == "name"), "").replace(NBSP, " ")  # metadata: plain spaces
    for kind, text, dates in _with_dates(lines):
        size, style, before, after = STYLES[kind]
        if kind == "name":
            doc.add_paragraph(text, style="Title")
        elif kind == "title":
            doc.add_paragraph(text, style="Subtitle")
        elif kind == "section":
            doc.add_paragraph(text, style="Heading 1")
        elif kind == "role":
            par = doc.add_paragraph(text, style="Heading 2")
            if dates:
                par.paragraph_format.tab_stops.add_tab_stop(Mm(TEXT_MM), WD_TAB_ALIGNMENT.RIGHT)
                run = par.add_run(f"\t{dates}")
                run.bold, run.font.size, run.font.color.rgb = False, Pt(STYLES["dates"][0]), RGBColor(*GREY)
        elif kind == "dates":
            par = doc.add_paragraph()
            run = par.add_run(text)
            run.font.size, run.font.color.rgb = Pt(size), RGBColor(*GREY)
            par.paragraph_format.space_after = Pt(after)
            par.paragraph_format.line_spacing = Pt(size * LINE)
            par.paragraph_format.keep_with_next = True
        elif kind == "project":
            doc.add_paragraph(text, style="Heading 3")
        elif kind == "bullet":
            doc.add_paragraph(text, style="List Bullet")
        else:
            par = doc.add_paragraph()
            run = par.add_run(text)
            run.italic = "I" in style
            run.font.size = Pt(contacts_pt if kind == "contacts" and contacts_pt else size)
            fmt = par.paragraph_format
            fmt.line_spacing = Pt(run.font.size.pt * LINE)
            fmt.space_before, fmt.space_after = Pt(before), Pt(after)
            fmt.keep_with_next = kind in KEEP_WITH_NEXT
    _embed_fonts(doc)
    props = doc.core_properties
    props.author = props.last_modified_by = name
    props.title = f"{name}{DASH}резюме" if name else "Резюме"
    props.language, props.comments, props.keywords = _language(content), "", ""
    doc.save(path)


# --- PDF ---

def _height(pdf, kind, text, dates=""):
    size, style, before, after = STYLES[kind]
    pdf.set_font(FONT, style, size)
    width = TEXT_MM - (BULLET_INDENT_MM if kind == "bullet" else 0)
    if dates:
        width -= pdf.get_string_width(dates) + 4
    lines = pdf.multi_cell(width, size * PT_MM * LINE, text, dry_run=True, output="LINES")
    return (before + after) * PT_MM + len(lines) * size * PT_MM * LINE


def render_pdf(lines, path, content=None, contacts_pt=None):
    content = content or {}
    pdf = _pdf()
    name = next((t for k, t in lines if k == "name"), "").replace(NBSP, " ")  # metadata: plain spaces
    pdf.set_title(f"{name}{DASH}резюме" if name else "Резюме")
    pdf.set_author(name)
    pdf.set_lang(_language(content))
    pdf.set_creator(PRODUCT_NAME)
    pdf.set_producer(PRODUCT_NAME)
    pdf.add_page()
    items = _with_dates(lines)
    bottom = PAGE_MM[1] - MARGIN_MM
    for i, (kind, text, dates) in enumerate(items):
        size, style, before, after = STYLES[kind]
        if kind == "contacts" and contacts_pt:
            size = contacts_pt
        need = _height(pdf, kind, text, dates)
        if kind in KEEP_WITH_NEXT and i + 1 < len(items):  # a heading never stays alone at the page end
            need += _height(pdf, *items[i + 1])
        if pdf.get_y() > pdf.t_margin + 1 and pdf.get_y() + need > bottom:
            pdf.add_page()
        elif pdf.get_y() > pdf.t_margin + 1:
            pdf.ln(before * PT_MM)
        h = size * PT_MM * LINE
        pdf.set_text_color(*(GREY if kind == "dates" else (0, 0, 0)))
        pdf.set_font(FONT, style, size)
        if kind == "bullet":
            pdf.set_x(pdf.l_margin + 1)
            pdf.cell(BULLET_INDENT_MM - 1, h, "•")
            pdf.multi_cell(TEXT_MM - BULLET_INDENT_MM, h, text, align="L", new_x="LMARGIN", new_y="NEXT")
        elif kind == "role" and dates:
            y = pdf.get_y()
            pdf.set_font(FONT, "", STYLES["dates"][0])
            dates_w = pdf.get_string_width(dates)
            pdf.set_text_color(*GREY)
            pdf.set_xy(pdf.l_margin + TEXT_MM - dates_w, y)
            pdf.cell(dates_w, h, dates, align="R")
            pdf.set_text_color(0, 0, 0)
            pdf.set_font(FONT, style, size)
            pdf.set_xy(pdf.l_margin, y)
            pdf.multi_cell(TEXT_MM - dates_w - 4, h, text, align="L", new_x="LMARGIN", new_y="NEXT")
        else:
            shown = text.upper() if kind == "section" else text
            pdf.multi_cell(TEXT_MM, h, shown, align="L", new_x="LMARGIN", new_y="NEXT")
        if kind == "section":
            pdf.set_line_width(0.25)
            pdf.line(pdf.l_margin, pdf.get_y() + 0.6, pdf.l_margin + TEXT_MM, pdf.get_y() + 0.6)
            pdf.ln(0.6)
        pdf.ln(after * PT_MM)
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
    contacts_pt = contacts_size(lines)
    render_docx(lines, docx_path, cv["content"], contacts_pt)
    render_pdf(lines, pdf_path, cv["content"], contacts_pt)
    docx = Document(docx_path)
    docx_text = "\n".join(p.text for p in docx.paragraphs)
    pdf_pages = PdfReader(pdf_path).pages
    pdf_text = "\n".join(page.extract_text() or "" for page in pdf_pages)
    sizes = {"docx": [round(docx.sections[0].page_width.mm), round(docx.sections[0].page_height.mm)],
             "pdf": [round(float(pdf_pages[0].mediabox.width) * PT_MM), round(float(pdf_pages[0].mediabox.height) * PT_MM)]}
    manifest = {
        "content_hash": content_hash,
        "renderer": RENDERER,
        "files": [{"name": p.name, "path": str(p), "sha256": _sha256(p), "format": p.suffix[1:]}
                  for p in (docx_path, pdf_path)],
        "text_extraction": {"docx": extraction_check(lines, docx_text), "pdf": extraction_check(lines, pdf_text)},
        "layout": "not_verified",
        "checks": {
            "a4": all(size == list(PAGE_MM) for size in sizes.values()),
            "pdf_pages": len(pdf_pages),
            "warnings": [f"PDF: {len(pdf_pages)} стр., больше {MAX_PAGES}: сократите резюме"]
            if len(pdf_pages) > MAX_PAGES else [],
        },
    }
    resume_mod.record_render(store, candidate_id, track_id, manifest)
    return manifest
