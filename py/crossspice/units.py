"""Números con sufijos de ingeniería al estilo SPICE/LTspice."""
from __future__ import annotations

import math
import re

_SUFFIX = [("meg", 1e6), ("mil", 25.4e-6), ("t", 1e12), ("g", 1e9), ("k", 1e3),
           ("m", 1e-3), ("u", 1e-6), ("µ", 1e-6), ("μ", 1e-6), ("n", 1e-9),
           ("p", 1e-12), ("f", 1e-15), ("a", 1e-18)]
_NUM = re.compile(r"^\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)([A-Za-zµμ]*)\s*$")


def parse_value(s: str) -> float | None:
    """'10k' -> 10000.0, '1Meg' -> 1e6, '4.7µ' -> 4.7e-6. None si no es número."""
    if s is None:
        return None
    m = _NUM.match(str(s))
    if not m:
        return None
    v = float(m.group(1))
    suf = m.group(2).lower()
    for k, mult in _SUFFIX:
        if suf.startswith(k):
            return v * mult
    return v


def fmt(v: float, unit: str = "", digits: int = 4) -> str:
    """Formato de ingeniería legible: 0.0047 -> '4.7m'."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    if v == 0:
        return f"0{unit}"
    prefixes = [(1e12, "T"), (1e9, "G"), (1e6, "Meg" if not unit else "M"), (1e3, "k"),
                (1, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"), (1e-12, "p"),
                (1e-15, "f"), (1e-18, "a")]
    a = abs(v)
    for mult, p in prefixes:
        if a >= mult * 0.9999999:
            s = f"{v / mult:.{digits}g}"
            return f"{s}{p}{unit}"
    return f"{v:.{digits}g}{unit}"


def spice_num(v: float) -> str:
    """Número para escribir en una netlist."""
    s = fmt(v, "", 6)
    return s.replace("µ", "u")
