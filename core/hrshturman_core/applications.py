"""Vacancy import and application cards (stages 14-18, 22).

Delivery state and employer funnel are separate models (spec section 12).
Vacancies arrive through import_vacancies in a neutral format; the adapter for
collector/CONTRACT.md converts into it. This module never parses sources.
"""
from datetime import datetime, timedelta, timezone

from .common import DomainError, check_id, digest, parse_time, reject_secrets
from .workspace import get_candidate

VACANCY_FIELDS = {"source", "external_id", "url", "title", "company", "text", "published_at", "collected_at",
                  "location", "salary", "work_format", "collector_vacancy_id"}
DELIVERY_STATES = {"found", "needs_full_text", "evaluated", "deferred", "rejected_policy", "approved",
                   "draft_ready", "authorized", "sending", "sent_confirmed", "send_unknown",
                   "not_sent_confirmed", "blocked"}
FUNNEL_EVENTS = {"awaiting_reply", "invited", "employer_rejected", "employer_frozen",
                 "interview_done", "offer_discussion"}
# States in which a repeated attempt to deliver the same application is forbidden.
DELIVERY_LOCKED = {"sending", "sent_confirmed", "send_unknown"}


def _require(app, allowed, action):
    if app["state"] not in allowed:
        raise DomainError("state_version_conflict", f"Cannot {action} from state {app['state']}")


def import_vacancies(store, candidate_id, items, track_id="main", actor="collector"):
    """Idempotent by source+external_id. Re-import refreshes the snapshot, never the card's fate."""
    if not isinstance(items, list):
        raise DomainError("invalid_schema", "items must be a list")
    check_id(track_id, "track_id")
    clean = []
    for item in items:
        if not isinstance(item, dict) or not {"source", "external_id", "title"} <= set(item) or set(item) - VACANCY_FIELDS:
            raise DomainError("invalid_schema", f"vacancy fields: required source, external_id, title; allowed {sorted(VACANCY_FIELDS)}")
        reject_secrets(item)
        source = check_id(str(item["source"]).strip().casefold(), "source")
        external_id = str(item["external_id"]).strip()
        if not external_id or len(external_id) > 256:
            raise DomainError("invalid_schema", "Invalid external_id")
        clean.append(dict(item, source=source, external_id=external_id))
    counts = {"added": 0, "updated": 0, "unchanged": 0}
    with store.transaction(actor) as tx:
        get_candidate(tx, candidate_id)
        for item in clean:
            key = f"{item['source']}:{item['external_id']}"
            snapshot_hash = digest(item)
            app = tx.find("application", candidate_id, key)
            has_text = bool((item.get("text") or "").strip())
            if app is None:
                app = tx.insert("application", candidate_id, {
                    "vacancy": item, "snapshot_hash": snapshot_hash, "track_id": track_id,
                    "state": "found" if has_text else "needs_full_text",
                    "score": None, "decision": None, "material": None, "authority": None,
                    "operation_id": None, "evidence": None, "funnel": [], "blocker": None,
                }, "app", natural_key=key)
                tx.event(candidate_id, "vacancy_found", ("application", app["object_id"]), key=key)
                counts["added"] += 1
            elif app["snapshot_hash"] == snapshot_hash:
                counts["unchanged"] += 1
            else:
                state = app["state"]
                if state == "needs_full_text" and has_text:
                    state = "found"
                tx.update("application", candidate_id, app["object_id"],
                          dict(app, vacancy=item, snapshot_hash=snapshot_hash, state=state), app["version"])
                tx.event(candidate_id, "vacancy_refreshed", ("application", app["object_id"]), key=key)
                counts["updated"] += 1
    return counts


def _mutate(store, candidate_id, application_id, expected_version, actor, fn, event, **details):
    with store.transaction(actor) as tx:
        cand = get_candidate(tx, candidate_id)
        app = tx.get("application", candidate_id, application_id)
        changes = fn(app, cand)
        obj = tx.update("application", candidate_id, application_id, dict(app, **changes), expected_version)
        tx.event(candidate_id, event, ("application", application_id),
                 previous_state=app["state"], new_state=obj["state"], **details)
        return obj


def score(store, candidate_id, application_id, expected_version, preliminary, why, fact_ids,
          final=None, risks=None, actor="ai"):
    """Stage 15: explained 1-5 score. Without full text only a preliminary score is allowed."""
    for value in (preliminary, final):
        if value is not None and value not in (1, 2, 3, 4, 5):
            raise DomainError("invalid_schema", "score must be 1..5")
    if not isinstance(why, str) or not why.strip() or not isinstance(fact_ids, list):
        raise DomainError("invalid_schema", "why and fact_ids are required")

    def change(app, _):
        _require(app, {"found", "needs_full_text", "evaluated", "deferred"}, "score")
        if app["state"] == "needs_full_text" and final is not None:
            raise DomainError("incomplete_source", "Final score needs the full vacancy text")
        state = app["state"] if app["state"] == "needs_full_text" else "evaluated"
        return {"state": state, "score": {"preliminary": preliminary, "final": final, "why": why,
                                          "fact_ids": fact_ids, "risks": risks or []}}
    return _mutate(store, candidate_id, application_id, expected_version, actor, change, "scored")


