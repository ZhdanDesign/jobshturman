"""Stage 00: initial diagnostic and request map (spec, «Начальная диагностика»)."""
import re

from .common import DomainError, reject_secrets
from .workspace import get_candidate

PAINS = {
    1: "Не знаю, какую работу искать.",
    2: "Не понимаю, на какие должности могу претендовать.",
    3: "Не умею рассказать о себе так, чтобы заинтересовать работодателя.",
    4: "Не знаю, как выделиться среди других кандидатов.",
    5: "Трачу много времени на вакансии и отклики, а продвижения не вижу.",
    6: "На собеседовании теряюсь или не могу убедительно ответить.",
    7: "Не знаю, сколько просить и как обсуждать деньги.",
    8: "Поиск выматывает, всё труднее продолжать.",
}

# Matrix «трудность → следующий шаг → кто помогает». Stages refer to the 25-stage canon.
HELP_MATRIX = {
    1: {"next_step": "Разобрать опыт, пожелания и ограничения; выбрать проверяемые гипотезы направлений",
        "helpers": ["career_consultant", "agent"], "stages": [2, 3]},
    2: {"next_step": "Сопоставить подтверждённый опыт с реальными требованиями рынка; обозначить пробелы",
        "helpers": ["career_consultant", "agent"], "stages": [3, 4, 5, 6]},
    3: {"next_step": "Выбрать достижения, проверить факты, подготовить CV и профили",
        "helpers": ["career_consultant", "agent"], "stages": [7, 8, 9, 10]},
    4: {"next_step": "Найти подтверждённые сильные стороны и примеры вклада; проверить позиционирование",
        "helpers": ["career_consultant"], "stages": [10, 12]},
    5: {"next_step": "Проверить источники, фильтры, материалы, доставку и стадии воронки",
        "helpers": ["agent", "technical_specialist", "career_consultant"], "stages": [13, 14, 15, 22]},
    6: {"next_step": "Подготовить ответы и провести репетицию конкретной встречи",
        "helpers": ["career_consultant", "agent"], "stages": [24, 25]},
    7: {"next_step": "Уточнить ожидания и аргументы; подготовить переговоры об условиях",
        "helpers": ["career_consultant"], "stages": [24]},
    8: {"next_step": "Согласовать посильный ритм, уменьшить рутину; отдельно предложить обсудить поддержку",
        "helpers": ["agent", "psychologist_optional"], "stages": []},
}

SITUATION_FIELDS = {"target_roles", "employment_status", "deadlines", "search_history", "funnel",
                    "ready_materials", "constraints", "time_available", "help_wanted", "do_not_disclose"}
HELP_KINDS = {"career_consultant", "technical_specialist", "psychologist"}

# The questionnaire in blocks of 3–4 questions (diagnostic sections 1–2). The same blocks feed every
# way of asking: an MCP form, the agent's built-in choice, or a numbered list in the chat.
PAINS_INTRO = ("Не знаете, куда двигаться дальше? Откликаетесь, но вас редко зовут на встречи? Или вакансии есть, "
               "а сил на поиск уже не хватает? Отметьте два или три пункта, которые мешают вам сейчас. "
               "Начнем с самого важного для вас.")
SITUATION_BLOCKS = {
    "situation_goal": [
        ("target_roles", "Какие должности или направления вы рассматриваете? Если пока не знаете, так и напишите."),
        ("employment_status", "Вы сейчас работаете, заканчиваете проект или уже ищете новую работу?"),
        ("deadlines", "Какие сроки для вас важны?"),
    ],
    "situation_search": [
        ("search_history", "Как долго ищете и что уже пробовали: вакансии, контакты, консультанта, собственный агент?"),
        ("funnel", ("Что происходит в воронке: находите подходящие вакансии, отправляете отклики, получаете ответы, "
                   "доходите до встреч? Если точных чисел нет, можно описать словами.")),
        ("ready_materials", ("Какие материалы готовы: резюме, профили, проекты, список компаний, агент или таблица "
                            "откликов?")),
    ],
    "situation_limits": [
        ("constraints", ("Что для вас обязательно: страна и город, удаленная работа, доход, тип занятости, отрасль "
                        "и ограничения?")),
        ("time_available", "Сколько времени вы готовы уделять поиску?"),
        ("help_wanted", "Какая помощь сейчас была бы полезна?"),
        ("do_not_disclose", ("Есть ли сведения, которые нельзя показывать консультантам или использовать во внешних "
                            "откликах?")),
    ],
}
BLOCKS = ("pains", *SITUATION_BLOCKS)
SKIP_WORDS = ("пропуст", "пропуск", "skip")


