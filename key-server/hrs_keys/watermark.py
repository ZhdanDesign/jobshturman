"""Invisible watermark in the methodology texts that hrs hands to the agent.

Each key has a short id: the first 8 hex digits of sha256 over the key hash kept in the database.
The server sends the id to hrs with the key status; hrs writes it with zero-width characters into
every paragraph of every text it gives out. `hrs-keys watermark decode <file>` reads the ids back
from a copied text and finds their keys. hrs_cli/watermark.py carries the same encoding.
"""
import hashlib
import re

START, END = "\u2060", "\ufeff"
DIGITS = "\u200b\u200c\u200d\u2062"  # 2 bits each: an 8-digit hex id is 16 characters
FRAME = re.compile(f"{START}([{DIGITS}]{{16}}){END}")


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
