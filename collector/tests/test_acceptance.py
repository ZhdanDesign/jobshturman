"""Acceptance scenarios from references/target_channels.md §8 and ТЗ §13, against the real
engine + store with an in-memory channel."""

from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import T0, FakeChannel
from hrs_collector.errors import SourceError
from hrs_collector.models import CollectParams, RunRequest, SourceIn, SourceSpec


def add_channel(collector, source_id: str, username: str, candidate: str = "default", enabled: bool = True):
    spec = SourceSpec(platform="telegram", transport="telegram_web", kind="channel", config={"username": username})
    collector.store.upsert_source(candidate, SourceIn(source_id=source_id, spec=spec, enabled=enabled), keep_user_state=False)


def run(collector, now=T0, candidate="default", **kw):
    params = CollectParams(now=now, **{k: kw.pop(k) for k in list(kw) if k in CollectParams.model_fields})
    return collector.run(RunRequest(candidate_id=candidate, params=params, **kw))


def stored(collector, candidate="default", source_id=None):
    items, _ = collector.store.search_vacancies(candidate, source_id=source_id, include_duplicates=True, limit=500)
    return items


def test_first_run_reads_lookback_window_only(collector, fake_adapters):
    # 300 posts, one per hour, newest at T0.
    fake_adapters["ch"] = FakeChannel(300, T0 - timedelta(hours=299), timedelta(hours=1))
    add_channel(collector, "TG-1", "ch")
    r = run(collector, lookback_days=7, page_budget=50)
    rep = r.sources[0]
    assert rep.status == "ok" and rep.coverage.complete
    in_window = [v for v in stored(collector) if v.published_at >= T0 - timedelta(days=7)]
    assert len(in_window) == 7 * 24 + 1
    src = collector.store.get_source("default", "TG-1")
    assert src.cursor.watermark_at == T0 and src.cursor.resume_token is None
    assert src.access_status == "accessible" and src.stable_chat_id == "777"


def test_more_than_200_posts_with_small_budget_is_partial_then_completes(collector, fake_adapters):
    fake_adapters["ch"] = FakeChannel(400, T0 - timedelta(minutes=399 * 20), timedelta(minutes=20))
    add_channel(collector, "TG-1", "ch")
    r1 = run(collector, lookback_days=7, page_budget=5)
    assert r1.sources[0].status == "partial" and not r1.sources[0].coverage.complete
    src = collector.store.get_source("default", "TG-1")
    assert src.cursor.resume_token is not None and src.cursor.watermark_at is None
    for _ in range(5):
        r = run(collector, lookback_days=7, page_budget=5)
        if r.sources[0].status in ("ok", "no_new"):
            break
    src = collector.store.get_source("default", "TG-1")
    assert src.cursor.resume_token is None and src.cursor.watermark_at == T0
    keys = [v.item_key for v in stored(collector)]
    assert len(keys) == len(set(keys)) == 400  # 400*20min < 7 days: whole channel, nothing duplicated


def test_crash_after_page_commit_loses_and_duplicates_nothing(collector, fake_adapters):
    ch = fake_adapters["ch"] = FakeChannel(100, T0 - timedelta(hours=99), timedelta(hours=1))
    add_channel(collector, "TG-1", "ch")
    ch.fail_on_read = 3  # pages 1-2 saved, then network dies
    r1 = run(collector, lookback_days=30, page_budget=50)
    assert r1.sources[0].status == "partial" and r1.sources[0].error.code == "service_unreachable"
    assert len(stored(collector)) == 40
    ch.fail_on_read = None
    run(collector, lookback_days=30, page_budget=50, force=True)
    keys = [v.item_key for v in stored(collector)]
    assert len(keys) == len(set(keys)) == 100


def test_five_day_idle_is_closed_from_watermark_not_last_72h(collector, fake_adapters):
    ch = fake_adapters["ch"] = FakeChannel(10, T0 - timedelta(hours=9), timedelta(hours=1))
    add_channel(collector, "TG-1", "ch")
    run(collector, lookback_days=7)
    for h in range(1, 5 * 24 + 1):  # 120 new posts over five idle days
        ch.add(T0 + timedelta(hours=h), f"Вакансия поздняя {h}: требования")
    r = run(collector, now=T0 + timedelta(days=5), lookback_days=7, overlap_hours=72, page_budget=50)
    assert r.sources[0].items_new == 120
    assert len(stored(collector)) == 130


def test_edited_post_gets_new_version_and_keeps_old(collector, fake_adapters):
    ch = fake_adapters["ch"] = FakeChannel(5, T0 - timedelta(hours=4), timedelta(hours=1))
    add_channel(collector, "TG-1", "ch")
    run(collector)
    ch.messages[3].text = "Вакансия 3: требования изменены, зарплата выше"
    r = run(collector, now=T0 + timedelta(hours=1))
    assert r.sources[0].items_updated == 1
    vac = next(v for v in stored(collector) if v.external_id == "3")
    details = collector.store.vacancy_details("default", vac.vacancy_id)
    assert vac.version == 2 and [x["version"] for x in details["versions"]] == [1, 2]


def test_same_first_140_chars_stay_separate(collector, fake_adapters):
    head = "Ищем финансового директора в крупный холдинг. Требования: опыт от 10 лет, управление командой, " * 2
    fake_adapters["ch"] = FakeChannel(2, T0 - timedelta(hours=1), timedelta(hours=1),
                                      text=lambda i: head + ("Москва, офис" if i == 1 else "Дубай, релокация"))
    add_channel(collector, "TG-1", "ch")
    run(collector)
    items = stored(collector)
    assert len(items) == 2 and {v.duplicate_status for v in items} == {"unique"}


