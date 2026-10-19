"""Search status: next step, blockers and funnel (stages 22, 23; spec section 17).

Counts are by unique application. Every metric lists its IDs so a number can
always be opened. draft and send_unknown are never counted as sent.
"""
from .common import DomainError
from .workspace import get_candidate

AFTER_AUTHORITY = {"sending", "sent_confirmed", "send_unknown", "not_sent_confirmed"}
MANUAL_SEND_NOTE = "Часть откликов отмечена со слов кандидата: подтверждения с площадки по ним нет"

NEXT_STEP = {
    "needs_full_text": ("collector", "Получить полный текст вакансии"),
    "found": ("ai", "Оценить вакансию по фактам кандидата"),
    "evaluated": ("candidate", "Решить: откликаться, отложить или отказаться"),
    "approved": ("ai", "Подготовить комплект под вакансию"),
    "draft_ready": ("candidate", "Проверить комплект и разрешить отклик"),
    "authorized": ("ai", "Откликнуться через браузер и записать результат с доказательством"),
    "sending": ("runner", "Дождаться подтверждения или сверить результат"),
    "send_unknown": ("runner", "Сверить историю канала до любого повтора"),
    "not_sent_confirmed": ("ai", "Проверить комплект и повторить в пределах разрешения"),
}


def status(store, candidate_id):
    with store.read() as tx:
        cand = get_candidate(tx, candidate_id)
        intake = tx.find("intake", candidate_id, "active")
        resumes = tx.list("resume", candidate_id)
        facts = tx.list("fact", candidate_id)
        apps = tx.list("application", candidate_id)
        coverage = tx.find("coverage", candidate_id, "latest")

    by_state = {}
    for app in apps:
        by_state.setdefault(app["state"], []).append(app["object_id"])
    sent = [a for a in apps if a["state"] == "sent_confirmed"]

    def with_funnel(kind):
        return [a["object_id"] for a in sent if any(e["kind"] == kind for e in a["funnel"])]

    funnel = {
        "found": [a["object_id"] for a in apps],
        "needs_full_text": by_state.get("needs_full_text", []),
        "evaluated": [a["object_id"] for a in apps if a["score"] and a["score"]["final"]],
        "rejected_or_deferred": by_state.get("rejected_policy", []) + by_state.get("deferred", []),
        # Cumulative stages: reached this stage or went further.
        "prepared": [a["object_id"] for a in apps if a["material"]],
        "authorized": [a["object_id"] for a in apps if a["authority"] or a["state"] in AFTER_AUTHORITY],
        "sent_confirmed": [a["object_id"] for a in sent],
        "sent_self_reported": [a["object_id"] for a in sent if a["evidence"]["level"] == "self_reported"],
        "send_unknown": by_state.get("send_unknown", []),
        "invited": with_funnel("invited"),
        "interviews": with_funnel("interview_done"),
        "employer_rejected": with_funnel("employer_rejected"),
    }

    next_steps = []
    if intake is None or intake["status"] != "confirmed":
        next_steps.append({"owner": "candidate", "action": "Заполнить и подтвердить анкету-диагностику", "ref": None})
    if not facts:
        next_steps.append({"owner": "ai", "action": "Собрать факты о кандидате из материалов и интервью", "ref": None})
    if not any(r["status"] == "approved" for r in resumes):
        next_steps.append({"owner": "candidate", "action": "Заполнить и утвердить мастер-резюме", "ref": None})
    for app in apps:
        step = NEXT_STEP.get(app["state"])
        if step:
            next_steps.append({"owner": step[0], "action": step[1], "ref": app["object_id"],
                               "title": app["vacancy"]["title"]})

    return {
        "candidate_id": candidate_id,
        "paused": cand["paused"],
        "intake": intake and intake["status"],
        "resume": [{"track_id": r["content"]["track_id"], "status": r["status"]} for r in resumes],
        "facts": {"total": len(facts), "confirmed": sum(f["status"] == "confirmed" for f in facts)},
        "funnel": {"counts": {k: len(v) for k, v in funnel.items()}, "ids": funnel},
        "next_steps": next_steps,
        "coverage": coverage and coverage["report"],
        "limitations": ([MANUAL_SEND_NOTE] if any(a["evidence"]["level"] == "self_reported" for a in sent) else [])
        + ([] if coverage and coverage["report"]["complete"] else
           ["Охват источников неполный или не проверен: «нет вакансий» не означает отсутствие рынка"]),
    }


def save_coverage(store, candidate_id, coverage_report, actor="collector"):
    """Latest source coverage from the collector. `complete` is true only if every source read fully."""
    if not isinstance(coverage_report, dict) or not isinstance(coverage_report.get("complete"), bool):
        raise DomainError("invalid_schema", "coverage needs a boolean `complete`")
    with store.transaction(actor) as tx:
        get_candidate(tx, candidate_id)
        current = tx.find("coverage", candidate_id, "latest")
        if current is None:
            obj = tx.insert("coverage", candidate_id, {"report": coverage_report}, "cov", natural_key="latest")
        else:
            obj = tx.update("coverage", candidate_id, current["object_id"], {"report": coverage_report}, current["version"])
        tx.event(candidate_id, "coverage_saved", ("coverage", obj["object_id"]), complete=coverage_report["complete"])
        return obj


def events(store, candidate_id):
    with store.read() as tx:
        if tx.find("candidate", candidate_id, "workspace") is None:
            raise DomainError("not_found", "Unknown candidate")
        return tx.events(candidate_id)
