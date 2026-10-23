"""Rules for answering employers in the person's own Telegram, mail and other chats.

JobShturman has no messenger of its own: an agent reads and writes through the person's integrations,
so these rules are all that keeps it from old threads and empty small talk.
"""
import json

from hrshturman_core import DomainError

from . import web

DEFAULT_MAX_AGE_DAYS = 14

RULES = (
    "Переписка с работодателями в Telegram, почте и других чатах человека.\n"
    "1. Читай только диалоги с работодателями и рекрутерами по откликам и вакансиям кандидата. Чужие чаты "
    "не открывай, новых диалогов сам не начинай.\n"
    "2. Если последнее сообщение работодателя старше max_age_days дней, не отвечай: только перечисли такие "
    "диалоги человеку одной строкой.\n"
    "3. Отвечай только на сообщение, которое ждёт действия кандидата: вопрос к нему, анкета или форма, "
    "приглашение или выбор времени встречи, просьба прислать документ, тестовое задание. На приветствия, "
    "«спасибо», «принято», рассылки, рекламу и просто разговор не отвечай.\n"
    "4. На одно сообщение — не больше одного ответа. Ответ строй только из подтверждённых фактов кандидата "
    "и проверь text_check.\n"
    "5. Ответ — черновик: покажи его человеку и отправь только после его явного «да». В плановом пробуждении "
    "ничего не отправляй, черновики оставь в сводке."
)


def max_age_days() -> int:
    try:
        value = json.loads(web._settings_file().read_text(encoding="utf-8")).get("inbox_max_age_days")
    except FileNotFoundError:
        return DEFAULT_MAX_AGE_DAYS
    return value if isinstance(value, int) else DEFAULT_MAX_AGE_DAYS


def set_max_age_days(days: int) -> int:
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 365:
        raise DomainError("invalid_schema", "max_age_days: от 1 до 365")
    path = web._settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    path.write_text(json.dumps(data | {"inbox_max_age_days": days}, ensure_ascii=False, indent=1), encoding="utf-8")
    return days


def rules(days: int | None = None) -> dict:
    if days is not None:
        set_max_age_days(days)
    return {"max_age_days": max_age_days(), "rules": RULES}
