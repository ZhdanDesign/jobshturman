#!/usr/bin/env python3
"""Висячие предлоги: приклеивает короткие слова и числа к следующему слову неразрывным пробелом.

Правила (детерминированные, идемпотентные):
  1. Слово из 1–2 букв и служебные слова из SHORT + пробел → NBSP («в Москве», «по шагам», «при этом»).
  2. Число (в том числе «$20», «300+», «№ 5») + пробел + слово → NBSP («4 региона», «15 лет»).
  3. Пробел перед тире «—» → NBSP.
  4. Пробел перед частицами «бы», «ли», «же» → NBSP.

Обрабатывает только видимый текст: в HTML пропускает теги, атрибуты, <script>, <style>, <code>, <pre>, <svg>;
в Markdown пропускает фронтматтер, код и адреса ссылок. Слова по обе стороны тега (<b>, <mark>) тоже склеивает.

  python3 scripts/nbsp.py landing/index.html landing/oferta/index.html   # исправить на месте
  python3 scripts/nbsp.py --check landing/index.html                     # только проверить, код выхода 1 при правках
"""
import re
import sys

NBSP = " "
TAG = ""  # на время обработки каждый тег и блок кода заменяется этим символом
T = f"{TAG}*"
SHORT = (
    "без для изо над обо подо при про через между перед после около вне "
    "или ни не но да что как так все уже лишь"
).split()
PARTICLES = "бы|ли|же"
WORD = r"[A-Za-zА-Яа-яЁё]"
SHORT_RE = re.compile(
    rf"(?<![\w\-.])(?!(?:{PARTICLES})\b)({WORD}{{1,2}}|{'|'.join(SHORT)})({T}) (?={T}\S)",
    re.IGNORECASE,
)
NUM_RE = re.compile(rf"(?<![\w.,])([№$€₽]?\d[\d.,:–\-]*[+%]?)({T}) (?={T}[\w«$№(])")
DASH_RE = re.compile(rf" (?={T}—)")
PARTICLE_RE = re.compile(rf"(?<=\w)({T}) (?={T}(?:{PARTICLES})(?!\w))")
HTML_SKIP = re.compile(r"<(script|style|code|pre|svg)\b.*?</\1>|<!--.*?-->|<[^>]+>", re.S | re.I)
MD_SKIP = re.compile(r"\A---\n.*?\n---\n|```.*?```|`[^`\n]*`|\]\([^)]*\)|https?://\S+", re.S)


def fix_text(text: str) -> str:
    prev = None
    while prev != text:  # «и в Москве»: несколько коротких подряд
        prev = text
        text = SHORT_RE.sub(lambda m: m.group(1) + m.group(2) + NBSP, text)
    text = NUM_RE.sub(lambda m: m.group(1) + m.group(2) + NBSP, text)
    text = DASH_RE.sub(NBSP, text)
    text = PARTICLE_RE.sub(lambda m: m.group(1) + NBSP, text)
    return text


def process(src: str, html: bool) -> str:
    kept = []

    def hide(m):
        kept.append(m.group(0))
        return TAG

    parts = fix_text((HTML_SKIP if html else MD_SKIP).sub(hide, src)).split(TAG)
    return "".join(p + (kept[i] if i < len(kept) else "") for i, p in enumerate(parts))


def main(argv):
    check = "--check" in argv
    changed = 0
    for path in (a for a in argv if a != "--check"):
        src = open(path, encoding="utf-8").read()
        dst = process(src, path.endswith((".html", ".htm")))
        n = dst.count(NBSP) - src.count(NBSP)
        if n:
            changed += 1
            print(f"{path}: {n} пробелов → неразрывные")
            if not check:
                open(path, "w", encoding="utf-8").write(dst)
    if not changed:
        print("висячих предлогов нет")
    return 1 if check and changed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
