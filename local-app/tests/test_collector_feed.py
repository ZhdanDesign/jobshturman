"""Collector adapter against a fake collector that speaks CONTRACT.md 1.0-draft (stateful API)."""
import json
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from hrshturman_core import DomainError, Store
from hrshturman_core import applications as apps
from hrshturman_core import report
from hrshturman_core.workspace import create_candidate
from hrshturman_local import collector_feed

TOKEN = "test-token"


def vacancy(n, platform="hh", completeness="snippet", is_vacancy="likely", text="Кратко"):
    key = f"{platform}:{n}" if platform != "telegram" else f"telegram:1001:{n}"
    return {"item_key": key, "platform": platform, "external_id": str(n), "url": f"https://example.invalid/{n}",
            "title": f"Директор {n}", "company": "ООО Пример", "location": "Москва", "salary": None,
            "employment": None, "work_format": [], "published_at": "2026-10-08T10:00:00Z", "edited_at": None,
            "text": text, "contacts": {"emails": [], "telegram": [], "phones": [], "links": []},
            "is_vacancy": is_vacancy, "completeness": completeness, "content_hash": f"h{n}{completeness}",
            "fingerprint": f"f{n}", "raw": {}, "vacancy_id": f"vac_{n}", "candidate_id": "me", "source_id": "src-hh",
            "version": 1, "status": "normalized", "first_seen_at": "2026-10-09T08:00:00Z",
            "last_seen_at": "2026-10-09T09:00:00Z", "changed_at": "2026-10-09T08:00:00Z",
            "canonical_id": f"vac_{n}", "duplicate_status": "unique"}


class FakeCollector:
    def __init__(self):
        self.vacancies = [vacancy(1), vacancy(2, "telegram", "full", text="Полный пост о вакансии"),
                          vacancy(3, "telegram", "full", "unlikely", "Поздравляем с праздником")]
        self.run = {"run_id": "run_1", "candidate_id": "me", "status": "partial", "started_at": "2026-10-09T09:00:00Z",
                    "finished_at": "2026-10-09T09:05:00Z", "params": {}, "totals": {},
                    "sources": [{"source_id": "src-hh", "status": "ok", "pages": 1, "items_seen": 1, "items_new": 1,
                                 "items_updated": 0, "coverage": {"from_at": None, "to_at": None, "complete": True,
                                                                  "pages": 1, "items_seen": 1, "reason": None}},
                                {"source_id": "TG-001", "status": "partial", "pages": 10, "items_seen": 40,
                                 "items_new": 2, "items_updated": 0,
                                 "coverage": {"from_at": None, "to_at": None, "complete": False, "pages": 10,
                                              "items_seen": 40, "reason": "page_budget"}}]}
        self.sources = [{"source_id": "src-hh", "enabled": True, "title": "HH: финансовый директор", "access_status": "accessible"},
                        {"source_id": "TG-001", "enabled": True, "title": "C-level вакансии", "access_status": "accessible"},
                        {"source_id": "TG-002", "enabled": False, "title": "Выключен", "access_status": "disabled"}]
        self.runs_started = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, body, status=200):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def route(self):
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    return self.reply({"detail": "unauthorized"}, 401)
                url = urllib.parse.urlparse(self.path)
                query = urllib.parse.parse_qs(url.query)
                assert query["candidate_id"] == ["me"], query
                if url.path == "/v1/runs" and self.command == "POST":
                    fake.runs_started += 1
                    return self.reply(fake.run)
                if url.path == "/v1/runs":
                    return self.reply([fake.run])
                if url.path == "/v1/sources":
                    return self.reply(fake.sources)
                if url.path == "/v1/vacancies":  # two items per page to exercise keyset pagination
                    start = int((query.get("page_token") or ["0"])[0])
                    page = fake.vacancies[start:start + 2]
                    more = start + 2 < len(fake.vacancies)
                    return self.reply({"items": page, "next_page_token": str(start + 2) if more else None})
                if url.path.endswith("/fetch-full"):
                    vid = url.path.split("/")[3]
                    item = next(v for v in fake.vacancies if v["vacancy_id"] == vid)
                    item.update(completeness="full", text="Полное описание вакансии", content_hash="full")
                    return self.reply(item)
                return self.reply({"detail": "not found"}, 404)

            do_GET = do_POST = route

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


class CollectorFeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "ws.sqlite3").init()
        create_candidate(self.store, "me", "Europe/Moscow")
        self.fake = FakeCollector()
        self.client = collector_feed.CollectorClient(self.fake.url, "me", TOKEN)

    def tearDown(self):
        self.fake.server.shutdown()
        self.fake.server.server_close()
        self.tmp.cleanup()

    def cards(self):
        return {a["vacancy"]["source"] + ":" + a["vacancy"]["external_id"]: a
                for a in apps.list_applications(self.store, "me")}

    def test_sync_maps_vacancies_and_keeps_chatter_out_visibly(self):
        result = collector_feed.sync(self.store, "me", self.client)
        self.assertEqual(result["imported"]["added"], 2)
        self.assertEqual(result["skipped_not_vacancy"], 1)
        cards = self.cards()
        self.assertEqual(cards["hh:1"]["state"], "needs_full_text")  # search snippet is not a full text
        self.assertEqual(cards["telegram:1001:2"]["state"], "found")
        self.assertEqual(cards["hh:1"]["vacancy"]["collector_vacancy_id"], "vac_1")
        self.assertEqual(self.fake.runs_started, 0)

    def test_repeat_sync_is_idempotent_and_keeps_card_state(self):
        collector_feed.sync(self.store, "me", self.client)
        card = self.cards()["telegram:1001:2"]
        apps.score(self.store, "me", card["object_id"], card["version"], 4, "подходит", [], final=4)
        again = collector_feed.sync(self.store, "me", self.client, run=True)
        self.assertEqual(again["imported"], {"added": 0, "updated": 0, "unchanged": 2})
        self.assertEqual(self.cards()["telegram:1001:2"]["state"], "evaluated")
        self.assertEqual(self.fake.runs_started, 1)

    def test_fetch_full_unblocks_final_score(self):
        collector_feed.sync(self.store, "me", self.client)
        card = collector_feed.fetch_full(self.store, "me", self.client, self.cards()["hh:1"]["object_id"])
        self.assertEqual(card["state"], "found")
        self.assertEqual(card["vacancy"]["text"], "Полное описание вакансии")

    def test_coverage_is_incomplete_with_partial_source(self):
        collector_feed.sync(self.store, "me", self.client)
        cov = report.status(self.store, "me")["coverage"]
        self.assertFalse(cov["complete"])
        self.assertEqual((cov["counts"]["requested"], cov["counts"]["read_fully"], cov["counts"]["partial"]), (2, 1, 1))
        self.assertIn("Охват источников неполный", " ".join(report.status(self.store, "me")["limitations"]))

    def test_coverage_complete_when_every_enabled_source_read(self):
        self.fake.run["sources"] = self.fake.run["sources"][:1]
        self.fake.sources = self.fake.sources[:1]
        collector_feed.sync(self.store, "me", self.client)
        status = report.status(self.store, "me")
        self.assertTrue(status["coverage"]["complete"])
        self.assertEqual(status["limitations"], [])

    def test_errors_are_named(self):
        bad = collector_feed.CollectorClient(self.fake.url, "me", "wrong")
        with self.assertRaises(DomainError) as ctx:
            collector_feed.sync(self.store, "me", bad)
        self.assertEqual(ctx.exception.code, "not_authenticated")
        down = collector_feed.CollectorClient("http://127.0.0.1:9", "me", TOKEN, timeout=2)
        with self.assertRaises(DomainError) as ctx:
            collector_feed.sync(self.store, "me", down)
        self.assertEqual(ctx.exception.code, "service_unreachable")


if __name__ == "__main__":
    unittest.main()