def test_repost_in_three_channels_links_to_one_canonical_and_keeps_all(collector, fake_adapters):
    text = lambda i: "Вакансия: COO в ритейл. Требования: P&L от 5 млрд. Резюме: hr@example.com"
    for n, name in enumerate(["a", "b", "c"]):
        fake_adapters[name] = FakeChannel(1, T0 - timedelta(hours=n), timedelta(hours=1), text=text)
        add_channel(collector, f"TG-{name}", name)
    run(collector)
    items = stored(collector)
    assert len(items) == 3
    canon = {v.canonical_id for v in items}
    assert len(canon) == 1
    assert sorted(v.duplicate_status for v in items) == ["duplicate", "duplicate", "unique"]
    visible, _ = collector.store.search_vacancies("default")
    assert len(visible) == 1  # duplicates hidden by default, not deleted


def test_disabled_source_is_not_read(collector, fake_adapters):
    ch = fake_adapters["ch"] = FakeChannel(5, T0, timedelta(hours=1))
    add_channel(collector, "TG-1", "ch", enabled=False)
    r = run(collector)
    assert r.sources[0].status == "skipped" and ch.reads == 0
    assert collector.store.get_source("default", "TG-1").access_status == "disabled"


def test_failing_source_does_not_block_others_and_backs_off(collector, fake_adapters):
    fake_adapters["ok"] = FakeChannel(3, T0 - timedelta(hours=2), timedelta(hours=1))
    bad = fake_adapters["bad"] = FakeChannel(3, T0, timedelta(hours=1))
    bad.fail_on_read = 1
    add_channel(collector, "TG-ok", "ok")
    add_channel(collector, "TG-bad", "bad")
    r = run(collector)
    by = {s.source_id: s for s in r.sources}
    assert by["TG-ok"].status == "ok" and by["TG-bad"].status == "error"
    assert r.status == "partial"
    assert collector.store.get_source("default", "TG-bad").next_retry_at is not None
    r2 = run(collector)
    assert {s.source_id: s.status for s in r2.sources}["TG-bad"] == "skipped"
    bad.fail_on_read = None
    r3 = run(collector, force=True)
    assert {s.source_id: s.status for s in r3.sources}["TG-bad"] == "ok"


def test_candidates_are_isolated(collector, fake_adapters):
    fake_adapters["a"] = FakeChannel(3, T0 - timedelta(hours=2), timedelta(hours=1), text=lambda i: f"A вакансия {i} требования")
    fake_adapters["b"] = FakeChannel(4, T0 - timedelta(hours=3), timedelta(hours=1), text=lambda i: f"B вакансия {i} требования")
    add_channel(collector, "TG-1", "a", candidate="alice")
    add_channel(collector, "TG-1", "b", candidate="bob")
    run(collector, candidate="alice")
    run(collector, candidate="bob")
    assert {v.text[0] for v in stored(collector, "alice")} == {"A"} and len(stored(collector, "alice")) == 3
    assert {v.text[0] for v in stored(collector, "bob")} == {"B"} and len(stored(collector, "bob")) == 4
    assert collector.store.get_vacancy("bob", stored(collector, "alice")[0].vacancy_id) is None


def test_lease_blocks_second_run_and_stale_writer(collector, fake_adapters):
    fake_adapters["ch"] = FakeChannel(3, T0, timedelta(hours=1))
    add_channel(collector, "TG-1", "ch")
    assert collector.store.acquire_lease("default", "TG-1", "run_other")
    r = run(collector)
    assert r.sources[0].status == "skipped" and r.sources[0].error.code == "lease_lost"
    with pytest.raises(SourceError) as exc:
        collector.store.commit_page("default", "TG-1", "run_stale", [], collector.store.get_source("default", "TG-1").cursor)
    assert exc.value.code == "lease_lost"


def test_rerun_is_idempotent(collector, fake_adapters):
    fake_adapters["ch"] = FakeChannel(30, T0 - timedelta(hours=29), timedelta(hours=1))
    add_channel(collector, "TG-1", "ch")
    run(collector)
    r = run(collector)
    assert r.sources[0].status == "no_new" and r.sources[0].items_new == 0 and r.sources[0].items_updated == 0
    assert len(stored(collector)) == 30


def test_same_hh_vacancy_from_two_queries_is_one_record_with_two_origins(collector):
    from hrs_collector.adapters.base import make_vacancy

    item = make_vacancy(platform="hh", item_key="hh:555", external_id="555", title="CFO", company="Пример",
                        text="CFO\nПример", published_at=T0, is_vacancy="likely")
    for sid in ("hh-cfo", "hh-finance-director"):
        collector.store.upsert_vacancy("default", sid, item)
    items = stored(collector)
    assert len(items) == 1
    details = collector.store.vacancy_details("default", items[0].vacancy_id)
    assert [o["source_id"] for o in details["origins"]] == ["hh-cfo", "hh-finance-director"]


def test_snippet_after_full_fetch_does_not_create_spurious_versions(collector):
    from hrs_collector.adapters.base import make_vacancy

    snippet = make_vacancy(platform="hh", item_key="hh:9", external_id="9", title="COO", company="X",
                           text="COO\nX", published_at=T0, completeness="snippet")
    full = make_vacancy(platform="hh", item_key="hh:9", external_id="9", title="COO", company="X",
                        text="Полное описание: обязанности, требования", published_at=T0, completeness="full")
    assert collector.store.upsert_vacancy("default", "s", snippet) == "new"
    assert collector.store.upsert_vacancy("default", "s", full) == "updated"
    assert collector.store.upsert_vacancy("default", "s", snippet) == "unchanged"
    vac = stored(collector)[0]
    assert vac.completeness == "full" and vac.version == 2
