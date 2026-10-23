"""The watermark the key server reads back from a copied text (`hrs-keys watermark decode`): one encoding for
hrs and the server, kept in hrshturman_core.watermark. This module only keeps the old import path."""
from hrshturman_core.watermark import DIGITS, END, FRAME, START, decode, encode, key_id

__all__ = ["DIGITS", "END", "FRAME", "START", "decode", "encode", "key_id"]
