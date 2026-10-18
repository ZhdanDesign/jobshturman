"""The collector's HTTP API called in-process, so `hrs serve` needs no second server or port.

Same paths and payloads as collector/CONTRACT.md 1.0: the local app keeps using its
CollectorClient, only the transport changes.
"""
import os

from fastapi.testclient import TestClient
from hrs_collector.api import create_app
from hrs_collector.service import Collector
from hrshturman_core import DomainError
from hrshturman_local.collector_feed import CollectorClient


class InProcessCollectorClient(CollectorClient):
    def __init__(self, collector: Collector, candidate_id: str):
        super().__init__("http://hrs-collector.local", candidate_id, os.environ.get("HRS_COLLECTOR_TOKEN") or None)
        self.collector = collector
        self.http = TestClient(create_app(collector), base_url=self.base_url, raise_server_exceptions=False)

    def _call(self, method, path, query=None, body=None, timeout=None):
        query = {k: v for k, v in dict(query or {}, candidate_id=self.candidate_id).items() if v is not None}
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        # timeout is meaningless in-process: the call runs synchronously in this thread.
        res = self.http.request(method, path, params=query, headers=headers, json=body)
        if res.status_code >= 400:
            code = "not_authenticated" if res.status_code in (401, 403) else "service_unreachable"
            raise DomainError(code, f"Collector answered HTTP {res.status_code} on {path}")
        return res.json()
