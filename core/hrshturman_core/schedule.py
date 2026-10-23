"""Regular work without the agent: schedule settings, reminders by fixed rules and the daily digest.

Everything here is deterministic and cheap: it reads the stored state and counts. The system
scheduler (launchd, Task Scheduler, systemd) runs `hrs schedule run <task>`; the agent reads the
same reminders and digest through MCP tools. Nothing here applies to a vacancy or writes to an
employer: those stay with the person (stages 16 and 18).
"""
import hashlib
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .common import DomainError, now
from .workspace import get_candidate

TASKS = {
    "license": "проверить ключ и скачать новую методику",
    "collect": "собрать новые вакансии по включённым источникам",
    "digest": "сводка: новые вакансии, что ждёт решения, воронка",
    "reminders": "напомнить о том, что ждёт человека",
    "wake": "разбудить агента: утром, днём и вечером он сам продолжает работу, работодателям ничего не отправляет",
    "watch": "сторож: раз в час проверить, что задачи стоят в планировщике, вернуть пропавшие и догнать пропущенные",
}
# Tasks the watch never repeats on its own: reminders fire several times a day anyway, the watch is itself.
NO_CATCH_UP = ("reminders", "watch")
DEFAULTS = {
    "tasks": {"license": {"enabled": True, "times": ["07:30"]},
              "collect": {"enabled": True, "times": ["08:00"]},
              "digest": {"enabled": True, "times": ["08:30"]},
              "reminders": {"enabled": True, "times": ["12:00", "16:00", "19:00"]},
              "wake": {"enabled": True, "times": ["09:30", "14:00", "19:30"]},
              "watch": {"enabled": True, "times": [], "every_minutes": 60}},
    # False after the person asked to take the tasks out of the system scheduler: nothing puts them back
    # until they ask again (schedule_install).
    "system_scheduler": True,
    "quiet_hours": {"from": "21:00", "to": "09:00"},
    "max_per_day": 3,
    "agent_tasks": [],
    # Automatic applications: the person switches them on once; only vacancies ranked min_rank+ go out.
    "auto_apply": {"enabled": False, "min_rank": 4, "daily_limit": 40, "changed_at": None},
    "last_runs": {},
    "shown_log": [],  # when notifications were shown, for the daily limit
}
AGENTS = {"claude_code", "codex", "grok", "hermes", "openclaw", "other"}
SNOOZE_MAX_HOURS = 24 * 14


def _hhmm(value):
    try:
        hours, minutes = (int(x) for x in str(value).split(":"))
    except ValueError:
        raise DomainError("invalid_schema", f"time must be HH:MM, got {value!r}") from None
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        raise DomainError("invalid_schema", f"time must be HH:MM, got {value!r}")
    return f"{hours:02d}:{minutes:02d}"


def get_settings(store, candidate_id):
    with store.read() as tx:
        get_candidate(tx, candidate_id)
        obj = tx.find("schedule", candidate_id, "settings")
    data = {k: obj[k] for k in DEFAULTS if k in obj} if obj else {}
    return {**DEFAULTS, **data, "tasks": {**DEFAULTS["tasks"], **data.get("tasks", {})},
            "auto_apply": {**DEFAULTS["auto_apply"], **data.get("auto_apply", {})}}


def _save(store, candidate_id, change, actor, event):
    with store.transaction(actor) as tx:
        get_candidate(tx, candidate_id)
        obj = tx.find("schedule", candidate_id, "settings")
        current = {**DEFAULTS, **({k: obj[k] for k in DEFAULTS if k in obj} if obj else {})}
        data = change(current)
        if obj is None:
            tx.insert("schedule", candidate_id, data, "sched", natural_key="settings")
        else:
            tx.update("schedule", candidate_id, obj["object_id"], data, obj["version"])
        tx.event(candidate_id, event)
    return get_settings(store, candidate_id)


