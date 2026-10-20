"""Núcleo de CrossSpice Web. Corre en Pyodide dentro de un Web Worker
(core-worker.js) y reutiliza tal cual el núcleo de escritorio: lector y
escritor .asc, símbolos .asy, base de modelos, conectividad y netlist.

La interfaz (JavaScript) le pide operaciones con `Api.call(op, json)` y
recibe JSON. El esquemático vive aquí; la interfaz solo dibuja lo que
devuelve `scene()`.

Librerías:
- básica propia de CrossSpice en /basic (siempre disponible);
- el lib.zip de LTspice que importa el estudiante, en /ltlib.zip. No se
  descomprime entero: los .asy y los .sub se sacan del zip cuando se usan.
"""
from __future__ import annotations

import base64
import copy
import json
import math
import os
import re
import sys
import time
import types
import unicodedata
import zipfile

# numpy y sqlite3 solo se usan en la versión de escritorio (resultados de
# ngspice y bases standard_*.db que el lib.zip no trae). Módulos mínimos
# para no descargarlos en la tableta.
if "numpy" not in sys.modules:
    sys.modules["numpy"] = types.ModuleType("numpy")
try:
    import sqlite3  # noqa: F401
except ImportError:
    _sq = types.ModuleType("sqlite3")

    class _SqError(Exception):
        pass

    def _no_sqlite(*_a, **_k):
        raise _SqError("sqlite3 no disponible")

    _sq.Error = _SqError
    _sq.connect = _no_sqlite
    sys.modules["sqlite3"] = _sq

from crossspice.models import ModelDB  # noqa: E402
from crossspice.netlist import (Netlister, build_connectivity, pin_positions,  # noqa: E402
                                replace_ideal_diodes, spice_node)
from crossspice.schematic import Component, Flag, Schematic, Text, Wire, dump_asc, parse_asc  # noqa: E402
from crossspice.simulate import expand_runs  # noqa: E402
from crossspice.textio import write_text  # noqa: E402
from crossspice.symbols import (WINDOW_ATTR, SymbolLibrary, Window, compose, ltspice_name,  # noqa: E402
                                text_layout, xform)

LTLIB = "/ltlib"
ZIPFILE = "/ltlib.zip"
BASIC = "/basic"
WORK = "/work"
GRID = 16


def snap(v: float) -> int:
    return int(round(v / GRID)) * GRID


# ================================================================ librerías
class WebSymbolLibrary(SymbolLibrary):
    """Índice de símbolos: primero los del lib.zip (sin extraerlos), luego
    los de la librería básica. Un .asy del zip se extrae al usarse."""

    def __init__(self, roots, zf: zipfile.ZipFile | None):
        self.zf = zf
        self.zip_members: dict[str, str] = {}
        super().__init__(roots)

    def _index(self) -> None:
        if self.zf is not None:
            for info in self.zf.infolist():
                parts = info.filename.split("/")
                if len(parts) < 3 or parts[1].lower() != "sym" or \
                        not parts[-1].lower().endswith(".asy"):
                    continue
                rel_dir = "/".join(parts[2:-1])
                base = parts[-1][:-4]
                full = LTLIB + "/sym/" + "/".join(parts[2:])
                rel = (rel_dir + "/" + base) if rel_dir else base
                key = rel.lower()
                if key in self.by_rel:
                    continue
                self.by_rel[key] = full
                self.by_base.setdefault(base.lower(), full)
                self.tree.setdefault(rel_dir, []).append(base)
                self.zip_members[full] = info.filename
            for names in self.tree.values():
                names.sort(key=str.lower)
        super()._index()

    def get(self, name, near=None):
        path = self.resolve(name, near)
        if path and path in self.zip_members and not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(self.zf.read(self.zip_members[path]))
        return super().get(name, near)


