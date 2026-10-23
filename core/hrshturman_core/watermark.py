"""Invisible watermark in the methodology texts hrs gives to the agent; the key server reads it back.

Each key has a short id: the first 8 hex digits of sha256 over the key hash kept in the server's database.
The server sends the id to hrs with the key status; hrs writes it with zero-width characters after the first
word of every paragraph, heading and list item. Code blocks (fenced or indented), table rows and the YAML
front matter stay byte for byte, so a copied paragraph carries the mark and nothing an agent might copy into
a command or a table changes. `hrs-keys watermark decode <file>` finds the ids in a copied text.
"""
import hashlib
import re

START, END = "\u2060", "\ufeff"
DIGITS = "\u200b\u200c\u200d\u2062"  # 2 bits each: an 8-digit hex id is 16 characters
FRAME = re.compile(f"{START}([{DIGITS}]{{16}}){END}")
FIRST_WORD = re.compile(r"\w(?=\s)")
ITEM = re.compile(r"\s*(#|[-*+] |\d+[.)] )")
VERBATIM = re.compile(r"(\s*\||    |\t)")  # table rows and indented code


def key_id(key_hash: str) -> str:
    return hashlib.sha256(f"watermark:{key_hash}".encode()).hexdigest()[:8]


def encode(wm: str) -> str:
    bits = int(wm, 16)
    return START + "".join(DIGITS[(bits >> shift) & 3] for shift in range(30, -1, -2)) + END


def decode(text: str) -> list[str]:
    """Ids found in the text, most frequent first."""
    counts: dict[str, int] = {}
    for frame in FRAME.findall(text):
        bits = 0
        for ch in frame:
            bits = bits << 2 | DIGITS.index(ch)
        wm = f"{bits:08x}"
        counts[wm] = counts.get(wm, 0) + 1
    return sorted(counts, key=counts.get, reverse=True)


def mark(text: str, wm: str) -> str:
    frame = encode(wm)
    head, body = "", text
    if text.startswith("---\n") and "\n---\n" in text[3:]:
        end = text.index("\n---\n", 3) + 5
        head, body = text[:end], text[end:]
    out, fenced, fresh = [], False, True
    for line in body.split("\n"):
        if line.lstrip().startswith("```"):
            fenced, fresh = not fenced, True
        elif not line.strip():
            fresh = True
        elif fenced or VERBATIM.match(line):
            fresh = False
        elif re.search(r"\w", line) and (fresh or ITEM.match(line)):
            word = FIRST_WORD.search(line)
            line = line[:word.end()] + frame + line[word.end():] if word else line + frame
            fresh = False
        out.append(line)
    return head + "\n".join(out)
