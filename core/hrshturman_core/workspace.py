"""Candidate workspace: one record per candidate_id (spec section 8)."""
from .common import DomainError, check_id

PROCESS_VERSION = "0.4.0-draft"


BRANCHES = {"local", "service"}


def create_candidate(store, candidate_id, timezone_name, actor="candidate", branch="local"):
    check_id(candidate_id, "candidate_id")
    if branch not in BRANCHES:
        raise DomainError("invalid_schema", f"branch must be one of {sorted(BRANCHES)}")
    if not isinstance(timezone_name, str) or not timezone_name:
        raise DomainError("invalid_schema", "timezone is required")
    with store.transaction(actor) as tx:
        existing = tx.find("candidate", candidate_id, "workspace")
        if existing:
            return existing
        obj = tx.insert("candidate", candidate_id, {
            "branch": branch,
            "timezone": timezone_name,
            "process_version": PROCESS_VERSION,
            "paused": False,
        }, "cand", natural_key="workspace")
        tx.event(candidate_id, "candidate_created", ("candidate", obj["object_id"]))
        return obj


def get_candidate(tx, candidate_id):
    obj = tx.find("candidate", candidate_id, "workspace")
    if obj is None:
        raise DomainError("not_found", "Unknown candidate")
    return obj