def set_task(store, candidate_id, task, times=None, enabled=None, actor="candidate_via_agent"):
    """Change when a task of hrs runs or switch it off. Only at the person's word."""
    if task not in TASKS:
        raise DomainError("invalid_schema", f"task must be one of {list(TASKS)}")
    if times is not None and task == "watch":
        raise DomainError("invalid_schema", "the watch runs every hour; it can only be switched on or off")
    if times is not None:
        if not isinstance(times, list) or not 1 <= len(times) <= 6:
            raise DomainError("invalid_schema", "times: 1 to 6 values HH:MM")
        times = sorted({_hhmm(t) for t in times})

    def change(data):
        tasks = {**DEFAULTS["tasks"], **data["tasks"]}
        entry = dict(tasks[task])
        if times is not None:
            entry["times"] = times
        if task == "wake":
            entry["agreed"] = True  # the person chose the hours: the agent stops asking
        if enabled is not None:
            entry["enabled"] = bool(enabled)
        return dict(data, tasks={**tasks, task: entry})
    return _save(store, candidate_id, change, actor, "schedule_changed")


AUTO_APPLY_MAX_PER_DAY = 40


def set_auto_apply(store, candidate_id, enabled, min_rank=None, daily_limit=None, actor="candidate_via_agent"):
    """Switch automatic applications on or off. Only at the person's explicit word: it is their standing
    permission to apply without asking about each vacancy. The person starts each batch in a conversation;
    nothing scheduled sends applications. hrs keeps the rank threshold, the daily limit and the pause."""
    if min_rank is not None and min_rank not in (4, 5):
        raise DomainError("invalid_schema", "min_rank: 4 or 5")
    if daily_limit is not None and (not isinstance(daily_limit, int) or not 1 <= daily_limit <= AUTO_APPLY_MAX_PER_DAY):
        raise DomainError("invalid_schema", f"daily_limit: 1 to {AUTO_APPLY_MAX_PER_DAY}")

    def change(data):
        policy = {**DEFAULTS["auto_apply"], **data.get("auto_apply", {}), "enabled": bool(enabled), "changed_at": now()}
        if min_rank is not None:
            policy["min_rank"] = min_rank
        if daily_limit is not None:
            policy["daily_limit"] = daily_limit
        return dict(data, auto_apply=policy)
    return _save(store, candidate_id, change, actor, "auto_apply_changed")


def set_quiet_hours(store, candidate_id, start, end, actor="candidate_via_agent"):
    start, end = _hhmm(start), _hhmm(end)
    return _save(store, candidate_id, lambda d: dict(d, quiet_hours={"from": start, "to": end}), actor,
                 "schedule_changed")


def register_agent_task(store, candidate_id, agent, name, cadence, purpose, actor="agent"):
    """The agent created a scheduled task with its own means and tells hrs, so the person sees and can stop it."""
    if agent not in AGENTS:
        raise DomainError("invalid_schema", f"agent must be one of {sorted(AGENTS)}")
    for field, value in (("name", name), ("cadence", cadence), ("purpose", purpose)):
        if not isinstance(value, str) or not value.strip() or len(value) > 300:
            raise DomainError("invalid_schema", f"{field}: nonempty text up to 300 characters")

    def change(data):
        others = [t for t in data["agent_tasks"] if not (t["agent"] == agent and t["name"] == name)]
        return dict(data, agent_tasks=others + [{"agent": agent, "name": name.strip(), "cadence": cadence.strip(),
                                                 "purpose": purpose.strip(), "registered_at": now()}])
    return _save(store, candidate_id, change, actor, "agent_task_registered")


def unregister_agent_task(store, candidate_id, agent, name, actor="agent"):
    def change(data):
        left = [t for t in data["agent_tasks"] if not (t["agent"] == agent and t["name"] == name)]
        if len(left) == len(data["agent_tasks"]):
            raise DomainError("not_found", f"agent task {agent}:{name}")
        return dict(data, agent_tasks=left)
    return _save(store, candidate_id, change, actor, "agent_task_removed")


