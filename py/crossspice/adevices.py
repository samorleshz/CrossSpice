"""Emulación de los dispositivos especiales 'A' de LTspice con elementos
estándar de ngspice (fuentes B, resistencias y capacitores).

Sintaxis LTspice:  Annn n1 n2 n3 n4 n5 n6 n7 n8 <modelo> [parámetros]
  * Compuertas (BUF, INV, AND, OR, XOR): entradas 1-5, salidas
    complementarias 6 (negada) y 7, común 8.  Las terminales conectadas al
    común se consideran sin uso.
  * SCHMITT / SCHMTBUF / SCHMTINV: igual, con umbral Vt e histéresis Vh.
  * OTA: transconductancia (G) de V(1,2), multiplicada por V(3,4) si se usa,
    con salida de corriente entre 7 y 8 (misma convención que una fuente G).

La equivalencia es aproximada (sin ruido, sin retardo de propagación), pero
suficiente para los macromodelos de amplificadores y controladores.
"""
from __future__ import annotations

import re

from .units import parse_value

SUPPORTED = {"OTA", "BUF", "INV", "AND", "OR", "XOR", "SCHMITT", "SCHMTBUF", "SCHMTINV"}


def _params(tokens: list[str]) -> tuple[dict, set]:
    text = " ".join(tokens)
    kv = {}
    for k, v in re.findall(r"(\w+)\s*=\s*(\{[^}]*\}|\"[^\"]*\"|[^\s=]+)", text):
        kv[k.lower()] = v
    rest = re.sub(r"(\w+)\s*=\s*(\{[^}]*\}|\"[^\"]*\"|[^\s=]+)", " ", text)
    flags = {t.lower() for t in rest.split()}
    return kv, flags


def _num(kv: dict, key: str, default: float | None) -> float | None:
    v = kv.get(key)
    if v is None:
        return default
    p = parse_value(v)
    return p if p is not None else None


def _e(v: str) -> str:
    """Valor para usar dentro de una expresión B: las llaves se conservan para
    que ngspice las sustituya por número (los .param no son visibles dentro
    de las expresiones de las fuentes B)."""
    v = v.strip()
    return v if v.startswith("{") else f"({v})"


def _expr(kv: dict, key: str, default: str) -> str:
    v = kv.get(key)
    if v is None:
        return default
    return _e(v)


def translate(line: str) -> tuple[list[str] | None, str]:
    """Devuelve (líneas_equivalentes, tipo). None si no se puede emular."""
    toks = line.split()
    if len(toks) < 10:
        return None, "?"
    name = toks[0]
    n = toks[1:9]
    model = toks[9].upper()
    kv, flags = _params(toks[10:])
    if model not in SUPPORTED:
        return None, model
    com = n[7]
    used = [x != com for x in n]
    base = re.sub(r"\W", "_", name)
    if model == "OTA":
        lines = _ota(base, n, used, kv, flags)
    elif model.startswith("SCHM"):
        lines = _schmitt(base, n, used, kv, model)
    else:
        lines = _gate(base, n, used, kv, model)
    return _hoist_params(base, lines), model


def _hoist_params(base: str, lines: list[str]) -> list[str]:
    """Pasa cada {expresión} de las fuentes B a un .param propio.

    ngspice evalúa mal una fuente B cuando la misma expresión entre llaves
    aparece repetida (p. ej. {slew*Cout}*tanh(x/{slew*Cout}) da 0)."""
    names: dict[str, str] = {}

    def sub(m):
        inner = m.group(1).strip()
        if inner not in names:
            names[inner] = f"cs_{base}_{len(names) + 1}".lower()
        return "{" + names[inner] + "}"
    out = []
    for ln in lines:
        if ln[:1].upper() == "B" and "{" in ln:
            ln = re.sub(r"\{([^{}]*)\}", sub, ln)
        out.append(ln)
    if names:
        out.insert(0, ".param " + " ".join(f"{v}={{{k}}}" for k, v in names.items()))
    return out


def _v(a: str, b: str) -> str:
    return f"V({a})" if b == "0" else f"V({a},{b})"