class Library:
    def __init__(self):
        self.zf: zipfile.ZipFile | None = None
        self.sub_index: dict[str, str] = {}
        self.symbols = None
        self.models = None
        self.netlister = None
        self.info: dict = {}

    def load_zip(self) -> dict:
        """Abre /ltlib.zip (lo escribe la interfaz) y reconstruye el índice."""
        t0 = time.time()
        zf = zipfile.ZipFile(ZIPFILE)
        n_sym = n_cmp = 0
        self.sub_index = {}
        for info in zf.infolist():
            parts = info.filename.split("/")
            if len(parts) < 3 or info.filename.endswith("/"):
                continue
            top = parts[1].lower()
            if top == "cmp":
                dest = os.path.join(LTLIB, "/".join(parts[1:]))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as fh:
                    fh.write(zf.read(info))
                n_cmp += 1
            elif top == "sub":
                self.sub_index[parts[-1].lower()] = info.filename
            elif top == "sym" and parts[-1].lower().endswith(".asy"):
                n_sym += 1
        if not n_sym:
            zf.close()
            raise ValueError("El archivo no parece el lib.zip de LTspice (no trae lib/sym).")
        self.zf = zf
        self.setup()
        self.info = {"symbols": n_sym, "models": len(self.models.models),
                     "subs": len(self.sub_index), "seconds": round(time.time() - t0, 2),
                     "mb": round(os.path.getsize(ZIPFILE) / 1e6, 1)}
        return self.info

    def unload_zip(self) -> None:
        if self.zf is not None:
            self.zf.close()
        self.zf = None
        self.sub_index = {}
        self.info = {}
        self.setup()

    def setup(self) -> None:
        os.makedirs(os.path.join(LTLIB, "sub"), exist_ok=True)
        os.makedirs(os.path.join(LTLIB, "sym"), exist_ok=True)
        if self.zf is None:
            # sin lib.zip no deben quedar restos de uno anterior
            for d in ("cmp",):
                p = os.path.join(LTLIB, d)
                if os.path.isdir(p):
                    for f in os.listdir(p):
                        os.remove(os.path.join(p, f))
        self.symbols = WebSymbolLibrary([os.path.join(LTLIB, "sym"),
                                         os.path.join(BASIC, "sym")], self.zf)
        self.models = ModelDB([LTLIB, BASIC])
        lib = self
        orig_find = self.models.find_lib_file

        def find_lib_file(name, near=None):
            key = os.path.basename(name.strip().strip('"').replace("\\", "/")).lower()
            member = lib.sub_index.get(key)
            if lib.zf is not None and member:
                dest = os.path.join(LTLIB, "sub", os.path.basename(member))
                if not os.path.exists(dest):
                    with open(dest, "wb") as fh:
                        fh.write(lib.zf.read(member))
                return dest
            return orig_find(name, near)

        basic_subs = {}
        sub_dir = os.path.join(BASIC, "sub")
        for f in sorted(os.listdir(sub_dir)) if os.path.isdir(sub_dir) else []:
            basic_subs[os.path.splitext(f)[0].lower()] = os.path.join(sub_dir, f)
        self.models.find_lib_file = find_lib_file
        self.models.subckt_index = lambda: basic_subs
        self.netlister = Netlister(self.symbols, self.models)


# ================================================================== dibujo
def _det(o: str) -> int:
    a = xform(o, 1, 0)
    b = xform(o, 0, 1)
    return a[0] * b[1] - a[1] * b[0]


def _p(x, y) -> str:
    return f"{round(x, 2):g} {round(y, 2):g}"


