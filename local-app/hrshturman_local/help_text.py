"""The product help the agent answers from (product_help): one Markdown file, sections by `## `."""
from functools import cache
from pathlib import Path

HELP = Path(__file__).with_name("help.md")


@cache
def sections():
    text = HELP.read_text(encoding="utf-8")
    head, *parts = text.split("\n## ")
    return head.strip(), {p.split("\n", 1)[0].strip(): "## " + p.strip() for p in parts}


def lookup(topic=None):
    """No topic: the intro and the list of sections. A topic: the sections that match its words best,
    a word in the title counting more than one in the text."""
    intro, by_title = sections()
    if not topic or not topic.strip():
        return {"intro": intro, "topics": list(by_title)}
    norm = lambda v: v.casefold().replace("ё", "е")
    words = [w for w in norm(topic).split() if len(w) > 2] or [norm(topic)]
    score = {t: 3 * sum(w in norm(t) for w in words) + sum(w in norm(body) for w in words)
             for t, body in by_title.items()}
    best = max(score.values())
    found = [t for t, v in score.items() if v == best][:2] if best else []
    return {"topic": topic, "sections": [by_title[t] for t in found] or None,
            "topics": list(by_title), **({} if found else {"note": "Раздел не найден: выбери тему из topics."})}