def set_system_scheduler(store, candidate_id, on, actor="candidate_via_agent"):
    return _save(store, candidate_id, lambda d: dict(d, system_scheduler=bool(on)), actor, "schedule_changed")


def record_run(store, candidate_id, task, result, actor="scheduler"):
    if task not in TASKS:
        raise DomainError("invalid_schema", f"task must be one of {list(TASKS)}")
    return _save(store, candidate_id,
                 lambda d: dict(d, last_runs={**d["last_runs"], task: {"at": now(), "result": result}}),
                 actor, "schedule_run")


# --- reminders ---

def _aware(stamp):
    value = datetime.fromisoformat(stamp)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _age(stamp, at):
    return at - _aware(stamp)


def _rules(store, candidate_id, at, license_info=None):
    """Every reminder the stored state calls for now, before done/snooze/quiet hours."""
    with store.read() as tx:
        intake = tx.find("intake", candidate_id, "active")
        resumes = tx.list("resume", candidate_id)
        facts = tx.list("fact", candidate_id)
        apps = tx.list("application", candidate_id)
    out = []

    def add(rule, text, ref=None, fingerprint=""):
        out.append({"id": f"{rule}:{ref}" if ref else rule, "rule": rule, "text": text, "ref": ref,
                    "fingerprint": hashlib.sha256(f"{rule}|{ref}|{fingerprint}".encode()).hexdigest()[:16]})

    if intake and intake["status"] != "confirmed" and _age(intake["updated_at"], at) >= timedelta(days=2):
        add("intake_unconfirmed", "Анкета заполнена, но не подтверждена: проверьте карту запроса.",
            fingerprint=str(intake["version"]))
    for r in resumes:
        if r["status"] != "approved" and _age(r["updated_at"], at) >= timedelta(days=3):
            add("resume_draft", "Резюме ещё в черновике: проверьте и утвердите его.", r["object_id"], str(r["version"]))
    unconfirmed = [f for f in facts if f["status"] in ("reported", "needs_confirmation")]
    if unconfirmed and resumes:
        add("facts_unconfirmed", f"Не подтверждено фактов о вас: {len(unconfirmed)}.",
            fingerprint=str(len(unconfirmed)))
    unscored = [a for a in apps if a["state"] in ("found", "needs_full_text") and not a["score"]]
    if unscored:
        add("vacancies_unscored", f"Новых вакансий без оценки: {len(unscored)}. Попросите агента оценить их.",
            fingerprint=str(len(unscored)))
    waiting = [a for a in apps if a["state"] == "evaluated" and _age(a["updated_at"], at) >= timedelta(days=1)]
    if waiting:
        add("decisions_waiting", f"Вакансий ждут вашего решения «да, позже, нет»: {len(waiting)}.",
            fingerprint=",".join(sorted(a["object_id"] for a in waiting)))
    for a in apps:
        title = a["vacancy"].get("title") or "вакансия"
        if a["state"] == "draft_ready":
            add("authority_missing", f"Отклик на «{title}» готов и ждёт вашего разрешения.", a["object_id"],
                str(a["version"]))
        auth = a.get("authority") or {}
        if a["state"] == "authorized" and auth.get("expires_at") and \
                timedelta(0) <= _aware(auth["expires_at"]) - at <= timedelta(days=1):
            add("authority_expiring", f"Разрешение на отклик «{title}» истекает в течение суток.", a["object_id"],
                auth["expires_at"])
        if a["state"] == "sent_confirmed":
            kinds = {e["kind"] for e in a["funnel"]}
            sent_at = (a.get("evidence") or {}).get("observed_at") or a["updated_at"]
            if kinds <= {"awaiting_reply"} and _age(sent_at, at) >= timedelta(days=7):
                add("no_reply", f"По отклику «{title}» нет ответа 7 дней: можно написать повторно.", a["object_id"],
                    sent_at)
            if "invited" in kinds and "interview_done" not in kinds:
                add("interview_prep", f"Приглашение на встречу по «{title}»: подготовьтесь вместе с агентом.",
                    a["object_id"])
    expires = (license_info or {}).get("expires_at")
    if expires and timedelta(0) <= _aware(expires) - at <= timedelta(days=1):
        add("key_expiring", "Ключ доступа истекает завтра: продлите его, чтобы методика оставалась доступной.",
            fingerprint=expires)
    return out


