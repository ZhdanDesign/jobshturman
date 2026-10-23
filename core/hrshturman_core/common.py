"""Shared helpers: errors, time, canonical hashing, secret filter."""
import hashlib
import json
import re
import uuid
from datetime import UTC, datetime

SCHEMA_VERSION = "0.1.0"

# Ported from the draft workflow.py: secrets never enter the career ledger.
SECRET_KEYS = re.compile(r"(?i)(password|passwd|token|secret|cookie|api[_ -]?key|authorization)")
SECRET_TEXT = re.compile(
    r"(?i)(-----BEGIN .*PRIVATE KEY|\bBearer\s+[A-Za-z0-9._-]{8,}|\bsk-[A-Za-z0-9_-]{16,}"
    r"|(?:password|passwd|api[_ -]?key|access[_ -]?token|secret)\s*[=:]\s*[^\s,}\"]+)"
)
ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


class DomainError(ValueError):
    """Rule violation. `code` follows the error catalog of the spec (section 15)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def now():
    return datetime.now(UTC).isoformat()


def new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def reject_secrets(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if SECRET_KEYS.search(str(key)):
                raise DomainError("restricted_disclosure", "Secret-like field is not permitted")
            reject_secrets(item)
    elif isinstance(value, list):
        for item in value:
            reject_secrets(item)
    elif isinstance(value, str) and SECRET_TEXT.search(value):
        raise DomainError("restricted_disclosure", "Secret-like value is not permitted")


def redact_secrets(value):
    """Public text (a vacancy) may show a key or token as an example: hide that part instead of refusing."""
    if isinstance(value, dict):
        return {key: redact_secrets(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str):
        return SECRET_TEXT.sub("[скрыто]", value)
    return value


def check_id(value, name):
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise DomainError("invalid_schema", f"Invalid {name}")
    return value


def parse_time(value, name):
    try:
        stamp = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        stamp = None
    if stamp is None or stamp.tzinfo is None:
        raise DomainError("invalid_schema", f"{name} must be ISO time with timezone")
    return stamp
