"""Fact ledger (stages 03, 11) and master CV with claim map (stage 07).

Every substantive CV claim links to fact_ids. Validation is deterministic: it
checks references, employer attribution, numbers and disclosure. It does not
replace professional review of meaning (spec section 10).
"""
import re

from .common import DomainError, check_id, digest, reject_secrets
from .workspace import get_candidate

FACT_STATUSES = {"reported", "needs_confirmation", "confirmed", "rejected"}
DISCLOSURE = {"public", "generalize_only", "nda"}
NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
PLACEHOLDER = re.compile(r"(\[[^\]]*\]|\{[^}]*\}|\bTODO\b|\bXX+\b|\?\?\?|<[^>]*>)")
RESUME_FIELDS = {"track_id", "language", "full_name", "contacts", "headline", "summary", "experience",
                 "competencies", "education", "languages"}


def add_fact(store, candidate_id, assertion, employer, period, source_refs, metric=None,
             disclosure="public", actor="ai"):
    if not isinstance(assertion, str) or not assertion.strip():
        raise DomainError("invalid_schema", "assertion is required")
    if disclosure not in DISCLOSURE:
        raise DomainError("invalid_schema", f"disclosure must be one of {sorted(DISCLOSURE)}")
    if not isinstance(source_refs, list) or any(not isinstance(r, dict) or "source_id" not in r for r in source_refs):
        raise DomainError("invalid_schema", "source_refs must be a list of {source_id, locator}")
    data = {"assertion": assertion, "employer": employer, "period": period or {}, "metric": metric,
            "source_refs": source_refs, "disclosure": disclosure,
            # A fact from a document or the AI is not confirmed until the candidate says so.
            "status": "reported"}
    reject_secrets(data)
    with store.transaction(actor) as tx:
        get_candidate(tx, candidate_id)
        obj = tx.insert("fact", candidate_id, data, "fact")
        tx.event(candidate_id, "fact_added", ("fact", obj["object_id"]))
        return obj


def set_fact_status(store, candidate_id, fact_id, status, expected_version, actor="candidate"):
    if status not in FACT_STATUSES:
        raise DomainError("invalid_schema", f"status must be one of {sorted(FACT_STATUSES)}")
    with store.transaction(actor) as tx:
        fact = tx.get("fact", candidate_id, fact_id)
        obj = tx.update("fact", candidate_id, fact_id, dict(fact, status=status), expected_version)
        tx.event(candidate_id, "fact_status", ("fact", fact_id), status=status)
        return obj


def _claims(resume):
    """Yield (path, claim, role) for every claim-bearing item."""
    for i, claim in enumerate(resume.get("summary", [])):
        yield f"summary[{i}]", claim, None
    for r, role in enumerate(resume.get("experience", [])):
        for b, claim in enumerate(role.get("bullets", [])):
            yield f"experience[{r}].bullets[{b}]", claim, role
    for i, claim in enumerate(resume.get("competencies", [])):
        yield f"competencies[{i}]", claim, None


def _require_claims(content):
    """A CV with only a headline is not a CV: approve and render need at least one stated claim."""
    if not any((claim.get("text") or "").strip() for _, claim, _ in _claims(content)):
        raise DomainError("empty_resume", "Resume has no claims in summary, experience bullets or competencies")


def _numbers(text):
    return {n.replace(",", ".") for n in NUMBER.findall(text or "")}


def validate_resume(resume, facts_by_id):
    errors, warnings, claim_map = [], [], []

    def err(code, path, message, fact_id=None):
        errors.append({"code": code, "path": path, "message": message, "fact_id": fact_id})

    for path, claim, role in _claims(resume):
        text, fact_ids = claim.get("text", ""), claim.get("fact_ids", [])
        if PLACEHOLDER.search(text):
            err("invalid_material", path, "placeholder left in text")
        if not fact_ids:
            err("missing_fact", path, "claim has no fact_ids")
            continue
        facts = []
        for fid in fact_ids:
            fact = facts_by_id.get(fid)
            if fact is None:
                err("missing_fact", path, f"unknown fact {fid}", fid)
            elif fact["status"] == "rejected":
                err("fact_conflict", path, f"fact {fid} is rejected", fid)
            else:
                facts.append(fact)
        for fact in facts:
            if role is not None and fact.get("employer") and fact["employer"] != role.get("employer"):
                err("fact_conflict", path, f"fact {fact['object_id']} belongs to {fact['employer']}, not {role.get('employer')}",
                    fact["object_id"])
            if fact["disclosure"] == "nda":
                err("restricted_disclosure", path, f"fact {fact['object_id']} is under NDA", fact["object_id"])
            if fact["status"] != "confirmed":
                warnings.append({"code": "needs_confirmation", "path": path, "fact_id": fact["object_id"]})
        known = set()
        for fact in facts:
            known |= _numbers(fact["assertion"])
            if fact.get("metric"):
                known |= _numbers(str(fact["metric"].get("value", "")))
            known |= _numbers(" ".join(str(v) for v in (fact.get("period") or {}).values()))
        if role is not None:
            known |= _numbers(f"{role.get('start', '')} {role.get('end', '')}")
        for number in sorted(_numbers(text) - known):
            err("missing_fact", path, f"number {number} is not in linked facts")
        claim_map.append({"path": path, "fact_ids": fact_ids,
                          "employer": role.get("employer") if role else None})
    return {"ok": not errors, "errors": errors, "warnings": warnings, "claim_map": claim_map}