def _ota(base, n, used, kv, flags) -> list[str]:
    g = _expr(kv, "g", "1")
    if g.strip("()") in ("0", "0.0"):
        return [f"* {base}: OTA usada solo como fuente de ruido (omitida)"]
    out, com = n[6], n[7]
    vin = _v(n[0], n[1])
    if "ref" in kv:
        vin = f"({vin}-{_expr(kv, 'ref', '0')})"
    lin = f"{g}*{vin}"
    if used[2] or used[3]:
        lin += f"*{_v(n[2], n[3])}"
    iout = kv.get("iout", kv.get("isource"))
    isink = kv.get("isink")
    lines = []
    if "linear" in flags:
        expr = lin
        if iout or isink:
            hi = _expr(kv, "iout" if "iout" in kv else "isource", "1e308")
            lo = _expr(kv, "isink", f"(-{hi})")
            expr = f"min(max({lin},{lo}),{hi})"
    else:
        hi = _expr(kv, "iout" if "iout" in kv else "isource", "10u")
        if isink is not None:
            lo = f"abs({_expr(kv, 'isink', '0')})"
            expr = f"(({lin})>0 ? {hi}*tanh(({lin})/{hi}) : {lo}*tanh(({lin})/{lo}))"
        else:
            expr = f"{hi}*tanh(({lin})/{hi})"
    lines.append(f"B{base}_ota {out} {com} I={expr}")
    if "rout" in kv:
        lines.append(f"R{base}_rout {out} {com} {kv['rout']}")
    if "cout" in kv:
        lines.append(f"C{base}_cout {out} {com} {kv['cout']}")
    vh = _num(kv, "vhigh", 2.0)
    vl = _num(kv, "vlow", 0.0)
    rc = _e(kv.get("rclamp", "1"))
    vo = _v(out, com)
    parts = []
    if vh is None or abs(vh) < 1e300:
        h = _e(kv.get("vhigh", "2"))
        parts.append(f"({vo}>{h} ? ({vo}-{h})/{rc} : 0)")
    if vl is None or abs(vl) < 1e300:
        lo_ = _e(kv.get("vlow", "0"))
        parts.append(f"({vo}<{lo_} ? ({vo}-{lo_})/{rc} : 0)")
    if parts:
        lines.append(f"B{base}_clamp {out} {com} I={'+'.join(parts)}")
    return lines


def _levels(kv):
    vh = _e(kv.get("vhigh", "1"))
    vl = _e(kv.get("vlow", "0"))
    ref = _e(kv["ref"]) if "ref" in kv else f"(({vh}+{vl})/2)"
    return vh, vl, ref


def _outputs(base, n, used, kv, f_expr) -> list[str]:
    """Etapa de salida común: niveles, Rout y capacitancia/Tau."""
    vh, vl, _ = _levels(kv)
    com = n[7]
    rout = kv.get("rout", "1")
    lines = []
    tau = kv.get("tau")
    cout = kv.get("cout")
    for idx, inv in ((6, False), (5, True)):
        if not used[idx]:
            continue
        node = n[idx]
        f = f"(1-({f_expr}))" if inv else f"({f_expr})"
        mid = f"{base}_o{idx}"
        lines.append(f"B{base}_q{idx} {mid} {com} V={vl}+({vh}-{vl})*{f}")
        lines.append(f"R{base}_r{idx} {mid} {node} {rout}")
        if tau:
            t_, r_ = tau.strip("{}"), rout.strip("{}")
            lines.append(f"C{base}_c{idx} {node} {com} {{({t_})/({r_})}}")
        elif cout:
            lines.append(f"C{base}_c{idx} {node} {com} {cout}")
    return lines


def _gate(base, n, used, kv, model) -> list[str]:
    _vh, _vl, ref = _levels(kv)
    com = n[7]
    ins = [i for i in range(5) if used[i]]
    if not ins:
        return [f"* {base}: compuerta sin entradas"]
    vh, vl, _ = _levels(kv)
    w = f"(abs({vh}-{vl})*1e-3+1e-6)"
    s = [f"(0.5*(1+tanh(({_v(n[i], com)}-{ref})/{w})))" for i in ins]
    if model in ("BUF", "INV"):
        f = s[0]
        if model == "INV":
            f = f"(1-{f})"
    elif model == "AND":
        f = "*".join(s)
    elif model == "OR":
        f = "(1-" + "*".join(f"(1-{x})" for x in s) + ")"
    else:  # XOR: verdadero solo si exactamente una entrada lo es
        terms = []
        for i, x in enumerate(s):
            others = [f"(1-{y})" for j, y in enumerate(s) if j != i]
            terms.append("*".join([x] + others))
        f = "(" + "+".join(terms) + ")"
    return _outputs(base, n, used, kv, f)


def _schmitt(base, n, used, kv, model) -> list[str]:
    com = n[7]
    vin = _v(n[0], n[1]) if used[1] else _v(n[0], com)
    vt = _e(kv.get("vt", "0.5"))
    hy = _e(kv.get("vh", "0"))
    st = f"{base}_st"
    lines = [
        # Estado con realimentación: umbral alto/bajo según el estado actual
        f"B{base}_s {st}_d {com} V=({vin} > ({_v(st, com)}>0.5 ? {vt}-{hy} : "
        f"{vt}+{hy})) ? 1 : 0",
        f"R{base}_s {st}_d {st} 1k",
        f"C{base}_s {st} {com} 1p",
    ]
    f = f"V({st},{com})" if com != "0" else f"V({st})"
    if model == "SCHMTINV":
        f = f"(1-{f})"
    return lines + _outputs(base, n, used, kv, f)
