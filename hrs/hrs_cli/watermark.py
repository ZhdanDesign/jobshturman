"""The watermark hrs writes into every methodology text: one encoding for hrs and the key server, kept in
hrshturman_core.watermark. This module only keeps the old import path."""
from hrshturman_core.watermark import (
    DIGITS,
    END,
    FIRST_WORD,
    ITEM,
    START,
    VERBATIM,
    encode,
    mark,
)

__all__ = ["DIGITS", "END", "FIRST_WORD", "ITEM", "START", "VERBATIM", "encode", "mark"]
