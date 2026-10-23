"""The candidate's own screen: one HTML page for the questionnaire of stage 0 and the route progress.

The same page is shown three ways. In a browser (the agent's built-in one or the system browser) it is
served by hrs on 127.0.0.1 with a random port; the link carries a one-time token that turns into a
session cookie, and every request checks the Host header. In clients that render MCP Apps (Claude
desktop and web, ChatGPT and others) the page is the ui:// resource of the MCP server and talks to it
through the host. Answers go to the same workspace as the agent's tools, so either side sees the other's.
The page shows only questions and progress, never the methodology text.
"""
import json
import secrets
import threading
import time
import webbrowser
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from hrshturman_core import DomainError, intake, methodology, resume
from hrshturman_core import applications as apps
from hrshturman_core.brand import PRODUCT_NAME

from . import paths

STATIC = Path(__file__).with_name("static")
MODES = ("chat", "builtin_browser", "external_browser")
COOKIE = "hrs_session"
LINK_TTL = 15 * 60  # a one-time link not opened within this is dead
SESSION_TTL = 12 * 3600  # an idle session; `jbs serve` lives for days under an agent and must not collect them

def page() -> str:
    """app.html with the design tokens inlined, so one file works as a web page and as an MCP App."""
    css = (STATIC / "tokens.css").read_text(encoding="utf-8")
    html = (STATIC / "app.html").read_text(encoding="utf-8").replace("/*TOKENS*/", css)
    return html.replace("{{PRODUCT}}", PRODUCT_NAME)


# --- settings: how the candidate wants to answer ---

def _settings_file() -> Path:
    return paths.data_dir() / "settings.json"


def get_mode() -> str | None:
    try:
        return json.loads(_settings_file().read_text(encoding="utf-8")).get("ui_mode")
    except FileNotFoundError:
        return None


def set_mode(mode: str) -> str:
    if mode not in MODES:
        raise DomainError("invalid_schema", f"ui_mode: {', '.join(MODES)}")
    path = _settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    path.write_text(json.dumps(data | {"ui_mode": mode}, ensure_ascii=False, indent=1), encoding="utf-8")
    return mode


# --- what the page shows ---

def route_of(library) -> methodology.Route:
    """The stages as the methodology on this computer lists them; the format-1 route until it is downloaded."""
    return methodology.route_or_legacy(library) if library is not None else methodology.legacy_route()


def signals(store, cid) -> dict[str, tuple[str, int | None]]:
    """Progress signals from the workspace itself: signal → (status, count). Stages point at them in the manifest."""
    form = intake.get_intake(store, cid)
    facts = resume.list_facts(store, cid)
    cards = apps.list_applications(store, cid)
    try:
        cv = resume.get_resume(store, cid)
    except DomainError:
        cv = None
    states = [c["state"] for c in cards]
    out = {}
    if form:
        out["intake"] = ("done" if form["status"] == "confirmed" else "active", None)
    if facts:
        out["facts"] = ("active", len(facts))
        confirmed = sum(f["status"] == "confirmed" for f in facts)
        if confirmed:
            out["facts_confirmed"] = ("active", confirmed)
    if cv:
        out["resume"] = ("done" if cv.get("status") == "approved" else "active", None)
    if cards:
        out["vacancies"] = ("active", len(cards))
    if any(s != "found" and s != "needs_full_text" for s in states):
        out["scored"] = ("active", sum(s == "evaluated" for s in states))
    if any(s in ("approved", "deferred", "rejected_policy", "draft_ready", "authorized") for s in states):
        out["decided"] = ("active", None)
    if any(s in ("sent_confirmed", "send_unknown") for s in states):
        out["sent"] = ("active", None)
    if any(c.get("funnel") for c in cards):
        out["funnel"] = ("active", None)
    return out


def progress(store, cid, route: methodology.Route | None = None) -> list[dict]:
    """Where the candidate is on the route, from the workspace itself: done, active or todo per stage."""
    route = route or methodology.legacy_route()
    lit = signals(store, cid)
    out = []
    for stage in route.stages:
        status, count = lit.get(stage.progress, ("todo", None))
        out.append({"stage": stage.number, "id": stage.id, "title": stage.title,
                    "group": route.step_title(stage.step), "status": status, "count": count or None})
    return out


def state(store, cid, route: methodology.Route | None = None) -> dict:
    form = intake.get_intake(store, cid)
    return {"intake": form, "request_map": intake.request_map(form) if form else None,
            "blocks": [intake.questions(b) for b in intake.BLOCKS], "progress": progress(store, cid, route),
            "ui_mode": get_mode()}


def answer(store, cid, block, value, actor="candidate", route=None):
    intake.answer_block(store, cid, block, value, actor=actor)
    return state(store, cid, route)


def confirm(store, cid, expected_version, actor="candidate", route=None):
    intake.confirm(store, cid, expected_version, actor=actor)
    return state(store, cid, route)