def shape_svg(kind: str, c: list, o: str) -> str:
    """Trazo SVG (coordenadas ya orientadas) de una forma de un .asy."""
    if kind == "LINE":
        a = xform(o, c[0], c[1])
        b = xform(o, c[2], c[3])
        return f"M{_p(*a)}L{_p(*b)}"
    if kind == "RECTANGLE":
        pts = [xform(o, x, y) for x, y in ((c[0], c[1]), (c[2], c[1]), (c[2], c[3]),
                                            (c[0], c[3]))]
        return "M" + "L".join(_p(*p) for p in pts) + "Z"
    x0, x1 = sorted((c[0], c[2]))
    y0, y1 = sorted((c[1], c[3]))
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    rx, ry = max((x1 - x0) / 2, 1e-9), max((y1 - y0) / 2, 1e-9)
    swap = xform(o, 1, 0)[0] == 0
    trx, try_ = (ry, rx) if swap else (rx, ry)
    if kind == "CIRCLE":
        ccx, ccy = xform(o, cx, cy)
        return (f"M{_p(ccx - trx, ccy)}A{_p(trx, try_)} 0 1 0 {_p(ccx + trx, ccy)}"
                f"A{_p(trx, try_)} 0 1 0 {_p(ccx - trx, ccy)}Z")
    if kind == "ARC":
        def ang(x, y):
            return math.atan2(-(y - cy) / ry, (x - cx) / rx)
        a1 = ang(c[4], c[5])
        a2 = ang(c[6], c[7])
        sweep = (a2 - a1) % (2 * math.pi) or 2 * math.pi
        p1 = xform(o, cx + rx * math.cos(a1), cy - ry * math.sin(a1))
        p2 = xform(o, cx + rx * math.cos(a2), cy - ry * math.sin(a2))
        sf = 0 if _det(o) > 0 else 1       # antihorario en pantalla
        if sweep >= 2 * math.pi - 1e-6:
            pm = xform(o, cx - rx * math.cos(a1), cy + ry * math.sin(a1))
            return (f"M{_p(*p1)}A{_p(trx, try_)} 0 0 {sf} {_p(*pm)}"
                    f"A{_p(trx, try_)} 0 0 {sf} {_p(*p1)}")
        large = 1 if sweep > math.pi else 0
        return f"M{_p(*p1)}A{_p(trx, try_)} 0 {large} {sf} {_p(*p2)}"
    return ""


def _is_box(shape, sym) -> bool:
    c = shape.coords
    return sym.prefix.upper().startswith("X") and abs(c[2] - c[0]) >= 64 and \
        abs(c[3] - c[1]) >= 64


def symbol_texts(comp, sym) -> list:
    """Textos visibles de un componente, como los dibuja la versión de
    escritorio (gui/items.py)."""
    o = comp.orient
    out = []
    attrs = dict(sym.attrs)
    attrs.update(comp.attrs)
    for num in sorted(set(sym.windows) | set(comp.windows)):
        w = comp.windows.get(num) or sym.windows.get(num)
        if w is None or w.just == "Invisible":
            continue
        key = WINDOW_ATTR.get(num)
        if key is None:
            continue
        val = attrs.get(key, "")
        if not val:
            continue
        x, y = xform(o, w.x, w.y)
        vert, just = text_layout(o, w.just)
        out.append([val, x, y, just, w.size, vert, "attr"])
    for t in sym.texts:
        x, y = xform(o, t.x, t.y)
        vert, just = text_layout(o, t.just)
        out.append([t.text, x, y, just, t.size, vert, "symtext"])
    for p in sym.pins:
        if p.just in ("NONE", "") or not p.name:
            continue
        j = p.just.capitalize() if not p.just.startswith("V") else "V" + p.just[1:].capitalize()
        off = p.offset
        dx, dy = {"Left": (off, 0), "Right": (-off, 0), "Top": (0, off),
                  "Bottom": (0, -off), "VLeft": (0, -off), "VRight": (0, off),
                  "VTop": (off, 0), "VBottom": (-off, 0)}.get(j, (0, 0))
        x, y = xform(o, p.x + dx, p.y + dy)
        vert, just = text_layout(o, j)
        out.append([p.name, x, y, just, 2, vert, "pin"])
    return out


def render_symbol(comp, sym) -> dict:
    if sym is None:
        return {"missing": True, "shapes": [["M-16 -16L16 -16L16 16L-16 16Z", 0, 0]],
                "texts": [[f"? {comp.symbol}", 20, 0, "Left", 2, False, "attr"]],
                "pins": [], "bb": [-16, -16, 16, 16]}
    o = comp.orient
    shapes = []
    xs, ys = [], []
    for s in sym.shapes:
        fill = 1 if (s.kind == "RECTANGLE" and (sym.symtype == "BLOCK" or _is_box(s, sym))) \
            else 0
        shapes.append([shape_svg(s.kind, s.coords, o), s.style, fill])
        n = 4 if s.kind == "ARC" else len(s.coords)
        for i in range(0, n, 2):
            x, y = xform(o, s.coords[i], s.coords[i + 1])
            xs.append(x)
            ys.append(y)
    pins = [list(xform(o, p.x, p.y)) for p in sym.pins]
    for x, y in pins:
        xs.append(x)
        ys.append(y)
    bb = [min(xs), min(ys), max(xs), max(ys)] if xs else [-16, -16, 16, 16]
    return {"shapes": shapes, "texts": symbol_texts(comp, sym), "pins": pins, "bb": bb}