def questions(block):
    """What to ask in a block, in a shape any agent can show (form, choice, numbered list)."""
    if block == "pains":
        return {"block": "pains", "intro": PAINS_INTRO, "multiple": True,
                "options": [{"id": i, "text": text} for i, text in PAINS.items()],
                "also": ["свой вариант своими словами", "пропустить"],
                "answer_format": "номера через запятую, например «1, 3»; можно дописать свой вариант текстом "
                                 "или написать «пропустить»"}
    if block not in SITUATION_BLOCKS:
        raise DomainError("invalid_schema", f"block must be one of {list(BLOCKS)}")
    return {"block": block, "multiple": False,
            "questions": [{"field": f, "text": q} for f, q in SITUATION_BLOCKS[block]],
            "answer_format": "ответ на каждый вопрос своими словами; пустой ответ оставляет вопрос открытым"}


def parse_pains(answer):
    """Normalize an answer about pains from any channel into (pain_ids, custom_pain, skipped).

    Accepts numbers («1, 3», «1 и 7»), the option texts the agent showed as choices, «пропустить»,
    and free text, which becomes the person's own pain. A list of such pieces is accepted too.
    """
    pieces = answer if isinstance(answer, list) else [answer]
    pain_ids, custom, skipped = [], [], False
    for piece in pieces:
        if isinstance(piece, int) and not isinstance(piece, bool):
            piece = str(piece)
        if not isinstance(piece, str):
            raise DomainError("invalid_schema", "answer must be text, a number or a list of them")
        text = piece.strip()
        matched = [i for i, label in PAINS.items() if text and label.rstrip(".").lower() in text.lower()]
        if matched:
            pain_ids += matched
            continue
        if text.lower().startswith(SKIP_WORDS):
            skipped = True
            continue
        rest = []
        for token in re.split(r"[,;\s]+", text):
            word = token.strip(".)»«\"")
            if word.isdigit() and not rest:
                if not 1 <= int(word) <= len(PAINS):
                    raise DomainError("invalid_schema", f"нет пункта {word}: номера от 1 до {len(PAINS)}")
                pain_ids.append(int(word))
            elif word.lower() in ("и", "and", ""):
                continue
            else:
                rest.append(token)
        own = " ".join(rest).strip()
        own = re.sub(r"^((и|ещё|еще|своё|свое|свой|своя|мой|моё|вариант|другое|плюс)[\s,]*)+[:\-—]\s*", "", own,
                     flags=re.IGNORECASE).strip()
        if own:
            custom.append(own)
    pain_ids = list(dict.fromkeys(pain_ids))
    custom_pain = "; ".join(custom) or None
    if pain_ids or custom_pain:
        skipped = False
    return pain_ids, custom_pain, skipped


def answer_block(store, candidate_id, block, answers, actor="candidate"):
    """Save one block of answers into the single active draft, keeping the other blocks.

    pains: answers is text or a list (see parse_pains). Situation blocks: answers is a dict
    {field: text} with fields of that block; empty text leaves the question open.
    """
    current = get_intake(store, candidate_id)
    data = {k: current[k] for k in ("pain_ids", "custom_pain", "skipped", "situation")} if current else \
        {"pain_ids": [], "custom_pain": None, "skipped": False, "situation": {}}
    if block == "pains":
        data["pain_ids"], data["custom_pain"], data["skipped"] = parse_pains(answers)
    elif block in SITUATION_BLOCKS:
        fields = {f for f, _ in SITUATION_BLOCKS[block]}
        if not isinstance(answers, dict) or set(answers) - fields:
            raise DomainError("invalid_schema", f"answers for {block}: {{field: text}} with fields {sorted(fields)}")
        situation = dict(data["situation"])
        for field, value in answers.items():
            value = (value or "").strip() if isinstance(value, str) else value
            if value:
                situation[field] = value
            else:
                situation.pop(field, None)
        data["situation"] = situation
    else:
        raise DomainError("invalid_schema", f"block must be one of {list(BLOCKS)}")
    obj = save_draft(store, candidate_id, data["pain_ids"], data["custom_pain"], data["skipped"], data["situation"],
                     current["version"] if current else None, actor=actor)
    return get_intake(store, candidate_id) | {"saved_version": obj["version"]}


def _validate(pain_ids, custom_pain, skipped, situation):
    if not isinstance(pain_ids, list) or any(p not in PAINS for p in pain_ids) or len(set(pain_ids)) != len(pain_ids):
        raise DomainError("invalid_schema", "pain_ids must be distinct values 1..8")
    if custom_pain is not None and (not isinstance(custom_pain, str) or not custom_pain.strip()):
        raise DomainError("invalid_schema", "custom_pain must be nonempty text or null")
    if skipped and (pain_ids or custom_pain):
        raise DomainError("invalid_schema", "skipped intake cannot carry selected pains")
    if not isinstance(situation, dict) or set(situation) - SITUATION_FIELDS:
        raise DomainError("invalid_schema", f"situation fields: {sorted(SITUATION_FIELDS)}")
    reject_secrets(situation)
    reject_secrets(custom_pain or "")


