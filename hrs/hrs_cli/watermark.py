"""Invisible watermark in the methodology texts hrs gives to the agent.

The id comes from the key server with the key status. It is written with zero-width characters
after the first word of every paragraph, heading and list item. Code blocks (fenced or indented),
table rows and the YAML front matter stay byte for byte, so a copied paragraph carries the mark and
nothing an agent might copy into a command or a table changes.
The encoding matches key-server/hrs_keys/watermark.py, where `hrs-keys watermark decode` reads it.
"""
import re

START, END = "\u2060", "\ufeff"
DIGITS = "\u200b\u200c\u200d\u2062"
FIRST_WORD = re.compile(r"\w(?=\s)")
ITEM = re.compile(r"\s*(#|[-*+] |\d+[.)] )")
VERBATIM = re.compile(r"(\s*\||    |\t)")  # table rows and indented code


def encode(wm: str) -> str:
    bits = int(wm, 16)
    return START + "".join(DIGITS[(bits >> shift) & 3] for shift in range(30, -1, -2)) + END


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
