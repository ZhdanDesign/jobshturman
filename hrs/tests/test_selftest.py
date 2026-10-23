"""Contract test of the real methodology bundle: all 26 stages as hrs hands them out (no model)."""
import io
import json
import zipfile
from pathlib import Path

import pytest
from hrs_cli import main, selftest, watermark
from hrs_keys import bundle

LIBRARY = (Path(__file__).resolve().parents[2] / "MetaProject_Job_Search_—_Specifications_and_Library" / "ФИНАЛЬНОЕ"
           / "MD библиотека" / "Библиотека навыков")
CHANNELS = LIBRARY.parents[1] / "02 Полный реестр Telegram каналов.csv"
# The methodology lives in the private repository; without it next to the code these tests skip.
pytestmark = pytest.mark.skipif(not LIBRARY.is_dir(), reason="нет библиотеки методики рядом с кодом")


@pytest.fixture(scope="module")
def real():
    return bundle.build(LIBRARY, {"telegram_channels.csv": CHANNELS})[1]


@pytest.fixture(scope="module")
def tools():
    return selftest.tool_names()


def rewrite(data: bytes, change) -> bytes:
    """A copy of the bundle with files replaced (None removes)."""
    files = {}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            files[name] = z.read(name)
    for name, content in change.items():
        if content is None:
            files.pop(name, None)
        else:
            files[name] = content.encode("utf-8") if isinstance(content, str) else content
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
    return out.getvalue()


def test_the_real_bundle_holds_its_contract(real, tools):
    report = selftest.run(real, tools)
    assert report["stages"] == 26
    assert report["errors"] == [], report["errors"]
    assert {"stage_procedure", "intake_ask", "license_status"} <= tools
    # Library findings are reported, never fixed by hrs (no_genesis: the main prompt comes with the editor).
    assert all(f["code"] in ("broken_link", "unknown_tool", "front_matter_fields", "no_genesis")
               for f in report["findings"])


def test_each_check_catches_its_fault(real, tools):
    stage7 = "skills/stage-07-master-cv/references/procedure.md"
    with zipfile.ZipFile(io.BytesIO(real)) as z:
        seven = z.read(stage7).decode("utf-8")
        three = z.read("skills/stage-03-experience-interview/references/procedure.md").decode("utf-8")
    own_paragraph = next(p for p in seven.split("\n\n") if len(p) > 80 and p.startswith("1."))
    broken = rewrite(real, {
        "skills/stage-05-competency-matrix/references/acceptance.md": "для нас",
        "skills/stage-03-experience-interview/references/procedure.md": three + "\n\n" + own_paragraph
        + "\n\nСм. [справочник](../../../references/missing.md) и `salary_magic(`.\n",
        "skills/stage-09-cover-letter-base/SKILL.md": "---\nname stage-09\n---\n# 09\n",
        "skills/stage-25-meeting-debrief/references/procedure.md": None,
        "skills/stage-25-meeting-debrief/SKILL.md": None,
    })
    report = selftest.run(broken, tools)
    codes = {(e["stage"], e["code"]) for e in report["errors"]}
    assert {(None, "forbidden_file"), (7, "foreign_text"), (9, "front_matter_broken"),
            (25, "stage_missing")} <= codes
    found = {(f["stage"], f["code"], f["detail"].split(" → ")[-1]) for f in report["findings"]}
    assert (3, "broken_link", "../../../references/missing.md") in found
    assert (3, "unknown_tool", "salary_magic") in found


def test_tables_and_code_stay_byte_for_byte(real, tools):
    table = "| Поле | Смысл |\n|---|---|\n| salary | зарплата |\n"
    code = "```\nhrs stage 7\n```\n\n    indented code line\n"
    text = f"# Таблица\n\nАбзац перед таблицей.\n\n{table}\n{code}\nАбзац после.\n"
    marked = watermark.mark(text, "0badc0de")
    assert table in marked and "    indented code line" in marked and "```\nhrs stage 7\n```" in marked
    assert selftest.verbatim(marked) == selftest.verbatim(text) and selftest.plain(marked) == text
    stage = "skills/stage-11-candidate-knowledge-base/references/procedure.md"
    with zipfile.ZipFile(io.BytesIO(real)) as z:
        source = z.read(stage).decode("utf-8")
    assert selftest.run(rewrite(real, {stage: source + "\n" + text}), tools)["errors"] == []


def test_cli(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HRSHTURMAN_HOME", str(tmp_path / "home"))
    assert main.main(["selftest", "--library", str(LIBRARY)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Этапов проверено: 26 из 26. Ошибок выдачи: 0.")
    assert main.main(["selftest"]) == 1  # no key, no bundle on this computer
    assert json.loads(capsys.readouterr().out)["error"] == "no_bundle"