def _states(store, candidate_id):
    with store.read() as tx:
        return {o["reminder_id"]: o for o in tx.list("reminder", candidate_id)}


def reminders(store, candidate_id, at=None, license_info=None):
    """Active reminders: the rules that hold now, minus those closed for this same state or snoozed."""
    at = at or datetime.now(UTC)
    states = _states(store, candidate_id)
    active = []
    for item in _rules(store, candidate_id, at, license_info):
        st = states.get(item["id"])
        if st and st.get("done_fingerprint") == item["fingerprint"]:
            continue
        if st and st.get("snoozed_until") and _aware(st["snoozed_until"]) > at:
            continue
        active.append(item | {"shown_fingerprint": (st or {}).get("shown_fingerprint")})
    return active


def _update_reminder(store, candidate_id, reminder_id, fields, actor, event):
    with store.transaction(actor) as tx:
        get_candidate(tx, candidate_id)
        obj = tx.find("reminder", candidate_id, reminder_id)
        if obj is None:
            obj = tx.insert("reminder", candidate_id, {"reminder_id": reminder_id, **fields}, "rem",
                            natural_key=reminder_id)
        else:
            obj = tx.update("reminder", candidate_id, obj["object_id"], dict(obj, **fields), obj["version"])
        tx.event(candidate_id, event, ("reminder", obj["object_id"]))
        return obj


def _find_active(store, candidate_id, reminder_id, at, license_info):
    item = next((r for r in _rules(store, candidate_id, at, license_info) if r["id"] == reminder_id), None)
    if item is None:
        raise DomainError("not_found", f"reminder {reminder_id} is not active")
    return item


def reminder_done(store, candidate_id, reminder_id, actor="candidate_via_agent", at=None, license_info=None):
    """Close a reminder for the current state; it comes back only if the state changes."""
    item = _find_active(store, candidate_id, reminder_id, at or datetime.now(UTC), license_info)
    return _update_reminder(store, candidate_id, reminder_id, {"done_fingerprint": item["fingerprint"],
                                                                "snoozed_until": None}, actor, "reminder_done")


def reminder_snooze(store, candidate_id, reminder_id, hours, actor="candidate_via_agent", at=None, license_info=None):
    if not isinstance(hours, (int, float)) or not 0 < hours <= SNOOZE_MAX_HOURS:
        raise DomainError("invalid_schema", f"hours: from 1 to {SNOOZE_MAX_HOURS}")
    at = at or datetime.now(UTC)
    _find_active(store, candidate_id, reminder_id, at, license_info)
    until = (at + timedelta(hours=hours)).isoformat()
    return _update_reminder(store, candidate_id, reminder_id, {"snoozed_until": until}, actor, "reminder_snoozed")


def _quiet(settings, local):
    start, end = settings["quiet_hours"]["from"], settings["quiet_hours"]["to"]
    t = local.strftime("%H:%M")
    return start <= t or t < end if start > end else start <= t < end


quiet = _quiet


