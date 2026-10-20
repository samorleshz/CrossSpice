"""Lectura y escritura de los archivos de texto de LTspice.

LTspice guarda los .asc/.asy en ISO-8859-1 o, cuando hace falta (p. ej. por
la letra µ), en UTF-16LE sin BOM. Aquí se detecta automáticamente.
"""
from __future__ import annotations

import os


def detect_encoding(raw: bytes) -> str:
    if raw.startswith(b"\xff\xfe"):
        return "utf-16"
    if raw.startswith(b"\xfe\xff"):
        return "utf-16"
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    # UTF-16LE sin BOM: un byte nulo en cada posición impar
    sample = raw[:400]
    if len(sample) >= 4 and sample[1::2].count(0) > len(sample) // 4:
        return "utf-16-le"
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "latin-1"


def read_text(path: str) -> tuple[str, str]:
    """Devuelve (texto, codificación)."""
    with open(path, "rb") as fh:
        raw = fh.read()
    enc = detect_encoding(raw)
    text = raw.decode(enc, errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n"), enc


def write_text(path: str, text: str, encoding: str | None = None) -> None:
    """Escribe con finales CRLF, como LTspice."""
    if encoding is None or encoding in ("utf-8", "utf-8-sig"):
        try:
            text.encode("latin-1")
            encoding = "latin-1"
        except UnicodeEncodeError:
            encoding = "utf-16-le"
    data = text.replace("\r\n", "\n").replace("\n", "\r\n")
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data.encode(encoding, errors="replace"))
    os.replace(tmp, path)


def is_probably_encrypted(path: str) -> bool:
    """Los modelos cifrados de ADI son binarios; los normales son texto."""
    try:
        with open(path, "rb") as fh:
            b = fh.read(512)
    except OSError:
        return False
    if not b:
        return False
    if detect_encoding(b).startswith("utf-16"):
        return False
    bad = sum(1 for c in b if c < 9 or (13 < c < 32))
    high = sum(1 for c in b if c > 127)
    return bad > len(b) * 0.05 or high > len(b) * 0.3
