"""`hrs collect …`: the command line over the collector, without the network."""
import json

import pytest
from hrs_collector import cli


@pytest.fixture
def run(tmp_path, capsys):
    def go(*args):
        code = cli.main(["--data", str(tmp_path / "data"), "--candidate", "me", *args])
        return code, capsys.readouterr().out
    return go


def test_sources_are_added_listed_and_switched(run):
    code, out = run("add-hh", "hh-cfo", "--text", "CFO", "--area", "1")
    assert code == 0 and json.loads(out)["spec"]["config"] == {"text": "CFO", "area": "1"}
    assert run("add-linkedin", "li-cfo", "--keywords", "CFO")[0] == 0
    assert run("add-telegram", "tg-jobs", "--username", "@cfojobs")[0] == 0
    code, out = run("sources", "--json")
    sources = {s["source_id"]: s for s in json.loads(out)}
    assert code == 0 and set(sources) == {"hh-cfo", "li-cfo", "tg-jobs"}
    assert sources["tg-jobs"]["spec"]["config"]["username"] == "cfojobs"
    assert run("disable", "tg-jobs")[0] == 0
    _, out = run("sources", "--json")
    assert next(s for s in json.loads(out) if s["source_id"] == "tg-jobs")["enabled"] is False


def test_the_channel_registry_comes_from_a_csv_the_person_names(run, tmp_path):
    csv = tmp_path / "registry.csv"
    csv.write_text("source_id,current_username,current_name,included_in_search\n"
                   "tg-1,@finjobs,Финансовые вакансии,да\n"
                   "tg-2,,Закрытый канал,нет\n", encoding="utf-8")
    code, out = run("import-telegram", "--csv", str(csv))
    assert code == 0 and json.loads(out) == {"created": 2, "updated": 0, "total": 2, "csv": str(csv)}
    code, out = run("import-telegram", "--csv", str(csv))
    assert json.loads(out)["updated"] == 2  # a re-import keeps the rows, nothing is duplicated
    _, out = run("sources", "--json")
    by_id = {s["source_id"]: s for s in json.loads(out)}
    assert by_id["tg-1"]["enabled"] and by_id["tg-1"]["spec"]["transport"] == "telegram_web"
    assert not by_id["tg-2"]["enabled"] and by_id["tg-2"]["spec"]["transport"] == "telegram_mtproto"
    with pytest.raises(SystemExit):  # the registry is not part of the program: --csv is required
        run("import-telegram")


def test_an_unknown_vacancy_is_reported_not_raised(run):
    code, _ = run("show", "vac_nothing")
    assert code != 0