def decide(store, candidate_id, application_id, expected_version, decision, reason, actor="candidate"):
    """Stage 16: yes / later / no. Silence is never consent."""
    targets = {"approve": "approved", "defer": "deferred", "reject": "rejected_policy"}
    if decision not in targets:
        raise DomainError("invalid_schema", f"decision must be one of {sorted(targets)}")

    def change(app, _):
        _require(app, {"evaluated", "deferred", "approved", "rejected_policy"}, "decide")
        if decision == "approve" and not (app["score"] and app["score"]["final"]):
            raise DomainError("incomplete_source", "Approve needs a final score")
        return {"state": targets[decision], "decision": {"decision": decision, "reason": reason}}
    return _mutate(store, candidate_id, application_id, expected_version, actor, change, "decided",
                   decision=decision)


def _policy_rank(app, policy):
    if not policy.get("enabled"):
        raise DomainError("permission_required", "Автоотклики выключены: решение и разрешение даёт человек")
    final = (app["score"] or {}).get("final")
    if not final or final < policy["min_rank"]:
        raise DomainError("permission_required", f"Автоотклик только на вакансии с итоговой оценкой {policy['min_rank']}+")
    return final


def auto_approve(store, candidate_id, application_id, expected_version, policy, actor="auto_policy"):
    """Automatic mode: the person's standing permission stands for «approve» on a high-ranked vacancy."""
    def change(app, _):
        _require(app, {"evaluated"}, "approve automatically")
        rank = _policy_rank(app, policy)
        return {"state": "approved", "decision": {"decision": "approve", "reason": f"автоотклик: оценка {rank}",
                                                  "by": "auto_policy"}}
    return _mutate(store, candidate_id, application_id, expected_version, actor, change, "decided", decision="approve")


def auto_authorize(store, candidate_id, application_id, expected_version, policy, actor="auto_policy"):
    """Automatic mode: permission for the current package, valid one day, while the mode stays on."""
    expiry = datetime.now(timezone.utc) + timedelta(days=1)

    def change(app, _):
        _require(app, {"draft_ready"}, "authorize automatically")
        _policy_rank(app, policy)
        return {"state": "authorized", "authority": {"material_hash": app["material"]["hash"],
                                                     "channel": app["material"]["channel"],
                                                     "expires_at": expiry.isoformat(), "revoked": False,
                                                     "by": "auto_policy"}}
    return _mutate(store, candidate_id, application_id, expected_version, actor, change, "authorized")


def prepare(store, candidate_id, application_id, expected_version, material, channel, actor="ai"):
    """Stage 17: exact package for this vacancy. Hash binds later authority to these bytes."""
    if not isinstance(material, dict) or not material:
        raise DomainError("invalid_material", "material must be a nonempty object")
    check_id(channel, "channel")
    reject_secrets(material)

    def change(app, _):
        _require(app, {"approved", "draft_ready", "authorized", "not_sent_confirmed"}, "prepare")
        return {"state": "draft_ready", "authority": None,
                "material": {"content": material, "hash": digest(material), "channel": channel}}
    return _mutate(store, candidate_id, application_id, expected_version, actor, change, "material_prepared")


def authorize(store, candidate_id, application_id, expected_version, material_hash, channel, expires_at,
              actor="candidate"):
    """The candidate approves this exact package for this channel until expires_at."""
    expiry = parse_time(expires_at, "expires_at")
    if expiry <= datetime.now(timezone.utc):
        raise DomainError("expired_authority", "expires_at is in the past")

    def change(app, _):
        _require(app, {"draft_ready"}, "authorize")
        if (app["material"]["hash"], app["material"]["channel"]) != (material_hash, channel):
            raise DomainError("invalid_material", "Authority must match the current package hash and channel")
        return {"state": "authorized", "authority": {"material_hash": material_hash, "channel": channel,
                                                     "expires_at": expiry.isoformat(), "revoked": False}}
    return _mutate(store, candidate_id, application_id, expected_version, actor, change, "authorized")


def revoke(store, candidate_id, application_id, expected_version, actor="candidate"):
    """Revocation stops the next action; the prepared package stays."""
    def change(app, _):
        _require(app, {"authorized"}, "revoke")
        return {"state": "draft_ready", "authority": dict(app["authority"], revoked=True)}
    return _mutate(store, candidate_id, application_id, expected_version, actor, change, "authority_revoked")


