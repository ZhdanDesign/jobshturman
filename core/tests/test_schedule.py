"""Regular work without the agent: schedule settings, reminders by rules, the daily digest."""
import unittest
from datetime import UTC, datetime, timedelta

from hrshturman_core import applications as apps
from hrshturman_core import intake, schedule
from test_core import Base

# 14:00 in Moscow: outside the default quiet hours 21:00–09:00.
NOON = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)


def later(days=0, hours=0):
    return datetime.now(UTC) + timedelta(days=days, hours=hours)


class ScheduleSettingsTests(Base):
    def test_defaults_and_changes(self):
        s = schedule.get_settings(self.store, "cand-a")
        self.assertEqual(set(s["tasks"]), set(schedule.TASKS))
        self.assertEqual(s["tasks"]["collect"], {"enabled": True, "times": ["08:00"]})
        s = schedule.set_task(self.store, "cand-a", "collect", times=["9:15", "18:00"])
        self.assertEqual(s["tasks"]["collect"]["times"], ["09:15", "18:00"])
        s = schedule.set_task(self.store, "cand-a", "reminders", enabled=False)
        self.assertFalse(s["tasks"]["reminders"]["enabled"])
        self.assertEqual(s["tasks"]["collect"]["times"], ["09:15", "18:00"])  # other tasks kept
        self.assertCode("invalid_schema", schedule.set_task, self.store, "cand-a", "collect", times=["25:00"])
        self.assertCode("invalid_schema", schedule.set_task, self.store, "cand-a", "apply")

    def test_agent_tasks_are_visible_and_removable(self):
        schedule.register_agent_task(self.store, "cand-a", "claude_code", "Утренний разбор", "ежедневно 09:00",
                                     "оценить новые вакансии")
        schedule.register_agent_task(self.store, "cand-a", "claude_code", "Утренний разбор", "ежедневно 10:00",
                                     "оценить новые вакансии")
        tasks = schedule.get_settings(self.store, "cand-a")["agent_tasks"]
        self.assertEqual([(t["name"], t["cadence"]) for t in tasks], [("Утренний разбор", "ежедневно 10:00")])
        schedule.unregister_agent_task(self.store, "cand-a", "claude_code", "Утренний разбор")
        self.assertEqual(schedule.get_settings(self.store, "cand-a")["agent_tasks"], [])
        self.assertCode("not_found", schedule.unregister_agent_task, self.store, "cand-a", "codex", "x")


class ReminderTests(Base):
    def vacancy(self, n=1):
        apps.import_vacancies(self.store, "cand-a", [{"source": "hh", "external_id": str(i), "title": f"CFO {i}",
                                                      "text": "описание"} for i in range(n)])

    def test_rules_follow_the_state(self):
        self.assertEqual(schedule.reminders(self.store, "cand-a"), [])
        intake.save_draft(self.store, "cand-a", [5])
        self.vacancy(2)
        now_ids = {r["id"] for r in schedule.reminders(self.store, "cand-a")}
        self.assertEqual(now_ids, {"vacancies_unscored"})  # the questionnaire is too fresh yet
        in_three_days = {r["id"] for r in schedule.reminders(self.store, "cand-a", later(days=3))}
        self.assertEqual(in_three_days, {"vacancies_unscored", "intake_unconfirmed"})
        key = {"expires_at": later(hours=20).isoformat()}
        self.assertIn("key_expiring", {r["id"] for r in schedule.reminders(self.store, "cand-a", license_info=key)})

    def test_done_holds_until_the_state_changes_and_snooze_until_time(self):
        self.vacancy(1)
        schedule.reminder_done(self.store, "cand-a", "vacancies_unscored")
        self.assertEqual(schedule.reminders(self.store, "cand-a"), [])
        self.vacancy(2)  # one more vacancy: a new state, the reminder is back
        self.assertEqual([r["id"] for r in schedule.reminders(self.store, "cand-a")], ["vacancies_unscored"])
        schedule.reminder_snooze(self.store, "cand-a", "vacancies_unscored", 3)
        self.assertEqual(schedule.reminders(self.store, "cand-a"), [])
        self.assertEqual(len(schedule.reminders(self.store, "cand-a", later(hours=4))), 1)
        self.assertCode("not_found", schedule.reminder_done, self.store, "cand-a", "no_reply:app_x")

    def test_notification_once_per_state_quiet_hours_and_daily_limit(self):
        self.vacancy(1)
        first = schedule.pick_notification(self.store, "cand-a", NOON)
        self.assertEqual(first["notify"]["rule"], "vacancies_unscored")
        self.assertEqual(schedule.pick_notification(self.store, "cand-a", NOON)["reason"], "nothing_new")
        night = datetime(2026, 10, 20, 20, 0, tzinfo=UTC)  # 23:00 in Moscow
        self.vacancy(2)
        self.assertEqual(schedule.pick_notification(self.store, "cand-a", night)["reason"], "quiet_hours")
        for n in (3, 4):
            self.vacancy(n)
            self.assertIsNotNone(schedule.pick_notification(self.store, "cand-a", NOON)["notify"])
        self.vacancy(5)
        self.assertEqual(schedule.pick_notification(self.store, "cand-a", NOON)["reason"], "daily_limit")
        intake.set_paused(self.store, "cand-a", True)
        self.assertEqual(schedule.pick_notification(self.store, "cand-a", NOON)["reason"], "paused")


class DigestTests(Base):
    def test_digest_counts_ranks_and_text(self):
        apps.import_vacancies(self.store, "cand-a", [{"source": "hh", "external_id": str(i), "title": t,
                                                      "text": "описание"} for i, t in enumerate(["CFO", "CEO"])])
        a = apps.list_applications(self.store, "cand-a")[0]
        apps.score(self.store, "cand-a", a["object_id"], a["version"], 4, "по профилю", [])
        d = schedule.digest(self.store, "cand-a")
        self.assertEqual((d["counts"]["new"], d["counts"]["unscored"], d["counts"]["waiting_decision"]), (2, 1, 1))
        self.assertEqual([(t["title"], t["rank"]) for t in d["top"]], [("CFO", 4)])
        self.assertIn("4/5 — CFO", d["markdown"])
        self.assertIn("JobShturman, продолжи", d["markdown"])
        self.assertEqual(schedule.digest(self.store, "cand-a", since=later(hours=1).isoformat())["counts"]["new"], 0)


if __name__ == "__main__":
    unittest.main()