# ------------------------------------------------- reglas del editor de escritorio
# (copiadas de gui/editor.py, que depende de Qt y no se puede importar aquí)
_JUSTS = ["Left", "Right", "Center", "Top", "Bottom", "VLeft", "VRight", "VCenter", "VTop",
          "VBottom"]


def auto_windows(comp, sym) -> None:
    """Al girar un componente sus atributos se reacomodan para leerse en
    horizontal, como en LTspice."""
    if sym is None:
        return
    o = comp.orient
    nums = [n for n in sorted(set(sym.windows) | set(comp.windows), key=lambda n: (n != 0, n))
            if (comp.windows.get(n) or sym.windows.get(n)).just != "Invisible"]
    for n in list(comp.windows):
        if comp.windows[n].just != "Invisible":
            del comp.windows[n]
    vertical = [n for n in nums if text_layout(o, sym.windows[n].just)[0]] \
        if all(n in sym.windows for n in nums) else []
    if not vertical:
        return
    xs, ys = [], []
    for s in sym.shapes:
        c = s.coords
        for i in range(0, 4, 2):
            x, y = xform(o, c[i], c[i + 1])
            xs.append(x)
            ys.append(y)
    if not xs:
        return
    cx = snap((min(xs) + max(xs)) / 2)
    a = xform(o, 1, 0)
    b = xform(o, 0, 1)
    for k, n in enumerate(vertical):
        if k == 0:
            px, py, just = cx, min(ys) - 8, "Bottom"
        else:
            px, py, just = cx, max(ys) + 8 + 28 * (k - 1), "Top"
        sx = a[0] * px + a[1] * py
        sy = b[0] * px + b[1] * py
        js = next((j for j in _JUSTS if text_layout(o, j) == (False, just)), "Left")
        comp.windows[n] = Window(int(sx), int(sy), js, sym.windows[n].size)


def _xf_point(op, x, y, cx, cy):
    rx, ry = xform(op, x - cx, y - cy)
    return int(cx + rx), int(cy + ry)


def flag_direction(sch, f) -> str:
    """El texto de la etiqueta va del lado opuesto al cable."""
    down = up = left = right = False
    for w in sch.wires:
        for (ax, ay, bx, by) in ((w.x1, w.y1, w.x2, w.y2), (w.x2, w.y2, w.x1, w.y1)):
            if (ax, ay) == (f.x, f.y):
                if bx == ax:
                    down |= by > ay
                    up |= by < ay
                elif by == ay:
                    right |= bx > ax
                    left |= bx < ax
    if down and not up:
        return "up"
    if up and not down:
        return "down"
    if right and not left and not (up or down):
        return "left"
    if left and not right and not (up or down):
        return "right"
    return "up"


def _ascii(text: str) -> str:
    """ngspice-WASM no produce resultados si la netlist trae caracteres no
    ASCII (p. ej. tildes en el título o en comentarios)."""
    if text.isascii():
        return text
    text = text.replace("µ", "u").replace("Ω", "")
    text = unicodedata.normalize("NFKD", text)
    return text.encode("ascii", "ignore").decode("ascii")


ANALYSIS_RX = re.compile(r"^\s*\.(tran|ac|dc|op|noise|tf)\b", re.I)