def _check_authority(app):
    auth = app["authority"]
    if not auth or auth["revoked"]:
        raise DomainError("revoked_authority", "No active authority")
    if datetime.fromisoformat(auth["expires_at"]) <= datetime.now(timezone.utc):
        raise DomainError("expired_authority", "Authority expired")
    if auth["material_hash"] != app["material"]["hash"]:
        raise DomainError("invalid_material", "Package changed after authorization")


def _evidence(evidence, need):
    if not isinstance(evidence, dict) or not {"observed_at", "proof_ref"} <= set(evidence):
        raise DomainError("invalid_schema", "evidence needs observed_at and proof_ref")
    reject_secrets(evidence)
    stamp = parse_time(evidence["observed_at"], "observed_at")
    if stamp > datetime.now(timezone.utc):
        raise DomainError("invalid_schema", "observed_at is in the future")
    for field in need:
        if not str(evidence.get(field) or "").strip():
            raise DomainError("invalid_schema", f"evidence needs {field}")
    return evidence


def record_sent_by_candidate(store, candidate_id, application_id, expected_version, evidence, actor="candidate"):
    """prepare_only mode: the candidate applied by hand. Evidence is self-reported, and marked so."""
    evidence = _evidence(evidence, ())

    def change(app, _):
        _require(app, {"authorized"}, "record a sent application")
        _check_authority(app)
        return {"state": "sent_confirmed",
                "evidence": dict(evidence, level="self_reported", method="candidate_manual")}
    return _mutate(store, candidate_id, application_id, expected_version, actor, change, "sent_by_candidate")


def reserve(store, candidate_id, application_id, expected_version, worker, actor="runner"):
    """Durable attempt before an external call (the agent's browser). One operation per vacancy."""
    check_id(worker, "worker")

    def change(app, cand):
        if cand["paused"]:
            raise DomainError("permission_required", "Candidate paused new external actions")
        _require(app, {"authorized"}, "reserve")
        _check_authority(app)
        vac, channel = app["vacancy"], app["material"]["channel"]
        return {"state": "sending", "operation_id": app["operation_id"] or
                f"{candidate_id}:{channel}:apply:{vac['source']}:{vac['external_id']}",
                "attempt": {"worker": worker, "started_at": datetime.now(timezone.utc).isoformat()}}
    return _mutate(store, candidate_id, application_id, expected_version, actor, change, "reserved")


def mark_unknown(store, candidate_id, application_id, expected_version, reason, actor="runner"):
    def change(app, _):
        _require(app, {"sending"}, "mark unknown")
        return {"state": "send_unknown", "blocker": {"code": "outcome_unknown", "reason": reason}}
    return _mutate(store, candidate_id, application_id, expected_version, actor, change, "send_unknown")


def reconcile(store, candidate_id, application_id, expected_version, outcome, evidence, actor="runner"):
    """Only path out of sending/send_unknown. No blind retry."""
    if outcome not in ("sent", "not_sent"):
        raise DomainError("invalid_schema", "outcome must be sent or not_sent")
    evidence = _evidence(evidence, ("platform_id",) if outcome == "sent" else ("check_reference",))

    def change(app, _):
        _require(app, {"sending", "send_unknown"}, "reconcile")
        state = "sent_confirmed" if outcome == "sent" else "not_sent_confirmed"
        return {"state": state, "blocker": None, "evidence": dict(evidence, level="caller_supplied")}
    return _mutate(store, candidate_id, application_id, expected_version, actor, change, "reconciled",
                   outcome=outcome)


def record_employer_event(store, candidate_id, application_id, expected_version, kind, occurred_at, source,
                          note=None, actor="candidate"):
    """Employer funnel after delivery. A frozen vacancy is not a rejection; every change has source and date."""
    if kind not in FUNNEL_EVENTS:
        raise DomainError("invalid_schema", f"kind must be one of {sorted(FUNNEL_EVENTS)}")
    parse_time(occurred_at, "occurred_at")
    if not isinstance(source, str) or not source.strip():
        raise DomainError("invalid_schema", "source is required")
    reject_secrets([note or "", source])

    def change(app, _):
        _require(app, {"sent_confirmed"}, "record an employer event")
        return {"funnel": app["funnel"] + [{"kind": kind, "occurred_at": occurred_at, "source": source, "note": note}]}
    return _mutate(store, candidate_id, application_id, expected_version, actor, change, "employer_event", kind=kind)


def list_applications(store, candidate_id):
    with store.read() as tx:
        get_candidate(tx, candidate_id)
        return tx.list("application", candidate_id)
