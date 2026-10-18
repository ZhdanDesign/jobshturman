"""Local MCP server: thin tools over hrshturman_core.

Run:  python3 -m hrshturman_local.mcp_server                      (stdio, Claude Desktop / Claude Code)
      python3 -m hrshturman_local.mcp_server --http --port 8742   (streamable HTTP on 127.0.0.1)
"""
import argparse
import functools
import inspect
from datetime import datetime, timezone
from typing import Annotated

from mcp.server.mcpserver import Context, Elicit, ElicitationResult, MCPServer, Resolve
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from pydantic import BaseModel, Field, create_model

from hrshturman_core import DomainError, Store
from hrshturman_core import applications as apps
from hrshturman_core import checks, intake, report, resume
from hrshturman_core.workspace import create_candidate

from . import collector_feed, config, render

INSTRUCTIONS = """HR Shturman: локальный учёт поиска работы кандидата по методике из 25 этапов.
Приложение хранит факты, резюме, карточки откликов и воронку; ты интервьюируешь, пишешь черновики и оцениваешь вакансии.
Правила:
- Начни с status_report: он показывает этап, следующий шаг и блокеры.
- Не выдумывай факты и цифры. Каждое утверждение резюме ссылается на fact_id.
- Подтверждение анкеты, фактов, резюме, решение по вакансии, разрешение и отметку об отправке делай только после явных слов кандидата в этом разговоре.
- Отправку кандидат делает сам. Отмечай её только с доказательством (ссылка, скриншот, номер отклика).
- Ошибка с кодом state_version_conflict значит, что объект изменился: перечитай его и повтори с новой версией.
- Анкету этапа 00 задавай блоками через intake_ask: pains, situation_goal, situation_search, situation_limits.
  Если формы не было (asked_by="agent"), задай вопросы сам и передай ответ как есть в intake_answer.
  Потом покажи карту запроса и подтверждай intake_confirm только после явного «да».
- Процедуру текущего этапа читай инструментом stage_procedure(stage_id): по одному этапу, весь пакет методики не выдаётся.
  Её же отдаёт ресурс hrshturman://procedure/{stage_id}.
- Проверки делай инструментами, а не вручную: план действий этапа 06 — plan_check; письмо, кейс портфолио,
  сопроводительный текст и ответ работодателю (этапы 09, 10, 17, 19) — text_check; жёсткие ограничения
  вакансии (зарплата, формат, география) на этапе 15 — vacancy_constraints. Их ответ не пересчитывай."""


def tool_errors(fn):
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def async_wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except DomainError as exc:
                raise ToolError(f"{exc.code}: {exc}") from exc
        return async_wrapper

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except DomainError as exc:
            raise ToolError(f"{exc.code}: {exc}") from exc
    return wrapper


