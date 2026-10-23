"""Russian typography for rendered text: non-breaking spaces where a line must not break.

The same rules as scripts/nbsp.py (the tool for the site and documents), applied to plain text:
  1. a 1–2 letter word or a short function word (SHORT) sticks to the next word: «в Москве», «при этом»;
  2. a number sticks to the next word: «15 лет», «+7 900 000-00-00»;
  3. a space before «—» is non-breaking;
  4. «бы», «ли», «же» stick to the previous word.
On top of them, for a CV:
  5. a Latin title-case phrase of 2–3 words stays whole: «Forward Deployed Engineer»;
  6. in headings, a short part between separators («|», «·», «—») stays on one line, and the separator
     stays at the end of the line before the break;
  7. a short last word of a paragraph does not stay alone on the last line.
Every rule only turns a space into U+00A0, so the text reads and extracts the same.
"""
import re

NBSP = "\u00a0"
SHORT = ["без", "для", "изо", "над", "обо", "подо", "при", "про", "через", "между", "перед", "после", "около", "вне", "или", "ни", "не", "но", "да", "что", "как", "так", "все", "уже", "лишь"]
PARTICLES = "бы|ли|же"
WORD = r"[A-Za-zА-Яа-яЁё]"
SHORT_RE = re.compile(rf"(?<![\w\-.])(?!(?:{PARTICLES})\b)({WORD}{{1,2}}|{'|'.join(SHORT)}) (?=\S)", re.IGNORECASE)
NUM_RE = re.compile(r"(?<![\w.,])([№$€₽+]?\d[\d.,:–\-]*[+%]?) (?=[\w«$№(+])")
DASH_RE = re.compile(r" (?=—)")
PARTICLE_RE = re.compile(rf"(?<=\w) (?=(?:{PARTICLES})(?!\w))")
LATIN_PHRASE = re.compile(r"\b[A-Z][\w\-]*(?: [A-Z][\w\-]*){1,2}\b")
SEPARATOR = re.compile(rf"([ {NBSP}]+[|·—] +)")
PHRASE_WORDS, PHRASE_CHARS = 4, 40
LAST_WORD_CHARS = 10


def fix_text(text):
    """Rules 1–5: safe for any text."""
    prev = None
    while prev != text:  # «и в Москве»: several short words in a row
        prev = text
        text = SHORT_RE.sub(lambda m: m.group(1) + NBSP, text)
    text = NUM_RE.sub(lambda m: m.group(1) + NBSP, text)
    text = DASH_RE.sub(NBSP, text)
    text = PARTICLE_RE.sub(NBSP, text)
    return LATIN_PHRASE.sub(lambda m: m.group(0).replace(" ", NBSP), text)


def keep_phrases(text, whole=False):
    """Rule 6: short parts between separators never break; whole=True keeps every part whole (contacts)."""
    out = []
    for part in SEPARATOR.split(text):
        if SEPARATOR.fullmatch(part):
            out.append(NBSP + part.strip(f" {NBSP}") + " ")
        elif whole or (len(part.split()) <= PHRASE_WORDS and len(part) <= PHRASE_CHARS):
            out.append(part.replace(" ", NBSP))
        else:
            out.append(part)
    return "".join(out)


def keep_last_word(text):
    """Rule 7: «… за шесть недель» never leaves «недель» alone on the last line."""
    head, sep, last = text.rpartition(" ")
    return f"{head}{NBSP}{last}" if sep and head and len(last) <= LAST_WORD_CHARS else text


def typeset(kind, text):
    """Typography of one CV line by its kind (see render_layout)."""
    text = fix_text(text)
    if kind == "contacts":
        return keep_phrases(text, whole=True)
    if kind in ("name", "title", "role", "dates", "project", "line"):
        text = keep_phrases(text)
    if kind in ("bullet", "scope", "line", "project"):
        text = keep_last_word(text)
    return text
