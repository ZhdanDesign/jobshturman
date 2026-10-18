"""HR Shturman domain core shared by the local app and the web service."""
from .common import SCHEMA_VERSION, DomainError
from .store import Store

__all__ = ["SCHEMA_VERSION", "DomainError", "Store"]