# --- the local web server ---

class WebUI:
    """Started on first use inside `jbs serve` (a daemon thread) or by `jbs ui`."""

    def __init__(self, store, cid, library=None):
        self.store, self.cid, self.library = store, cid, library
        self.tokens: dict[str, float] = {}  # token -> issued at
        self.sessions: dict[str, float] = {}  # session -> last seen
        self.server: ThreadingHTTPServer | None = None
        self.lock = threading.Lock()

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def start(self) -> "WebUI":
        with self.lock:
            if self.server is None:
                self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self))
                threading.Thread(target=self.server.serve_forever, daemon=True, name="hrs-ui").start()
        return self

    def link(self, view: str = "intake") -> str:
        """A fresh one-time link. Opening it sets the session cookie; the token is then spent."""
        if view not in ("intake", "progress"):
            raise DomainError("invalid_schema", "view: intake or progress")
        self.start()
        self.expire()
        token = secrets.token_urlsafe(24)
        self.tokens[token] = time.monotonic()
        return f"http://127.0.0.1:{self.port}/?t={token}#{view}"

    def expire(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self.tokens = {t: at for t, at in self.tokens.items() if now - at < LINK_TTL}
        self.sessions = {s: at for s, at in self.sessions.items() if now - at < SESSION_TTL}

    def session_ok(self, session: str) -> bool:
        self.expire()
        if session not in self.sessions:
            return False
        self.sessions[session] = time.monotonic()
        return True

    def stop(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


def open_external(url: str) -> bool:
    return webbrowser.open(url)


def make_handler(ui: WebUI):
    class Handler(BaseHTTPRequestHandler):
        server_version = "hrs-ui"

        def log_message(self, *args):  # candidate data stays out of logs
            pass

        def _send(self, status, body, content_type="application/json; charset=utf-8", headers=()):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            for k, v in [("Content-Type", content_type), ("Content-Length", str(len(data))),
                         ("Cache-Control", "no-store"), ("Referrer-Policy", "no-referrer"),
                         ("Content-Security-Policy", ("default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                      "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")),
                         *headers]:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _error(self, status, code, message):
            self._send(status, {"error": code, "message": message})

        def _host_ok(self):
            if self.headers.get("Host") in (f"127.0.0.1:{ui.port}", f"localhost:{ui.port}"):
                return True
            self._error(HTTPStatus.FORBIDDEN, "forbidden", "Unexpected Host header")
            return False

        def _session_ok(self):
            cookie = SimpleCookie(self.headers.get("Cookie") or "")
            if COOKIE in cookie and ui.session_ok(cookie[COOKIE].value):
                return True
            self._error(HTTPStatus.UNAUTHORIZED, "unauthorized", "Откройте свежую ссылку от агента")
            return False

        def _run(self, fn):
            try:
                self._send(HTTPStatus.OK, fn())
            except DomainError as exc:
                self._error(HTTPStatus.CONFLICT, exc.code, str(exc))
            except (KeyError, TypeError, ValueError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, "invalid_schema", f"Bad request: {exc}")

        def do_GET(self):
            if not self._host_ok():
                return
            url = urlsplit(self.path)
            if url.path == "/":
                token = (parse_qs(url.query).get("t") or [""])[0]
                ui.expire()
                if token and token in ui.tokens:
                    del ui.tokens[token]
                    session = secrets.token_urlsafe(24)
                    ui.sessions[session] = time.monotonic()
                    return self._send(HTTPStatus.SEE_OTHER, b"", "text/plain", [
                        ("Location", "/app"),
                        ("Set-Cookie", f"{COOKIE}={session}; HttpOnly; SameSite=Strict; Path=/")])
                if not self._session_ok():
                    return
                return self._send(HTTPStatus.SEE_OTHER, b"", "text/plain", [("Location", "/app")])
            if not self._session_ok():
                return
            if url.path == "/app":
                return self._send(HTTPStatus.OK, page().encode("utf-8"), "text/html; charset=utf-8")
            if url.path == "/api/state":
                return self._run(lambda: state(ui.store, ui.cid, route_of(ui.library)))
            self._error(HTTPStatus.NOT_FOUND, "not_found", "Unknown route")

        def do_POST(self):
            if not self._host_ok() or not self._session_ok():
                return
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "invalid_schema", "JSON only")
            length = int(self.headers.get("Content-Length") or 0)
            if length > 100_000:
                return self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "invalid_schema", "Too large")

            def handle():
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/api/answer":
                    return answer(ui.store, ui.cid, body["block"], body["answer"], route=route_of(ui.library))
                if self.path == "/api/confirm":
                    return confirm(ui.store, ui.cid, body["expected_version"], route=route_of(ui.library))
                raise KeyError(self.path)
            self._run(handle)

    return Handler
