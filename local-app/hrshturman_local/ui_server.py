"""Minimal local UI: one HTML page plus a JSON API over hrshturman_core.

Run:  python3 -m hrshturman_local.ui_server [--port 8741]
Listens only on 127.0.0.1. Requests with a foreign Host header are refused
(DNS rebinding), and POST requires application/json (no cross-site form posts).
"""
import argparse
import json
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from hrshturman_core import DomainError, Store
from hrshturman_core import applications as apps
from hrshturman_core import intake, report, resume
from hrshturman_core.workspace import create_candidate

from . import collector_feed, config, render

STATIC = Path(__file__).with_name("static")
ACTOR = "candidate"  # in the UI the candidate presses the button

APP_ACTIONS = {
    "decide": lambda s, c, i, v, b: apps.decide(s, c, i, v, b["decision"], b.get("reason", ""), actor=ACTOR),
    "authorize": lambda s, c, i, v, b: apps.authorize(s, c, i, v, b["material_hash"], b["channel"],
                                                      b["expires_at"], actor=ACTOR),
    "revoke": lambda s, c, i, v, b: apps.revoke(s, c, i, v, actor=ACTOR),
    "record_sent": lambda s, c, i, v, b: apps.record_sent_by_candidate(
        s, c, i, v, {"observed_at": b["observed_at"], "proof_ref": b["proof_ref"]}, actor=ACTOR),
    "employer_event": lambda s, c, i, v, b: apps.record_employer_event(
        s, c, i, v, b["kind"], b["occurred_at"], b["source"], b.get("note"), actor=ACTOR),
}


def make_handler(store, cid, port, collector=None, workspace=None):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    def get_routes(path):
        if path == "/api/status":
            return report.status(store, cid)
        if path == "/api/intake":
            return {"pains": intake.PAINS, "intake": intake.get_intake(store, cid)}
        if path == "/api/facts":
            return resume.list_facts(store, cid)
        if path == "/api/resume":
            try:
                return resume.get_resume(store, cid)
            except DomainError as exc:
                if exc.code == "not_found":
                    return None
                raise
        if path == "/api/applications":
            return apps.list_applications(store, cid)
        return HTTPStatus.NOT_FOUND

    def post_routes(path, body):
        if path == "/api/intake":
            obj = intake.save_draft(store, cid, body.get("pain_ids", []), body.get("custom_pain"),
                                    body.get("skipped", False), body.get("situation"),
                                    body.get("expected_version"), actor=ACTOR)
            return dict(obj, request_map=intake.request_map(obj))
        if path == "/api/intake/confirm":
            return intake.confirm(store, cid, body["expected_version"], actor=ACTOR)
        if path == "/api/pause":
            return intake.set_paused(store, cid, body["paused"], actor=ACTOR)
        if path == "/api/facts":
            return resume.add_fact(store, cid, body["assertion"], body.get("employer") or None, body.get("period"),
                                   body.get("source_refs", []), body.get("metric"),
                                   body.get("disclosure", "public"), actor=ACTOR)
        match = re.fullmatch(r"/api/facts/([\w.-]+)/status", path)
        if match:
            return resume.set_fact_status(store, cid, match[1], body["status"], body["expected_version"], actor=ACTOR)
        if path == "/api/resume":
            return resume.save_resume(store, cid, body["content"], body.get("expected_version"), actor=ACTOR)
        if path == "/api/resume/approve":
            return resume.approve_resume(store, cid, body.get("track_id", "main"), body["expected_version"], actor=ACTOR)
        if path == "/api/resume/render":
            return render.render_resume(store, cid, workspace, body.get("track_id", "main"))
        if path == "/api/vacancies/sync":
            return collector_feed.sync(store, cid, collector, run=bool(body.get("run")))
        match = re.fullmatch(r"/api/applications/([\w.-]+)/(\w+)", path)
        if match and match[2] == "fetch_full":
            return collector_feed.fetch_full(store, cid, collector, match[1])
        if match and match[2] in APP_ACTIONS:
            return APP_ACTIONS[match[2]](store, cid, match[1], body["expected_version"], body)
        return HTTPStatus.NOT_FOUND

    class Handler(BaseHTTPRequestHandler):
        server_version = "hrshturman-ui"

        def log_message(self, *args):  # keep candidate data out of the terminal
            pass

        def _send(self, status, body, content_type="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                        "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def _guard(self):
            if self.headers.get("Host") not in allowed_hosts:
                self._send(HTTPStatus.FORBIDDEN, {"error": "forbidden", "message": "Unexpected Host header"})
                return False
            return True

        def _run(self, fn):
            try:
                result = fn()
            except DomainError as exc:
                return self._send(HTTPStatus.CONFLICT, {"error": exc.code, "message": str(exc)})
            except (KeyError, TypeError, ValueError) as exc:
                return self._send(HTTPStatus.BAD_REQUEST, {"error": "invalid_schema", "message": f"Bad request: {exc}"})
            if result == HTTPStatus.NOT_FOUND:
                return self._send(HTTPStatus.NOT_FOUND, {"error": "not_found", "message": "Unknown route"})
            return self._send(HTTPStatus.OK, result)

        def do_GET(self):
            if not self._guard():
                return
            if self.path in ("/", "/index.html"):
                return self._send(HTTPStatus.OK, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            self._run(lambda: get_routes(self.path))

        def do_POST(self):
            if not self._guard():
                return
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self._send(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "invalid_schema", "message": "JSON only"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > 1_000_000:
                return self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "invalid_schema", "message": "Too large"})

            def handle():
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("body must be an object")
                return post_routes(self.path, body)
            self._run(handle)

    return Handler


def build_server(settings, port=8741):
    store = Store(settings.db_path).init()
    create_candidate(store, settings.candidate_id, settings.timezone, actor="installer")
    server = ThreadingHTTPServer(("127.0.0.1", port), BaseHTTPRequestHandler)
    # The Host check needs the bound port (port=0 picks a free one).
    collector = collector_feed.CollectorClient(settings.collector_url, settings.candidate_id, settings.collector_token)
    server.RequestHandlerClass = make_handler(store, settings.candidate_id, server.server_address[1], collector,
                                             settings.workspace)
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="HR Shturman local UI")
    parser.add_argument("--port", type=int, default=8741)
    args = parser.parse_args(argv)
    server = build_server(config.load(), args.port)
    print(f"HR Shturman: http://127.0.0.1:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
