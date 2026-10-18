"""Candidate documents (PDF, Word, …) converted to Markdown with MarkItDown, kept locally.

Mirrors web-app/src/hrs_web/documents.py: the original and its Markdown are stored under one
source_id, so every fact the agent extracts can point back to the exact file (source_refs).
"""
import hashlib
from pathlib import Path

from hrshturman_core import DomainError
from markitdown import MarkItDown, StreamInfo

MAX_BYTES = 10 * 1024 * 1024
ALLOWED = {".pdf", ".docx", ".pptx", ".xlsx", ".html", ".htm", ".txt", ".md", ".rtf"}


def import_document(path, documents: Path) -> dict:
    path = Path(path).expanduser()
    ext = path.suffix.lower()
    if ext == ".doc":
        raise DomainError("unsupported_format", "Старый формат .doc не поддерживается: сохраните файл как .docx или PDF")
    if ext not in ALLOWED:
        raise DomainError("unsupported_format", f"Формат {ext or 'без расширения'} не поддерживается: нужен PDF или Word (.docx)")
    if not path.is_file():
        raise DomainError("not_found", f"Файл не найден: {path}")
    data = path.read_bytes()
    if not data or len(data) > MAX_BYTES:
        raise DomainError("invalid_schema", "Файл пустой или больше 10 МБ")
    try:
        result = MarkItDown(enable_plugins=False).convert_local(str(path), stream_info=StreamInfo(extension=ext))
    except Exception as exc:
        raise DomainError("incomplete_extract", "Не удалось прочитать файл. Возможно, он защищён паролем или повреждён") from exc
    markdown = (result.text_content or "").strip()
    if not markdown:
        raise DomainError("incomplete_extract", "В файле нет текстового слоя (похоже на скан). Нужен PDF с текстом или Word")
    source_id = "file-" + hashlib.sha256(data).hexdigest()[:16]
    documents.mkdir(parents=True, exist_ok=True)
    (documents / f"{source_id}{ext}").write_bytes(data)
    md_path = documents / f"{source_id}.md"
    md_path.write_text(markdown, encoding="utf-8")
    return {"source_id": source_id, "filename": path.name, "markdown_path": str(md_path),
            "chars": len(markdown), "markdown": markdown}