STORED_DATA_NOTICE = {
    "local": "Анкета хранится только в локальном workspace на этом компьютере. "
             "Если вы пользуетесь облачной моделью, ответы, которые вы ей показываете, обрабатывает её провайдер.",
    "service": "Анкета хранится в вашем аккаунте сервиса HR Штурман. "
               "Кто и зачем получает эти сведения, описано в условиях обработки данных сервиса.",
}


def request_map(intake, branch="local"):
    """The card shown to the person before confirmation (ДИАГ-02)."""
    steps = [dict(HELP_MATRIX[p], pain_id=p, pain=PAINS[p]) for p in intake["pain_ids"]]
    unknown = sorted(SITUATION_FIELDS - {"do_not_disclose", "help_wanted"} - set(intake["situation"]))
    return {
        "pains": steps,
        "custom_pain": intake["custom_pain"],
        "skipped": intake["skipped"],
        "situation": intake["situation"],
        "unknown_fields": unknown,
        "first_focus": steps[0]["pain_id"] if len(steps) > 3 else None,
        "stored_data_notice": STORED_DATA_NOTICE[branch],
    }


def save_draft(store, candidate_id, pain_ids, custom_pain=None, skipped=False, situation=None,
               expected_version=None, actor="candidate"):
    """Create or update the single active intake. Never creates a second request (ДИАГ-09)."""
    situation = situation or {}
    _validate(pain_ids, custom_pain, skipped, situation)
    data = {"pain_ids": pain_ids, "custom_pain": custom_pain, "skipped": skipped,
            "situation": situation, "status": "draft", "confirmed_from_version": None}
    with store.transaction(actor) as tx:
        get_candidate(tx, candidate_id)
        current = tx.find("intake", candidate_id, "active")
        if current is None:
            obj = tx.insert("intake", candidate_id, data, "intake", natural_key="active")
            tx.event(candidate_id, "intake_draft_created", ("intake", obj["object_id"]))
            return obj
        if expected_version is None:
            raise DomainError("state_version_conflict", "Intake exists; pass expected_version")
        obj = tx.update("intake", candidate_id, current["object_id"], data, expected_version)
        tx.event(candidate_id, "intake_draft_updated", ("intake", obj["object_id"]))
        return obj


def confirm(store, candidate_id, expected_version, actor="candidate"):
    """Explicit confirmation of exactly the shown card. A repeated click is a no-op."""
    with store.transaction(actor) as tx:
        current = tx.find("intake", candidate_id, "active")
        if current is None:
            raise DomainError("not_found", "No intake to confirm")
        if current["status"] == "confirmed" and current["confirmed_from_version"] == expected_version:
            return current
        if current["version"] != expected_version:
            raise DomainError("state_version_conflict", "Intake changed after it was shown; review again")
        data = dict(current, status="confirmed", confirmed_from_version=expected_version)
        obj = tx.update("intake", candidate_id, current["object_id"], data, expected_version)
        tx.event(candidate_id, "intake_confirmed", ("intake", obj["object_id"]), shown_version=expected_version)
        return obj


def set_paused(store, candidate_id, paused, actor="candidate"):
    """Pause stops new work without losing state (diagnostic 4.8, spec section 14)."""
    with store.transaction(actor) as tx:
        cand = get_candidate(tx, candidate_id)
        obj = tx.update("candidate", candidate_id, cand["object_id"], dict(cand, paused=bool(paused)), cand["version"])
        tx.event(candidate_id, "paused" if paused else "resumed", ("candidate", obj["object_id"]))
        return obj


def request_help(store, candidate_id, kind, allowed_contact, transfer_scope, actor="candidate"):
    """help_request keeps only the minimum; consultation details never enter the fact ledger."""
    if kind not in HELP_KINDS:
        raise DomainError("invalid_schema", f"kind must be one of {sorted(HELP_KINDS)}")
    reject_secrets([allowed_contact, transfer_scope])
    with store.transaction(actor) as tx:
        get_candidate(tx, candidate_id)
        existing = tx.find("help_request", candidate_id, kind)
        if existing and existing["status"] == "waiting_owner":
            return existing
        data = {"kind": kind, "allowed_contact": allowed_contact, "transfer_scope": transfer_scope,
                "status": "waiting_owner", "owner": None}
        if existing:
            obj = tx.update("help_request", candidate_id, existing["object_id"], data, existing["version"])
        else:
            obj = tx.insert("help_request", candidate_id, data, "help", natural_key=kind)
        tx.event(candidate_id, "help_requested", ("help_request", obj["object_id"]), kind=kind)
        return obj


def get_intake(store, candidate_id):
    """Active intake with the request map, or None before the first draft."""
    with store.read() as tx:
        cand = get_candidate(tx, candidate_id)
        current = tx.find("intake", candidate_id, "active")
    return current and dict(current, request_map=request_map(current, cand["branch"]))