def resource_errors(fn):
    """Resources report a domain refusal (no key, key expired) as text the agent can show."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except DomainError as exc:
            raise ResourceError(f"{exc.code}: {exc}") from exc
    return wrapper


def _form_model(block):
    """Elicitation form for one questionnaire block: checkboxes for pains, text fields for the rest."""
    if block == "pains":
        fields = {f"pain_{i}": (bool, Field(False, title=text)) for i, text in intake.PAINS.items()}
        fields["own"] = (str, Field("", title="Свой вариант (необязательно)"))
        fields["skip"] = (bool, Field(False, title="Пропустить этот вопрос"))
    else:
        intake.questions(block)  # validates the block name
        fields = {f: (str, Field("", title=q)) for f, q in intake.SITUATION_BLOCKS[block]}
    return create_model(f"Intake_{block}", **fields)


def _ask_form(block: str, ctx: Context):
    """Resolver for intake_ask: an MCP form when the client can show one, otherwise nothing.

    From protocol 2026-07-28 the form rides an input-required result; before that it is a
    request in the middle of the tool call, which needs a back-channel to the client.
    """
    try:
        questions = intake.questions(block)
    except DomainError:
        return None  # the tool body reports the bad block name
    caps = ctx.client_capabilities
    if caps is None or caps.elicitation is None:
        return None
    session = ctx.session
    if (session.protocol_version or "") < "2026-07-28" and not session.can_send_request:
        return None
    message = questions.get("intro") or "Расскажите о своей ситуации. Можно ответить не на все вопросы."
    return Elicit(message, _form_model(block))


def _form_answers(block, data):
    values = data.model_dump()
    if block != "pains":
        return values
    if values["skip"]:
        return "пропустить"
    picked = [i for i in intake.PAINS if values[f"pain_{i}"]]
    return [*picked, *([f"свой: {values['own']}"] if values["own"].strip() else [])]


class FileLibrary:
    """The skills library as plain files (a project checkout). hrs swaps in the signed bundle."""

    def __init__(self, root):
        self.root = root

    def names(self):
        return [p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file()]

    def read(self, name):
        return (self.root / name).read_text(encoding="utf-8")


def stage_text(library, stage_id) -> str:
    """The procedure of one stage 0–25: its references/procedure.md, else its SKILL.md. Nothing of other stages."""
    prefix = f"skills/stage-{int(stage_id):02d}-"
    names = set(library.names())
    stages = sorted({n.split("/")[1] for n in names if n.startswith(prefix)})
    if not stages:
        raise DomainError("not_found", f"stage {int(stage_id)}")
    procedure_md = f"skills/{stages[0]}/references/procedure.md"
    return library.read(procedure_md if procedure_md in names else f"skills/{stages[0]}/SKILL.md")


def build_server(settings, collector=None, extra_instructions="", library=None):
    """collector: a CollectorClient-compatible object; by default the HTTP client from settings.
    library: an object with names() and read(name); by default the files in settings.library."""
    library = library or FileLibrary(settings.library)
    store = Store(settings.db_path).init()
    create_candidate(store, settings.candidate_id, settings.timezone, actor="installer")
    cid = settings.candidate_id
    mcp = MCPServer("hrshturman", instructions=INSTRUCTIONS + extra_instructions, log_level="WARNING")

    def tool(fn):
        return mcp.tool()(tool_errors(fn))

    # --- status ---

    @tool
    def status_report() -> dict:
        """Текущее состояние поиска: анкета, резюме, факты, воронка с ID, следующие шаги и ограничения."""
        return report.status(store, cid)

    @tool
    def set_pause(paused: bool) -> dict:
        """Пауза останавливает новые внешние действия без потери состояния. Только по просьбе кандидата."""
        return intake.set_paused(store, cid, paused, actor="candidate_via_agent")

    # --- stage 00: diagnostic ---

    @tool
    def intake_get() -> dict:
        """Анкета-диагностика: список 8 трудностей и текущая карта запроса (или null, если анкеты ещё нет)."""
        return {"pains": intake.PAINS, "intake": intake.get_intake(store, cid)}

    @tool
    def intake_save_draft(pain_ids: list[int], custom_pain: str | None = None, skipped: bool = False,
                          situation: dict | None = None, expected_version: int | None = None) -> dict:
        """Сохранить черновик анкеты. pain_ids из 1..8; можно больше трёх, свой ответ или пропуск.
        situation: target_roles, employment_status, deadlines, search_history, funnel, ready_materials,
        constraints, time_available, help_wanted, do_not_disclose. Для обновления передай expected_version."""
        obj = intake.save_draft(store, cid, pain_ids, custom_pain, skipped, situation, expected_version,
                                actor="candidate_via_agent")
        return dict(obj, request_map=intake.request_map(obj))

    @tool
    async def intake_ask(block: str,
                         form: Annotated[ElicitationResult[BaseModel], Resolve(_ask_form)]) -> dict:
        """Задать блок анкеты этапа 00: pains (8 трудностей), situation_goal, situation_search, situation_limits.
        Сервер сам показывает форму, если клиент умеет MCP-формы. Ответ формы сразу сохраняется в черновик.
        Если вернулось asked_by="agent" — формы не было или человек её закрыл: задай эти вопросы сам
        (встроенным выбором вариантов или нумерованным списком) и передай ответ в intake_answer."""
        questions = intake.questions(block)
        if form.action == "accept" and form.data is None:  # the resolver did not ask
            return {"asked_by": "agent", "reason": "client_has_no_forms", **questions}
        if form.action != "accept":
            return {"asked_by": "agent", "reason": f"form_{form.action}", **questions,
                    "note": "Форму закрыли или клиент её отклонил. Спроси, хочет ли человек ответить в чате "
                            "или пропустить блок."}
        card = intake.answer_block(store, cid, block, _form_answers(block, form.data), actor="candidate_via_form")
        return {"asked_by": "form", "block": block, "intake": card}

    @tool
    def intake_answer(block: str, answer: str | list[str | int] | dict[str, str]) -> dict:
        """Сохранить ответ на блок анкеты, полученный в чате или встроенным выбором агента.
        pains: текст как написал человек («1, 3», «пропустить», свой вариант) или список выбранных вариантов.
        situation_*: {поле: ответ} для полей этого блока; пустой ответ оставляет вопрос открытым.
        Возвращает черновик и карту запроса: покажи её человеку перед intake_confirm."""
        return intake.answer_block(store, cid, block, answer, actor="candidate_via_agent")

    @tool
    def intake_confirm(expected_version: int) -> dict:
        """Подтвердить карту запроса ровно той версии, которую кандидат видел. Только после явного «да»."""
        return intake.confirm(store, cid, expected_version, actor="candidate_via_agent")

    # --- stages 03, 07, 11: facts and resume ---

    @tool
    def fact_add(assertion: str, employer: str | None, source_refs: list[dict], period: dict | None = None,
                 metric: dict | None = None, disclosure: str = "public") -> dict:
        """Добавить факт о кандидате. source_refs: [{source_id, locator}] — откуда факт (файл, страница, ответ в интервью).
        disclosure: public | generalize_only | nda. Новый факт имеет статус reported, пока кандидат не подтвердит."""
        return resume.add_fact(store, cid, assertion, employer, period, source_refs, metric, disclosure)

    @tool
    def fact_set_status(fact_id: str, status: str, expected_version: int) -> dict:
        """Статус факта: reported | needs_confirmation | confirmed | rejected. confirmed — только со слов кандидата."""
        return resume.set_fact_status(store, cid, fact_id, status, expected_version, actor="candidate_via_agent")

    @tool
    def fact_list() -> list[dict]:
        """Все факты кандидата со статусами и источниками."""
        return resume.list_facts(store, cid)

    @tool
    def resume_get(track_id: str = "main") -> dict:
        """Мастер-резюме трека с результатом проверки claim map."""
        return resume.get_resume(store, cid, track_id)

    @tool
    def resume_save(content: dict, expected_version: int | None = None) -> dict:
        """Сохранить мастер-резюме. content: track_id, language, headline, summary[{text, fact_ids}],
        experience[{employer, title, start, end, scope, bullets[{text, fact_ids}]}], competencies[{text, fact_ids}],
        education, languages. Любая правка возвращает резюме в draft. Ответ содержит validation с ошибками."""
        return resume.save_resume(store, cid, content, expected_version, actor="agent")

    @tool
    def text_check(text: str, fact_ids: list[str]) -> dict:
        """Проверить текст для работодателя (письмо, кейс, ответ на вопрос) перед показом кандидату:
        плейсхолдеры, неизвестные, отклонённые и NDA-факты, числа без подтверждающего факта.
        fact_ids — факты, на которые текст опирается. Без fact_ids любое число в тексте — ошибка."""
        return checks.check_text(text, fact_ids, {f["object_id"]: f for f in resume.list_facts(store, cid)})

    @tool
    def plan_check(tasks: list[dict]) -> dict:
        """Этап 06, шаг 6: проверить план действий. tasks: [{id, title, owner, criterion, start, due, depends_on}],
        даты YYYY-MM-DD. Ошибки: нет владельца, критерия или срока, срок раньше начала, неизвестная зависимость,
        задача начинается раньше срока своей зависимости, цикл зависимостей."""
        return checks.check_plan(tasks)

    @tool
    def resume_approve(expected_version: int, track_id: str = "main") -> dict:
        """Утвердить резюме. Проходит только без ошибок и с подтверждёнными фактами. Только по слову кандидата."""
        return resume.approve_resume(store, cid, track_id, expected_version, actor="candidate_via_agent")

    @tool
    def resume_render(track_id: str = "main") -> dict:
        """Сделать DOCX и PDF из утверждённого резюме и проверить, что весь текст извлекается из файлов.
        Возвращает пути к файлам и sha256. Вёрстку проверяет человек: layout остаётся not_verified."""
        return render.render_resume(store, cid, settings.workspace, track_id)

    # --- stages 14-18, 22: vacancies and applications ---

    @tool
    def vacancy_constraints(application_id: str, policy: dict) -> dict:
        """Этап 15, шаг 4: зарплата, формат и география вакансии против политики кандидата.
        policy: salary_min (в месяц), currency (по умолчанию RUB), gross (true/false), formats [REMOTE, HYBRID,
        ON_SITE], locations [города или страны], relocation (true/false) — только то, что кандидат утвердил.
        Ответ: salary ok|below|unknown, format и location ok|conflict|unknown, hard_conflicts, notes.
        Неизвестная зарплата остаётся unknown и не считается ни нулём, ни совпадением."""
        app = next((a for a in apps.list_applications(store, cid) if a["object_id"] == application_id), None)
        if app is None:
            raise DomainError("not_found", f"application {application_id}")
        return checks.vacancy_constraints(app["vacancy"], policy)

    @tool
    def vacancies_import(items: list[dict], track_id: str = "main") -> dict:
        """Импорт вакансий: source, external_id, title, [url, company, text, published_at, collected_at].
        Повторный импорт обновляет снимок и не сбрасывает состояние карточки."""
        return apps.import_vacancies(store, cid, items, track_id, actor="agent")

    collector = collector or collector_feed.CollectorClient(settings.collector_url, cid, settings.collector_token)

    @tool
    def vacancies_sync(run: bool = False, lookback_days: int | None = None) -> dict:
        """Забрать вакансии у сборщика (HH, LinkedIn, Telegram) в карточки и обновить охват источников.
        run=true сначала запускает сбор по включённым источникам (может идти минуты). Повтор безопасен:
        состояние карточек не сбрасывается. partial и no_access в охвате не означают, что вакансий нет."""
        params = {"lookback_days": lookback_days} if lookback_days else None
        return collector_feed.sync(store, cid, collector, run=run, params=params)

    @tool
    def vacancy_fetch_full(application_id: str) -> dict:
        """Попросить сборщик дочитать полное описание вакансии (нужно для hh.ru и LinkedIn перед финальной оценкой)."""
        return collector_feed.fetch_full(store, cid, collector, application_id)

    @tool
    def applications_list(state: str | None = None) -> list[dict]:
        """Карточки откликов, при необходимости с фильтром по состоянию."""
        return [a for a in apps.list_applications(store, cid) if state is None or a["state"] == state]

    @tool
    def application_score(application_id: str, expected_version: int, preliminary: int, why: str,
                          fact_ids: list[str], final: int | None = None, risks: list[str] | None = None) -> dict:
        """Оценка 1–5 с объяснением и fact_ids. final допустим только при полном тексте вакансии."""
        return apps.score(store, cid, application_id, expected_version, preliminary, why, fact_ids, final, risks)

    @tool
    def application_decide(application_id: str, expected_version: int, decision: str, reason: str) -> dict:
        """Решение кандидата: approve | defer | reject. Молчание не согласие."""
        return apps.decide(store, cid, application_id, expected_version, decision, reason, actor="candidate_via_agent")

    @tool
    def application_prepare(application_id: str, expected_version: int, material: dict, channel: str) -> dict:
        """Точный комплект под вакансию (резюме, письмо, вложения). channel — куда и с какого аккаунта, например hh-main.
        Новый комплект снимает прежнее разрешение."""
        return apps.prepare(store, cid, application_id, expected_version, material, channel)

    @tool
    def application_authorize(application_id: str, expected_version: int, material_hash: str, channel: str,
                              expires_at: str) -> dict:
        """Кандидат разрешает отклик именно этим комплектом (material_hash) в этот канал до expires_at (ISO с часовым поясом)."""
        return apps.authorize(store, cid, application_id, expected_version, material_hash, channel, expires_at,
                              actor="candidate_via_agent")

    @tool
    def application_revoke(application_id: str, expected_version: int) -> dict:
        """Отозвать разрешение. Комплект сохраняется."""
        return apps.revoke(store, cid, application_id, expected_version, actor="candidate_via_agent")

    @tool
    def application_record_sent(application_id: str, expected_version: int, proof_ref: str,
                                observed_at: str | None = None) -> dict:
        """Кандидат откликнулся сам. proof_ref — ссылка, номер отклика или имя скриншота. Отметка хранится как «со слов кандидата»."""
        evidence = {"observed_at": observed_at or datetime.now(timezone.utc).isoformat(), "proof_ref": proof_ref}
        return apps.record_sent_by_candidate(store, cid, application_id, expected_version, evidence,
                                             actor="candidate_via_agent")

    @tool
    def application_record_employer_event(application_id: str, expected_version: int, kind: str, occurred_at: str,
                                          source: str, note: str | None = None) -> dict:
        """Ответ работодателя после отправки: awaiting_reply | invited | employer_rejected | employer_frozen |
        interview_done | offer_discussion. Нужны дата и источник (письмо, чат HH, звонок)."""
        return apps.record_employer_event(store, cid, application_id, expected_version, kind, occurred_at, source,
                                          note, actor="candidate_via_agent")

    # --- methodology: only the current stage, not the whole catalog ---

    @tool
    def stage_procedure(stage_id: int) -> str:
        """Процедура одного этапа 0–25 методики. Читай только этап, на котором кандидат (см. status_report)."""
        return stage_text(library, stage_id)

    @mcp.resource("hrshturman://procedure/{stage_id}")
    @resource_errors
    def procedure(stage_id: str) -> str:
        """Процедура этапа 0–25 из библиотеки навыков."""
        return stage_text(library, stage_id)

    @mcp.resource("hrshturman://runtime-constraints")
    @resource_errors
    def runtime_constraints() -> str:
        """Краткие общие требования к выполнению этапа."""
        return library.read("references/runtime_constraints.md")

    return mcp


def main(argv=None):
    parser = argparse.ArgumentParser(description="HR Shturman local MCP server")
    parser.add_argument("--http", action="store_true", help="streamable HTTP instead of stdio")
    parser.add_argument("--port", type=int, default=8742)
    args = parser.parse_args(argv)
    server = build_server(config.load())
    if args.http:
        server.run("streamable-http", host="127.0.0.1", port=args.port)
    else:
        server.run("stdio")


if __name__ == "__main__":
    main()
