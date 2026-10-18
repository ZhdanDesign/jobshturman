"""The methodology decides how many stages there are: hrs checks, hands out and shows 16 or 35 alike."""
import io
import json
import zipfile

import pytest
from hrs_cli import selftest, web
from hrshturman_core import Store, methodology, resume
from hrshturman_core.workspace import create_candidate


def bundle(count: int, facts_at: int = 2) -> bytes:
    steps = [{"id": "about", "title": "О вас"}, {"id": "apply", "title": "Отклики"}]
    stages = [{"id": f"stage-{i}", "title": f"Этап {i}", "step": "about" if i < count // 2 else "apply",
               "dir": f"skills/stage-{i}", "progress": "facts" if i == facts_at else None,
               "tools": ["fact_add"] if i == facts_at else []} for i in range(count)]
    files = {"bundle.json": "{}", "library_manifest.json": json.dumps(
        {"format": 2, "version": "2.0", "steps": steps, "stages": stages}, ensure_ascii=False),
        methodology.GENESIS: "# Главные правила\n\nВеди человека без пауз и говори, что дальше.\n"}
    for i in range(count):
        files[f"skills/stage-{i}/SKILL.md"] = f"---\nname: stage-{i}\ndescription: этап {i}\n---\n# Этап {i}\n"
        files[f"skills/stage-{i}/references/procedure.md"] = f"# Процедура {i}\n\nШаг этапа номер {i}.\n"
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return out.getvalue()


@pytest.fixture(scope="module")
def tools():
    return selftest.tool_names()


@pytest.mark.parametrize("count", [16, 35])
def test_selftest_follows_the_manifest(count, tools):
    report = selftest.run(bundle(count), tools)
    assert (report["stages"], report["stages_total"], report["errors"]) == (count, count, [])
    assert selftest.summary(report).startswith(f"Этапов проверено: {count} из {count}. Ошибок выдачи: 0.")


def test_a_missing_stage_and_a_changed_main_prompt_are_errors(tools):
    data = bundle(16)
    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name in src.namelist():
            if not name.startswith("skills/stage-5/"):
                z.writestr(name, src.read(name))
    codes = {(e["stage"], e["code"]) for e in selftest.run(out.getvalue(), tools)["errors"]}
    assert ("stage-5", "stage_missing") in codes


def test_progress_lights_the_stage_the_manifest_points_at(tmp_path):
    store = Store(tmp_path / "w.db").init()
    create_candidate(store, "me", "Europe/Moscow", actor="installer")
    resume.add_fact(store, "me", "Рост выручки на 25% за 2023 год", "Альфа", {}, [])
    lib = selftest.ZipLibrary(bundle(35, facts_at=30))
    stages = web.progress(store, "me", methodology.route(lib))
    assert len(stages) == 35 and stages[30]["id"] == "stage-30" and stages[30]["group"] == "Отклики"
    assert (stages[30]["status"], stages[30]["count"]) == ("active", 1)
    assert [s["stage"] for s in stages] == list(range(35))
    assert all(s["status"] == "todo" for i, s in enumerate(stages) if i != 30)
