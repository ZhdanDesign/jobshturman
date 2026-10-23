"""Adapter to the collector (block 1) by collector/CONTRACT.md 1.0, stateful API.

The collector owns sources, cursors and raw vacancies. This adapter only:
- optionally starts a run,
- reads normalized vacancies and maps them into the core import format,
- turns the latest run report into a coverage summary for the status screen.
No parsing of HH, LinkedIn or Telegram happens here.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

from hrshturman_core import DomainError, report
from hrshturman_core import applications as apps

CONTRACT_VERSION = "1.0"
READ_OK = {"ok", "no_new"}


class CollectorClient:
    def __init__(self, base_url, candidate_id, token=None, timeout=60):
        self.base_url = base_url.rstrip("/")
        self.candidate_id = candidate_id
        self.token = token
        self.timeout = timeout

    def _call(self, method, path, query=None, body=None, timeout=None):
        query = dict(query or {}, candidate_id=self.candidate_id)
        url = f"{self.base_url}{path}?{urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})}"
        req = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "Accept": "application/json"})
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as res:
                return json.loads(res.read())
        except urllib.error.HTTPError as err:
            code = "not_authenticated" if err.code in (401, 403) else "service_unreachable"
            raise DomainError(code, f"Collector answered HTTP {err.code} on {path}") from err
        except (urllib.error.URLError, TimeoutError, ConnectionError) as err:
            raise DomainError("service_unreachable", f"Collector is not reachable at {self.base_url}") from err

    def health(self):
        return self._call("GET", "/health")

    def run(self, params=None, source_ids=None):
        body = {"candidate_id": self.candidate_id, "params": params or {}}
        if source_ids:
            body["source_ids"] = source_ids
        return self._call("POST", "/v1/runs", {"wait": "true"}, body, timeout=max(self.timeout, 900))

    def latest_run(self):
        runs = self._call("GET", "/v1/runs", {"limit": 1})
        return runs[0] if runs else None

    def sources(self):
        return self._call("GET", "/v1/sources")

    def vacancies(self, since=None):
        token = None
        while True:
            page = self._call("GET", "/v1/vacancies", {"limit": 500, "page_token": token, "since": since})
            yield from page["items"]
            token = page.get("next_page_token")
            if not token:
                return

    def fetch_full(self, collector_vacancy_id):
        return self._call("POST", f"/v1/vacancies/{urllib.parse.quote(collector_vacancy_id)}/fetch-full", body={})


def to_core(vacancy):
    """Collector Vacancy -> core import item. A snippet has no full text, so the card waits for it."""
    platform, _, rest = vacancy["item_key"].partition(":")
    text = vacancy.get("text") or ""
    return {
        "source": platform,
        "external_id": rest,  # item_key without the platform prefix: stable, unique per platform
        "title": vacancy.get("title") or "Без названия",
        "company": vacancy.get("company"),
        "location": vacancy.get("location"),
        "salary": vacancy.get("salary"),
        "work_format": vacancy.get("work_format") or [],
        "url": vacancy.get("url"),
        "text": text if vacancy.get("completeness") == "full" and text.strip() else None,
        "published_at": vacancy.get("published_at"),
        "collected_at": vacancy.get("first_seen_at"),
        "collector_vacancy_id": vacancy["vacancy_id"],
    }


def coverage_summary(run, sources):
    """Coverage section of the status report (spec section 17). Errors stay visible per source."""
    enabled = {s["source_id"]: s for s in sources if s.get("enabled", True)}
    rows = []
    for rep in (run or {}).get("sources", []):
        src = enabled.get(rep["source_id"], {})
        cov = rep.get("coverage") or {}
        rows.append({
            "source_id": rep["source_id"],
            "title": src.get("title") or src.get("current_title"),
            "access_status": src.get("access_status"),
            "run_status": rep["status"],
            "items_seen": rep.get("items_seen", 0),
            "items_new": rep.get("items_new", 0),
            "from_at": cov.get("from_at"),
            "to_at": cov.get("to_at"),
            "complete": bool(cov.get("complete")) and rep["status"] in READ_OK,
            "reason": cov.get("reason"),
            "error_code": (rep.get("error") or {}).get("code"),
        })
    reported = {r["source_id"] for r in rows}
    not_run = sorted(set(enabled) - reported)
    counts = {"requested": len(enabled), "read_fully": sum(r["complete"] for r in rows),
              "partial": sum(r["run_status"] == "partial" for r in rows),
              "no_access": sum(r["run_status"] == "no_access" for r in rows),
              "failed": sum(r["run_status"] == "error" for r in rows),
              "skipped": sum(r["run_status"] == "skipped" for r in rows), "not_run": len(not_run)}
    return {
        "contract": CONTRACT_VERSION,
        "run_id": run and run["run_id"],
        "run_status": run and run["status"],
        "finished_at": run and run.get("finished_at"),
        "complete": bool(rows) and not not_run and all(r["complete"] for r in rows),
        "counts": counts,
        "sources": rows,
        "not_run": not_run,
    }


def sync(store, candidate_id, client, track_id="main", run=False, params=None):
    """Pull vacancies into application cards and refresh coverage. Idempotent; never resets a card's state."""
    run_report = client.run(params) if run else client.latest_run()
    skipped_unlikely = 0
    items = []
    for vacancy in client.vacancies():
        if vacancy.get("is_vacancy") == "unlikely":
            skipped_unlikely += 1  # chatter, not a vacancy; counted, not hidden
            continue
        items.append(to_core(vacancy))
    counts = apps.import_vacancies(store, candidate_id, items, track_id, actor="collector")
    coverage = coverage_summary(run_report, client.sources())
    coverage["skipped_not_vacancy"] = skipped_unlikely
    report.save_coverage(store, candidate_id, coverage)
    return {"imported": counts, "skipped_not_vacancy": skipped_unlikely, "coverage": coverage}


def fetch_full(store, candidate_id, client, application_id):
    """Ask the collector for the full text of one card and re-import it."""
    card = next((a for a in apps.list_applications(store, candidate_id) if a["object_id"] == application_id), None)
    if card is None:
        raise DomainError("not_found", "Unknown application")
    ref = card["vacancy"].get("collector_vacancy_id")
    if not ref:
        raise DomainError("not_configured", "This vacancy did not come from the collector")
    vacancy = client.fetch_full(ref)
    apps.import_vacancies(store, candidate_id, [to_core(vacancy)], card["track_id"], actor="collector")
    return next(a for a in apps.list_applications(store, candidate_id) if a["object_id"] == application_id)
