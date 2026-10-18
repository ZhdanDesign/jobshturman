"""The route comes from the manifest: any number of stages, stable ids, the old 26 still read."""
import json
import unittest

from hrshturman_core import DomainError, methodology


class DictLibrary:
    def __init__(self, files):
        self.files = files

    def names(self):
        return list(self.files)

    def read(self, name):
        return self.files[name]


def library(count: int, **overrides) -> DictLibrary:
    steps = [{"id": "start", "title": "Начало"}, {"id": "work", "title": "Работа"}]
    stages = [{"id": f"s{i}", "title": f"Этап {i}", "step": "start" if i < count // 2 else "work",
               "dir": f"skills/s{i}"} for i in range(count)]
    files = {f"skills/s{i}/SKILL.md": f"---\nname: s{i}\ndescription: этап {i}\n---\n# {i}\n" for i in range(count)}
    files |= {f"skills/s{i}/references/procedure.md": f"Процедура {i}" for i in range(0, count, 2)}
    manifest = {"format": 2, "version": "1.0", "steps": steps, "stages": stages} | overrides
    files["library_manifest.json"] = json.dumps(manifest, ensure_ascii=False)
    files[methodology.GENESIS] = "# Главные правила\n"
    return DictLibrary(files)


class RouteTest(unittest.TestCase):
    def test_any_number_of_stages(self):
        for count in (16, 26, 35):
            lib = library(count)
            route = methodology.route(lib)
            self.assertEqual([s.number for s in route.stages], list(range(count)))
            self.assertEqual(methodology.stage_text(lib, count - 1),
                             lib.files[f"skills/s{count - 1}/SKILL.md"] if (count - 1) % 2
                             else f"Процедура {count - 1}")
            self.assertEqual(methodology.stage_text(lib, "s2"), "Процедура 2")
            self.assertTrue(methodology.validate(lib.files)["ok"])
            with self.assertRaises(DomainError):
                methodology.stage_text(lib, count)

    def test_order_and_switched_off_stages(self):
        lib = library(4)
        manifest = json.loads(lib.files["library_manifest.json"])
        manifest["stages"] = [manifest["stages"][3], manifest["stages"][0], manifest["stages"][1] | {"status": "off"},
                              manifest["stages"][2]]
        lib.files["library_manifest.json"] = json.dumps(manifest)
        route = methodology.route(lib)
        self.assertEqual([(s.number, s.id) for s in route.stages], [(0, "s3"), (1, "s0"), (2, "s2")])
        self.assertEqual(methodology.stage_text(lib, 0), lib.files["skills/s3/SKILL.md"])

    def test_old_library_keeps_its_26_stages_and_numbers(self):
        files = {f"skills/{folder}/SKILL.md": f"# {folder}" for folder in methodology.LEGACY if folder[6:8] != "03"}
        lib = DictLibrary(files | {"library_manifest.json": json.dumps({"version": "0.4.0"})})
        route = methodology.route(lib)
        self.assertEqual(len(route.stages), 25)
        self.assertEqual(route.stage(7).id, "master-cv")
        self.assertEqual(route.stage("master-cv").title, "Основное резюме")
        self.assertEqual(route.step_title(route.stage(14).step), "Вакансии")
        self.assertEqual(len(methodology.legacy_route().stages), 26)

    def test_validate_names_each_fault(self):
        lib = library(3)
        manifest = json.loads(lib.files["library_manifest.json"])
        manifest["stages"] += [{"id": "s1", "title": "Дубль", "step": "start", "dir": "skills/s1"},
                               {"id": "New Stage", "title": "x", "step": "nowhere", "dir": "skills/new",
                                "progress": "magic", "tools": ["salary_magic"]}]
        files = dict(lib.files, **{"library_manifest.json": json.dumps(manifest)})
        del files[methodology.GENESIS]
        files["skills/s0/SKILL.md"] = "# без шапки\n\n[ссылка](missing.md)\n"
        report = methodology.validate(files, tools={"stage_procedure"})
        codes = {e["code"] for e in report["errors"]}
        self.assertEqual(codes, {"duplicate_id", "bad_id", "stage_missing", "unknown_step", "unknown_progress"})
        self.assertFalse(report["ok"])
        found = {f["code"] for f in report["findings"]}
        self.assertEqual(found, {"unknown_tool", "no_genesis", "front_matter", "broken_link"})

    def test_upgrade_writes_the_old_route_into_the_manifest(self):
        names = [f"skills/{folder}/SKILL.md" for folder in methodology.LEGACY]
        v1 = {"version": "0.4.0", "stages": [{"stage_id": "01", "entrypoint": "skills/stage-01-candidate-intake/SKILL.md"}]}
        v2 = methodology.upgrade(v1, names)
        self.assertEqual(v2["format"], 2)
        self.assertEqual(v2["legacy_stages"], v1["stages"])
        self.assertEqual([s["id"] for s in v2["stages"]], ["candidate-intake"])  # what the old manifest lists
        self.assertIs(methodology.upgrade(v2, names), v2)


if __name__ == "__main__":
    unittest.main()
