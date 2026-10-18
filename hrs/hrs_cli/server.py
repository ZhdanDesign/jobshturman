"""One MCP server for the agent: local app tools, collector tools and document import."""
from hrs_collector import mcp_server as collector_mcp
from hrs_collector.service import Collector
from hrshturman_core import DomainError, Store
from hrshturman_local import mcp_server as local_mcp
from mcp.server.mcpserver.exceptions import ToolError

from . import docs, license, paths, web
from .inproc import InProcessCollectorClient

INSTRUCTIONS = (
    "\n- Документы кандидата (резюме, письма, PDF, Word) загружай через document_import и ссылайся на "
    "его source_id в source_refs фактов.\n- Источники вакансий: list_sources, add_search_source, "
    "add_telegram_channel; сбор и перенос в карточки: vacancies_sync(run=true).\n- В начале работы вызови "
    "license_status. Если в ответе есть warning, перескажи его человеку со ссылкой на продление. Если "
    "methodology=false, процедуры этапов недоступны: объясни причину из message и не выдумывай методику."
    "\n- Где человек отвечает: ui_mode. Если режим не выбран, после ключа один раз спроси: отвечать в чате или в окне "
    "браузера (встроенном у агента или обычном) — и сохрани ui_mode(mode=...). Сменить можно в любой момент словами "
    "«открой в браузере» или «давай в чате».\n- chat: если ты умеешь показывать MCP Apps, вызови ui_show — анкета "
    "появится в чате; иначе задавай вопросы как обычно. builtin_browser: ui_open, затем открой url своим встроенным "
    "браузером (Browser pane Claude Code, браузер приложения Codex). external_browser: ui_open(external=true). "
    "Ответы со страницы сразу лежат в базе: читай их через intake_get, не переспрашивай."
)
APP_URI = "ui://hrshturman/app.html"
APP_MIME = "text/html;profile=mcp-app"


class Methodology:
    """The signed bundle once a key is entered; before that the library files of a project checkout."""

    def __init__(self, settings):
        self.files = local_mcp.FileLibrary(settings.library)
        self.bundle = license.BundleLibrary()

    def _source(self):
        return self.files if license.load() is None and self.files.root.is_dir() else self.bundle

    def names(self):
        return self._source().names()

    def read(self, name):
        return self._source().read(name)


def build(settings=None, collector: Collector | None = None):
    settings = settings or paths.settings()
    collector = collector or Collector(paths.collector_dir())
    client = InProcessCollectorClient(collector, settings.candidate_id)
    mcp = local_mcp.build_server(settings, collector=client, extra_instructions=INSTRUCTIONS,
                                 library=Methodology(settings))
    collector_mcp.register_tools(mcp, collector, default_candidate=settings.candidate_id)

    @mcp.tool()
    def document_import(path: str) -> dict:
        """Прочитать файл кандидата (PDF, DOCX, PPTX, XLSX, HTML, TXT, MD, RTF) в Markdown и сохранить локально.
        path — путь к файлу на этом компьютере. Возвращает source_id для source_refs фактов и текст документа."""
        try:
            return docs.import_document(path, paths.documents_dir(settings))
        except DomainError as exc:
            raise ToolError(f"{exc.code}: {exc}") from exc

    @mcp.tool()
    def license_login(key: str) -> dict:
        """Ввести ключ доступа из письма (HRS-XXXX-...): активировать его на этом компьютере и скачать методику.
        Ключ не повторяй в ответах и не сохраняй в файлы."""
        try:
            return license.login(key)
        except DomainError as exc:
            raise ToolError(f"{exc.code}: {exc}") from exc

    store = Store(settings.db_path).init()
    cid = settings.candidate_id
    screen = web.WebUI(store, cid)

    def domain(fn):
        try:
            return fn()
        except DomainError as exc:
            raise ToolError(f"{exc.code}: {exc}") from exc

    @mcp.tool()
    def ui_mode(mode: str | None = None) -> dict:
        """Где человек отвечает на вопросы: chat | builtin_browser | external_browser. Без mode — прочитать выбор.
        Спроси один раз и сохрани; смени, когда человек попросит."""
        return domain(lambda: {"ui_mode": web.set_mode(mode) if mode else web.get_mode(), "modes": list(web.MODES)})

    @mcp.tool()
    def ui_open(view: str = "intake", external: bool = False) -> dict:
        """Открыть страницу HR Штурман: view intake (анкета этапа 0) | progress (маршрут по этапам).
        Возвращает одноразовую ссылку на 127.0.0.1. external=true открывает её в обычном браузере на этом компьютере;
        иначе открой ссылку своим встроенным браузером. Ссылку не пересылай: она действует один раз."""
        def go():
            url = screen.link(view)
            return {"url": url, "opened_external": web.open_external(url) if external else False,
                    "note": "Ответы со страницы сразу видны через intake_get."}
        return domain(go)

    @mcp.tool(meta={"ui": {"resourceUri": APP_URI}})
    def ui_show(view: str = "intake") -> dict:
        """Показать анкету этапа 0 и маршрут прямо в чате (MCP App). Клиент без MCP Apps получит только данные:
        тогда задай вопросы текстом или предложи ui_open."""
        return domain(lambda: web.state(store, cid) | {"view": view})

    app_only = {"ui": {"visibility": ["app"]}}

    @mcp.tool(meta=app_only)
    def ui_state() -> dict:
        """Для страницы HR Штурман: анкета, вопросы блоков и маршрут."""
        return domain(lambda: web.state(store, cid))

    @mcp.tool(meta=app_only)
    def ui_answer(block: str, answer: str | list[str | int] | dict[str, str]) -> dict:
        """Для страницы HR Штурман: ответ кандидата на блок анкеты."""
        return domain(lambda: web.answer(store, cid, block, answer))

    @mcp.tool(meta=app_only)
    def ui_confirm(expected_version: int) -> dict:
        """Для страницы HR Штурман: кандидат нажал «Всё верно, подтверждаю»."""
        return domain(lambda: web.confirm(store, cid, expected_version))

    @mcp.resource(APP_URI, name="hrshturman-app", title="HR Штурман", mime_type=APP_MIME,
                  meta={"ui": {"prefersBorder": True}})
    def app_page() -> str:
        return web.page()

    @mcp.tool()
    def license_status(refresh: bool = False) -> dict:
        """Ключ доступа к методике: тариф, срок, предупреждение о продлении, скачана ли методика.
        refresh=true — спросить сервер сейчас (например, после продления ключа)."""
        return license.refresh(force=refresh)

    return mcp
