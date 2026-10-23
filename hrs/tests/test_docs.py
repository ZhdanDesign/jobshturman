"""`jbs doc import`: a candidate's file becomes Markdown next to the original, every refusal is a DomainError."""
import pytest
from hrs_cli import docs, main
from hrshturman_core import DomainError


def test_a_text_file_is_imported_under_its_hash(tmp_path, capsys):
    source = tmp_path / "Резюме.md"
    source.write_text("# Иван Петров\n\nФинансовый директор, 12 лет.\n", encoding="utf-8")
    out = docs.import_document(source, tmp_path / "documents")
    assert out["source_id"].startswith("file-") and out["filename"] == "Резюме.md"
    assert (tmp_path / "documents" / f"{out['source_id']}.md").read_text(encoding="utf-8").startswith("# Иван Петров")
    assert (tmp_path / "documents" / f"{out['source_id']}.md").exists()
    assert docs.import_document(source, tmp_path / "documents")["source_id"] == out["source_id"]  # same bytes, same id


def test_refusals_name_the_reason(tmp_path):
    for name, data, code in (("old.doc", b"x", "unsupported_format"), ("notes.xyz", b"x", "unsupported_format"),
                             ("empty.txt", b"", "invalid_schema")):
        path = tmp_path / name
        path.write_bytes(data)
        with pytest.raises(DomainError) as exc:
            docs.import_document(path, tmp_path / "documents")
        assert exc.value.code == code, name
    with pytest.raises(DomainError) as exc:
        docs.import_document(tmp_path / "missing.pdf", tmp_path / "documents")
    assert exc.value.code == "not_found"
    big = tmp_path / "big.txt"
    big.write_bytes(b"x" * (docs.MAX_BYTES + 1))
    with pytest.raises(DomainError) as exc:
        docs.import_document(big, tmp_path / "documents")
    assert exc.value.code == "invalid_schema"


def test_the_cli_prints_the_result_without_the_text(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HRSHTURMAN_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HRSHTURMAN_WORKSPACE", raising=False)
    source = tmp_path / "cv.txt"
    source.write_text("Опыт: 12 лет\n", encoding="utf-8")
    assert main.main(["doc", "import", str(source)]) == 0
    out = capsys.readouterr().out
    assert '"source_id"' in out and "Опыт" not in out
    assert main.main(["doc", "import", str(tmp_path / "none.pdf")]) == 1
    assert "not_found" in capsys.readouterr().out