# ===================================================================== API
class Api:
    def __init__(self):
        self.lib = Library()
        self.lib.setup()
        self.sch = Schematic()
        self.name = "Sin título.asc"
        self.undo_stack: list = []
        self.redo_stack: list = []
        self.con = None
        os.makedirs(WORK, exist_ok=True)

    # ------------------------------------------------------------ llamadas
    def call(self, op: str, args_json: str = "{}") -> str:
        args = json.loads(args_json or "{}")
        fn = getattr(self, "op_" + op, None)
        if fn is None:
            raise ValueError(f"operación desconocida: {op}")
        return json.dumps(fn(**args))

    # ------------------------------------------------------------ librería
    def op_load_zip(self):
        return self.lib.load_zip()

    def op_unload_zip(self):
        self.lib.unload_zip()
        return self.op_scene()

    def op_lib_info(self):
        return {"zip": self.lib.zf is not None, **self.lib.info}

    def op_symbol_tree(self):
        tree = self.lib.symbols.tree
        dirs = sorted(tree.keys(), key=lambda s: (s != "", s.lower()))
        return [[d, tree[d]] for d in dirs]

    def op_symbol_preview(self, name: str, orient: str = "R0"):
        sym = self.lib.symbols.get(name)
        comp = Component(name, 0, 0, orient, {"InstName": (sym.prefix if sym else "X")[:1] + "?"})
        auto_windows(comp, sym)
        d = render_symbol(comp, sym)
        d["description"] = sym.description if sym else ""
        d["prefix"] = sym.prefix if sym else ""
        return d

    # ---------------------------------------------------------- documento
    def op_new(self, name: str = "Sin título.asc"):
        self.sch = Schematic()
        self.name = name
        self.undo_stack.clear()
        self.redo_stack.clear()
        return self.op_scene()

    def op_open(self, text: str, name: str):
        sch = parse_asc(text.replace("\r\n", "\n").replace("\r", "\n"))
        self.sch = sch
        self.name = name or "Sin título.asc"
        self.undo_stack.clear()
        self.redo_stack.clear()
        return self.op_scene()

    def op_save(self):
        return {"text": dump_asc(self.sch), "name": self.name}

    def op_export(self):
        """Archivo .asc tal como lo escribe LTspice (latin-1 o UTF-16LE, CRLF)."""
        path = os.path.join(WORK, "_export.asc")
        write_text(path, dump_asc(self.sch), self.sch.encoding)
        with open(path, "rb") as fh:
            data = fh.read()
        return {"b64": base64.b64encode(data).decode("ascii"), "name": self.name}

    def op_rename(self, name: str):
        self.name = name
        return {"name": name}

    def _push(self):
        self.undo_stack.append(copy.deepcopy(self.sch))
        del self.undo_stack[:-80]
        self.redo_stack.clear()

    def op_undo(self):
        if self.undo_stack:
            self.redo_stack.append(self.sch)
            self.sch = self.undo_stack.pop()
        return self.op_scene()

    def op_redo(self):
        if self.redo_stack:
            self.undo_stack.append(self.sch)
            self.sch = self.redo_stack.pop()
        return self.op_scene()

    # -------------------------------------------------------------- escena
    def _sym(self, c):
        return self.lib.symbols.get(c.symbol, [WORK])

    def op_scene(self):
        sch = self.sch
        try:
            con = build_connectivity(sch, self.lib.symbols, [WORK])
        except Exception:  # noqa: BLE001
            con = None
        self.con = con

        def net(x, y):
            n = con.net_at(x, y) if con else None
            return spice_node(n).lower() if n else None

        comps = []
        pins_all: dict = {}
        for i, c in enumerate(sch.components):
            sym = self._sym(c)
            d = render_symbol(c, sym)
            attrs = dict(sym.attrs if sym else {})
            attrs.update(c.attrs)
            d.update({"i": i, "x": c.x, "y": c.y, "orient": c.orient, "symbol": c.symbol,
                      "name": c.name, "prefix": (sym.prefix if sym else "")[:1].upper(),
                      "value": attrs.get("Value", ""),
                      "nets": [spice_node(n).lower() for n in (con.pin_nets.get(i, [])
                                                                if con else [])]})
            comps.append(d)
            if sym:
                for p in pin_positions(c, sym):
                    pins_all[p] = pins_all.get(p, 0) + 1
        wires = []
        for i, w in enumerate(sch.wires):
            n = con.wire_net[i] if con and i < len(con.wire_net) else None
            wires.append([w.x1, w.y1, w.x2, w.y2, spice_node(n).lower() if n else None])
        flags = [{"i": i, "x": f.x, "y": f.y, "name": f.name, "io": f.iopin,
                  "dir": flag_direction(sch, f), "net": net(f.x, f.y)}
                 for i, f in enumerate(sch.flags)]
        texts = [{"i": i, "x": t.x, "y": t.y, "just": t.just, "size": t.size,
                  "text": t.body, "directive": t.is_directive}
                 for i, t in enumerate(sch.texts)]
        graphics = []
        for g in sch.graphics:
            graphics.append([shape_svg(g.kind, g.coords, "R0"), g.style])
        junctions, openpins = self._junctions(pins_all)
        return {"comps": comps, "wires": wires, "flags": flags, "texts": texts,
                "graphics": graphics, "junctions": junctions, "openpins": openpins,
                "name": self.name, "undo": len(self.undo_stack), "redo": len(self.redo_stack),
                "analysis": self._analysis_line()}

    def _junctions(self, pins):
        sch = self.sch
        count: dict = {}
        for w in sch.wires:
            for p in ((w.x1, w.y1), (w.x2, w.y2)):
                count[p] = count.get(p, 0) + 1
        flags = {(f.x, f.y) for f in sch.flags}
        horiz: dict = {}
        vert: dict = {}
        for w in sch.wires:
            if w.y1 == w.y2:
                horiz.setdefault(w.y1, []).append(w)
            elif w.x1 == w.x2:
                vert.setdefault(w.x1, []).append(w)
        mids = {}
        for p in set(count) | set(pins) | flags:
            n = 0
            for w in horiz.get(p[1], ()):
                if min(w.x1, w.x2) < p[0] < max(w.x1, w.x2):
                    n += 2
            for w in vert.get(p[0], ()):
                if min(w.y1, w.y2) < p[1] < max(w.y1, w.y2):
                    n += 2
            mids[p] = n
        junctions = []
        for p in set(count) | set(pins):
            total = count.get(p, 0) + pins.get(p, 0) + mids.get(p, 0)
            if total >= 3 and count.get(p, 0) + mids.get(p, 0) >= 1:
                junctions.append(list(p))
        openpins = [list(p) for p, n in pins.items()
                    if n == 1 and count.get(p, 0) == 0 and mids.get(p, 0) == 0
                    and p not in flags]
        return junctions, openpins

    def _analysis_line(self) -> str:
        for t in self.sch.texts:
            if t.is_directive:
                for line in t.body.split("\n"):
                    if ANALYSIS_RX.match(line):
                        return line.strip()
        return ""

    # ---------------------------------------------------------- edición
    def _get(self, kind, i):
        return {"comp": self.sch.components, "wire": self.sch.wires, "flag": self.sch.flags,
                "text": self.sch.texts, "graphic": self.sch.graphics}[kind][i]

    def op_add_wires(self, segs: list):
        segs = [s for s in segs if (s[0], s[1]) != (s[2], s[3])]
        if not segs:
            return self.op_scene()
        self._push()
        for s in segs:
            self.sch.wires.append(Wire(*[int(v) for v in s]))
        return self.op_scene()

    def op_add_flag(self, x: int, y: int, name: str, io=None):
        self._push()
        self.sch.flags.append(Flag(int(x), int(y), name, io))
        res = self.op_scene()
        res["selected"] = ["flag", len(self.sch.flags) - 1]
        return res

    def op_add_comp(self, symbol: str, x: int, y: int, orient: str = "R0", attrs=None):
        sym = self.lib.symbols.get(symbol, [WORK])
        if sym is None:
            raise ValueError(f"No se encontró el símbolo '{symbol}'.")
        self._push()
        c = Component(ltspice_name(symbol), int(x), int(y), orient)
        c.attrs = {"InstName": self.sch.next_name(sym.prefix or "X")}
        for k, v in (attrs or {}).items():
            c.attrs[k] = v
        auto_windows(c, sym)
        self.sch.components.append(c)
        res = self.op_scene()
        res["selected"] = ["comp", len(self.sch.components) - 1]
        return res

    def op_add_text(self, x: int, y: int, text: str, directive: bool = True):
        self._push()
        body = text.replace("\n", "\\n")
        self.sch.texts.append(Text(int(x), int(y), "Left", 2, ("!" if directive else ";") + body))
        res = self.op_scene()
        res["selected"] = ["text", len(self.sch.texts) - 1]
        return res

    def _objs(self, items):
        return [(k, self._get(k, i)) for k, i in items]

    def op_move(self, items: list, dx: int, dy: int, stretch: bool = True):
        objs = self._objs(items)
        if not objs or (dx, dy) == (0, 0):
            return self.op_scene()
        self._push()
        moved = set()
        ids = {id(o) for _, o in objs}
        for k, o in objs:
            if k == "comp":
                sym = self._sym(o)
                if sym:
                    moved |= set(pin_positions(o, sym))
            elif k == "wire":
                moved |= {(o.x1, o.y1), (o.x2, o.y2)}
            elif k == "flag":
                moved.add((o.x, o.y))
        if stretch:
            for w in self.sch.wires:
                if id(w) in ids:
                    continue
                e1 = (w.x1, w.y1) in moved
                e2 = (w.x2, w.y2) in moved
                if e1:
                    w.x1 += dx
                    w.y1 += dy
                if e2:
                    w.x2 += dx
                    w.y2 += dy
        for k, o in objs:
            if k == "wire":
                o.x1 += dx
                o.y1 += dy
                o.x2 += dx
                o.y2 += dy
            elif k == "graphic":
                o.coords = [v + (dx if j % 2 == 0 else dy) for j, v in enumerate(o.coords)]
            else:
                o.x += dx
                o.y += dy
        self.sch.wires = [w for w in self.sch.wires if (w.x1, w.y1) != (w.x2, w.y2)]
        return self.op_scene()

    def op_move_wire_end(self, i: int, end: int, x: int, y: int):
        w = self.sch.wires[i]
        self._push()
        if end == 1:
            w.x1, w.y1 = int(x), int(y)
        else:
            w.x2, w.y2 = int(x), int(y)
        if (w.x1, w.y1) == (w.x2, w.y2):
            self.sch.wires.remove(w)
        return self.op_scene()

    def op_transform(self, items: list, op: str = "R90"):
        objs = self._objs(items)
        if not objs:
            return self.op_scene()
        xs, ys = [], []
        for k, o in objs:
            if k == "wire":
                xs += [o.x1, o.x2]
                ys += [o.y1, o.y2]
            elif k == "comp":
                sym = self._sym(o)
                if sym:
                    for (x, y) in pin_positions(o, sym):
                        xs.append(x)
                        ys.append(y)
                else:
                    xs.append(o.x)
                    ys.append(o.y)
            elif k in ("flag", "text"):
                xs.append(o.x)
                ys.append(o.y)
        if not xs:
            return self.op_scene()
        cx, cy = snap((min(xs) + max(xs)) / 2), snap((min(ys) + max(ys)) / 2)
        self._push()
        for k, o in objs:
            if k == "wire":
                o.x1, o.y1 = _xf_point(op, o.x1, o.y1, cx, cy)
                o.x2, o.y2 = _xf_point(op, o.x2, o.y2, cx, cy)
            elif k == "comp":
                sym = self._sym(o)
                # girar alrededor del centro de los pines
                o.x, o.y = _xf_point(op, o.x, o.y, cx, cy)
                o.orient = compose(o.orient, op)
                auto_windows(o, sym)
            elif k in ("flag", "text"):
                o.x, o.y = _xf_point(op, o.x, o.y, cx, cy)
        return self.op_scene()

    def op_delete(self, items: list):
        objs = self._objs(items)
        if not objs:
            return self.op_scene()
        self._push()
        ids = {id(o) for _, o in objs}
        s = self.sch
        s.wires = [w for w in s.wires if id(w) not in ids]
        s.components = [c for c in s.components if id(c) not in ids]
        s.flags = [f for f in s.flags if id(f) not in ids]
        s.texts = [t for t in s.texts if id(t) not in ids]
        s.graphics = [g for g in s.graphics if id(g) not in ids]
        return self.op_scene()

    # ------------------------------------------------------- propiedades
    def op_comp_info(self, i: int):
        c = self.sch.components[i]
        sym = self._sym(c)
        attrs = dict(sym.attrs if sym else {})
        attrs.update(c.attrs)
        prefix = (attrs.get("Prefix") or (sym.prefix if sym else "") or "X")
        models = []
        letter = prefix[:1].upper()
        if letter in "DQMJ":
            base = (sym.attrs.get("Value", "") if sym else "").upper()
            mtype = base if base in ("NPN", "PNP", "NMOS", "PMOS", "NJF", "PJF") else None
            models = [[e.name, e.mtype, e.manufacturer]
                      for e in self.lib.models.family_models(prefix, mtype)][:3000]
        return {"i": i, "symbol": c.symbol, "name": c.name, "prefix": prefix,
                "letter": letter, "description": sym.description if sym else "",
                "attrs": {k: attrs.get(k, "") for k in ("Value", "Value2", "SpiceLine",
                                                         "SpiceLine2", "SpiceModel")},
                "npins": len(sym.pins) if sym else 0, "models": models}

    def op_set_comp(self, i: int, attrs: dict):
        c = self.sch.components[i]
        sym = self._sym(c)
        self._push()
        for k, v in attrs.items():
            v = (v or "").strip()
            if k == "InstName":
                if v:
                    c.attrs[k] = v
                continue
            base = (sym.attrs.get(k, "") if sym else "")
            if v == base and k not in c.attrs:
                continue
            if not v and not base:
                c.attrs.pop(k, None)
            else:
                c.attrs[k] = v
        return self.op_scene()

    def op_set_flag(self, i: int, name: str, io=None):
        f = self.sch.flags[i]
        self._push()
        f.name = name.strip() or f.name
        f.iopin = io or None
        return self.op_scene()

    def op_set_text(self, i: int, text: str, directive: bool = True):
        self._push()
        t = self.sch.texts[i]
        if not text.strip():
            self.sch.texts.remove(t)
        else:
            t.text = ("!" if directive else ";") + text.replace("\r", "").replace("\n", "\\n")
        return self.op_scene()

    def op_set_analysis(self, cmd: str, x=None, y=None):
        """Cambia el comando de simulación (o lo agrega si no hay)."""
        cmd = cmd.strip()
        for t in self.sch.texts:
            if not t.is_directive:
                continue
            lines = t.body.split("\n")
            for k, line in enumerate(lines):
                if ANALYSIS_RX.match(line):
                    self._push()
                    lines[k] = cmd
                    t.text = "!" + "\\n".join(lines)
                    return self.op_scene()
        if x is None:
            xs = [c.x for c in self.sch.components] + [w.x1 for w in self.sch.wires] or [0]
            ys = [c.y for c in self.sch.components] + [w.y1 for w in self.sch.wires] or [0]
            x, y = snap(min(xs)), snap(max(ys) + 64)
        return self.op_add_text(x, y, cmd, True)

    # ------------------------------------------------------------- netlist
    def op_netlist(self):
        t0 = time.time()
        path = os.path.join(WORK, os.path.basename(self.name) or "circuito.asc")
        self.sch.path = path
        nl = self.lib.netlister.build(self.sch)
        runs = [{"label": lab, "text": self._web_netlist(txt)} for lab, txt in expand_runs(nl)]
        currents = {k: v for k, v in nl.current_vectors.items() if v}
        return {"runs": runs, "errors": nl.errors, "warnings": nl.warnings,
                "analyses": nl.analyses, "currents": currents, "t": round(time.time() - t0, 3)}

    def _web_netlist(self, text: str) -> str:
        """Netlist para ngspice-WASM: un solo texto (los .include en línea),
        sin code models XSPICE y sin pedir corrientes de dispositivo (@x[i]),
        que esta versión de ngspice no escribe."""
        text = replace_ideal_diodes(self._flatten(text))
        text = "\n".join(ln for ln in text.split("\n")
                         if not (ln.lower().startswith(".save") and "@" in ln))
        return _ascii(text)

    def _flatten(self, text: str, depth: int = 0) -> str:
        out = []
        for line in text.split("\n"):
            m = re.match(r'^\s*\.include\s+"([^"]+)"\s*$', line, re.I)
            if m and depth < 8 and os.path.isfile(m.group(1)):
                with open(m.group(1), encoding="utf-8", errors="replace") as fh:
                    body = fh.read()
                body = "\n".join(ln for ln in body.split("\n") if ln.strip().lower() != ".end")
                out.append(f"* --- {os.path.basename(m.group(1))} ---")
                out.append(self._flatten(body, depth + 1))
                continue
            out.append(line)
        return "\n".join(out)


api: Api | None = None


def start() -> Api:
    global api
    api = Api()
    return api