def pick_notification(store, candidate_id, at=None, license_info=None, actor="scheduler"):
    """One reminder for a system notification: not in quiet hours, not more than max_per_day a day,
    never the same reminder twice for the same state. Marks it shown."""
    at = at or datetime.now(UTC)
    with store.read() as tx:
        cand = get_candidate(tx, candidate_id)
    if cand["paused"]:
        return {"notify": None, "reason": "paused"}
    settings = get_settings(store, candidate_id)
    local = at.astimezone(ZoneInfo(cand["timezone"]))
    if _quiet(settings, local):
        return {"notify": None, "reason": "quiet_hours"}
    today = [t for t in settings["shown_log"] if _aware(t).astimezone(local.tzinfo).date() == local.date()]
    if len(today) >= settings["max_per_day"]:
        return {"notify": None, "reason": "daily_limit"}
    fresh = [r for r in reminders(store, candidate_id, at, license_info)
             if r["shown_fingerprint"] != r["fingerprint"]]
    if not fresh:
        return {"notify": None, "reason": "nothing_new"}
    item = fresh[0]
    _update_reminder(store, candidate_id, item["id"], {"shown_fingerprint": item["fingerprint"]}, actor,
                     "reminder_shown")
    _save(store, candidate_id, lambda d: dict(d, shown_log=(d["shown_log"] + [at.isoformat()])[-20:]), actor,
          "notification_shown")
    return {"notify": {k: item[k] for k in ("id", "rule", "text", "ref")}, "reason": None}


# --- digest ---

def digest(store, candidate_id, since=None, at=None, license_info=None):
    """What changed since the last digest and what waits for the person. Same data for the file and the agent."""
    at = at or datetime.now(UTC)
    with store.read() as tx:
        get_candidate(tx, candidate_id)
        apps = tx.list("application", candidate_id)
    since_dt = _aware(since) if since else at - timedelta(days=1)
    new = [a for a in apps if _aware(a["created_at"]) >= since_dt]
    scored = sorted((a for a in apps if a["score"] and a["state"] != "rejected_policy"),
                    key=lambda a: -(a["score"]["final"] or a["score"]["preliminary"]))
    top = [{"application_id": a["object_id"], "title": a["vacancy"].get("title"),
            "company": a["vacancy"].get("company"), "url": a["vacancy"].get("url"),
            "rank": a["score"]["final"] or a["score"]["preliminary"]} for a in scored[:5]]
    counts = {
        "new": len(new),
        "unscored": sum(1 for a in apps if a["state"] in ("found", "needs_full_text") and not a["score"]),
        "waiting_decision": sum(1 for a in apps if a["state"] == "evaluated"),
        "waiting_permission": sum(1 for a in apps if a["state"] == "draft_ready"),
        "sent": sum(1 for a in apps if a["state"] == "sent_confirmed"),
        "invited": sum(1 for a in apps if any(e["kind"] == "invited" for e in a["funnel"])),
    }
    waiting = [r["text"] for r in reminders(store, candidate_id, at, license_info)]
    return {"since": since_dt.isoformat(), "at": at.isoformat(), "counts": counts, "top": top, "waiting": waiting,
            "markdown": _digest_markdown(counts, top, waiting, at)}


def _digest_markdown(counts, top, waiting, at):
    lines = [f"# Сводка JobShturman на {at.date().isoformat()}", "",
             f"- Новых вакансий: {counts['new']}, без оценки: {counts['unscored']}.",
             (f"- Ждут вашего решения: {counts['waiting_decision']}, ждут разрешения на отклик: "
             f"{counts['waiting_permission']}."),
             f"- Отправлено откликов: {counts['sent']}, приглашений: {counts['invited']}.", ""]
    if top:
        lines += ["## Лучшие вакансии", ""]
        lines += [f"{i}. {t['rank']}/5 — {t['title'] or 'без названия'}"
                  f"{', ' + t['company'] if t['company'] else ''}{' — ' + t['url'] if t['url'] else ''}"
                  for i, t in enumerate(top, 1)]
        lines.append("")
    if waiting:
        lines += ["## Что ждёт вас", ""] + [f"- {w}" for w in waiting] + [""]
    lines.append("Чтобы продолжить, откройте агента и напишите: «JobShturman, продолжи».")
    return "\n".join(lines) + "\n"