def _facts(tx, candidate_id):
    return {f["object_id"]: f for f in tx.list("fact", candidate_id)}


def save_resume(store, candidate_id, content, expected_version=None, actor="candidate"):
    """Create or update the master CV of one track. Any edit returns it to draft."""
    if not isinstance(content, dict) or set(content) - RESUME_FIELDS:
        raise DomainError("invalid_schema", f"resume fields: {sorted(RESUME_FIELDS)}")
    track_id = check_id(content.get("track_id", "main"), "track_id")
    content = dict(content, track_id=track_id)
    reject_secrets(content)
    data = {"content": content, "status": "draft", "approved_hash": None}
    with store.transaction(actor) as tx:
        get_candidate(tx, candidate_id)
        key = f"master:{track_id}"
        current = tx.find("resume", candidate_id, key)
        if current is None:
            obj = tx.insert("resume", candidate_id, data, "cv", natural_key=key)
        else:
            if expected_version is None:
                raise DomainError("state_version_conflict", "Resume exists; pass expected_version")
            obj = tx.update("resume", candidate_id, current["object_id"], data, expected_version)
        tx.event(candidate_id, "resume_saved", ("resume", obj["object_id"]), track_id=track_id)
        return dict(obj, validation=validate_resume(content, _facts(tx, candidate_id)))


def get_resume(store, candidate_id, track_id="main"):
    with store.read() as tx:
        obj = tx.find("resume", candidate_id, f"master:{track_id}")
        if obj is None:
            raise DomainError("not_found", "No resume for this track")
        return dict(obj, validation=validate_resume(obj["content"], _facts(tx, candidate_id)))


def approve_resume(store, candidate_id, track_id, expected_version, actor="candidate"):
    """Stage 07 step 8: the candidate approves a version whose claims all hold."""
    with store.transaction(actor) as tx:
        obj = tx.find("resume", candidate_id, f"master:{track_id}")
        if obj is None:
            raise DomainError("not_found", "No resume for this track")
        _require_claims(obj["content"])
        result = validate_resume(obj["content"], _facts(tx, candidate_id))
        if not result["ok"]:
            raise DomainError(result["errors"][0]["code"], "Resume has validation errors")
        if result["warnings"]:
            raise DomainError("missing_fact", "Linked facts are not confirmed by the candidate")
        content_hash = digest(obj["content"])
        obj = tx.update("resume", candidate_id, obj["object_id"],
                        dict(obj, status="approved", approved_hash=content_hash), expected_version)
        tx.event(candidate_id, "resume_approved", ("resume", obj["object_id"]), hash=content_hash)
        return obj


def record_render(store, candidate_id, track_id, manifest, actor="renderer"):
    """Stage 07 step 6: rendered files belong to the exact approved version they were made from."""
    if not isinstance(manifest, dict) or not {"content_hash", "files", "text_extraction"} <= set(manifest):
        raise DomainError("invalid_schema", "manifest needs content_hash, files, text_extraction")
    with store.transaction(actor) as tx:
        obj = tx.find("resume", candidate_id, f"master:{track_id}")
        if obj is None:
            raise DomainError("not_found", "No resume for this track")
        _require_claims(obj["content"])
        if obj["status"] != "approved" or obj["approved_hash"] != manifest["content_hash"]:
            raise DomainError("stale_inputs", "Render does not match the approved resume version")
        obj = tx.update("resume", candidate_id, obj["object_id"], dict(obj, render=manifest), obj["version"])
        tx.event(candidate_id, "resume_rendered", ("resume", obj["object_id"]),
                 files=[f["name"] for f in manifest["files"]])
        return obj


def list_facts(store, candidate_id):
    with store.read() as tx:
        get_candidate(tx, candidate_id)
        return tx.list("fact", candidate_id)
