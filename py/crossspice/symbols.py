"""Símbolos de LTspice (.asy) y el índice de la librería de símbolos."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from .textio import read_text

# Ventanas de atributos (WINDOW n): números estándar de LTspice
WINDOW_ATTR = {0: "InstName", 1: "Type", 2: "Description", 3: "Value",
               38: "SpiceModel", 39: "SpiceLine", 40: "SpiceLine2",
               123: "Value2", 124: "SpiceLine2"}
ATTR_WINDOW = {"InstName": 0, "Value": 3, "SpiceModel": 38, "SpiceLine": 39,
               "SpiceLine2": 40, "Value2": 123, "Description": 2}


# ----------------------------------------------------------------- geometría
ORIENTATIONS = ["R0", "R90", "R180", "R270", "M0", "M90", "M180", "M270"]


def xform(orient: str, x: float, y: float) -> tuple[float, float]:
    """Aplica una orientación de LTspice a un punto del símbolo.
    Rn: giro horario de n grados en pantalla (y hacia abajo).
    Mn: giro Rn y luego espejo horizontal."""
    orient = orient.upper()
    rot = int(orient[1:] or 0) % 360
    if rot == 90:
        x, y = -y, x
    elif rot == 180:
        x, y = -x, -y
    elif rot == 270:
        x, y = y, -x
    if orient.startswith("M"):
        x = -x
    return x, y


def compose(orient: str, extra: str) -> str:
    """Orientación resultante de aplicar `extra` después de `orient`."""
    def mat(o):
        a = xform(o, 1, 0)
        b = xform(o, 0, 1)
        return (a, b)
    ma, mb = mat(orient)
    # aplicar extra a las imágenes de los vectores base
    ra = xform(extra, *ma)
    rb = xform(extra, *mb)
    for o in ORIENTATIONS:
        if mat(o) == (ra, rb):
            return o
    return orient


H_SWAP = {"Left": "Right", "Right": "Left", "Center": "Center", "Top": "Top",
          "Bottom": "Bottom"}
V_SWAP = {"Top": "Bottom", "Bottom": "Top", "Left": "Left", "Right": "Right",
          "Center": "Center"}


def text_layout(orient: str, just: str) -> tuple[bool, str]:
    """Calcula cómo se ve un texto de LTspice (justificación `just`) dentro de
    un símbolo con orientación `orient`.

    Devuelve (vertical, justificación_efectiva) donde la justificación es una
    de Left/Right/Center/Top/Bottom, interpretada en el marco del texto ya
    legible (horizontal, o vertical leyendo de abajo hacia arriba)."""
    vertical = just.startswith("V")
    j = just[1:] if vertical else just
    if j not in H_SWAP:
        j = "Left"
    d = (0, -1) if vertical else (1, 0)          # dirección de lectura
    n = (-1, 0) if vertical else (0, -1)         # "arriba" del texto
    d = xform(orient, *d)
    n = xform(orient, *n)
    if d in ((-1, 0), (0, 1)):                    # quedaría al revés
        d = (-d[0], -d[1])
        j = H_SWAP[j]
    want_n = (0, -1) if d == (1, 0) else (-1, 0)
    if n != want_n:
        j = V_SWAP[j]
    return d == (0, -1), j


# ------------------------------------------------------------------- modelo
@dataclass
class Pin:
    x: int
    y: int
    just: str = "NONE"
    offset: int = 8
    attrs: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.attrs.get("PinName", "")

    @property
    def order(self) -> int:
        try:
            return int(self.attrs.get("SpiceOrder", 0))
        except ValueError:
            return 0


@dataclass
class Shape:
    kind: str                  # LINE, RECTANGLE, CIRCLE, ARC
    coords: list
    style: int = 0             # 0 continua, 1 guiones, 2 puntos, ...


@dataclass
class SymText:
    x: int
    y: int
    just: str
    size: int
    text: str


@dataclass
class Window:
    x: int
    y: int
    just: str
    size: int


@dataclass
class Symbol:
    name: str
    path: str = ""
    symtype: str = "CELL"
    shapes: list = field(default_factory=list)
    texts: list = field(default_factory=list)
    windows: dict = field(default_factory=dict)   # num -> Window
    attrs: dict = field(default_factory=dict)
    pins: list = field(default_factory=list)

    @property
    def prefix(self) -> str:
        return self.attrs.get("Prefix", "")

    @property
    def description(self) -> str:
        return self.attrs.get("Description", "")

    def bbox(self) -> tuple[int, int, int, int]:
        xs: list[float] = []
        ys: list[float] = []
        for s in self.shapes:
            c = s.coords
            n = 4 if s.kind == "ARC" else len(c)
            xs += c[0:n:2]
            ys += c[1:n:2]
        for p in self.pins:
            xs.append(p.x)
            ys.append(p.y)
        for t in self.texts:
            xs.append(t.x)
            ys.append(t.y)
        if not xs:
            return (-16, -16, 16, 16)
        return (min(xs), min(ys), max(xs), max(ys))


def _split(line: str) -> list[str]:
    return line.split()


def parse_asy(text: str, name: str = "", path: str = "") -> Symbol:
    sym = Symbol(name=name, path=path)
    cur_pin: Pin | None = None
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        head, _, rest = line.partition(" ")
        h = head.upper()
        try:
            if h == "SYMBOLTYPE":
                sym.symtype = rest.strip().upper()
            elif h in ("LINE", "RECTANGLE", "CIRCLE", "ARC"):
                parts = rest.split()
                nums = [int(float(v)) for v in parts[1:]]
                need = 8 if h == "ARC" else 4
                style = nums[need] if len(nums) > need else 0
                sym.shapes.append(Shape(h, nums[:need], style))
            elif h == "TEXT":
                p = rest.split(None, 4)
                sym.texts.append(SymText(int(p[0]), int(p[1]), p[2], int(p[3]),
                                         p[4] if len(p) > 4 else ""))
            elif h == "WINDOW":
                p = rest.split()
                sym.windows[int(p[0])] = Window(int(p[1]), int(p[2]), p[3],
                                                int(p[4]) if len(p) > 4 else 2)
            elif h == "SYMATTR":
                k, _, v = rest.partition(" ")
                sym.attrs[k] = v
            elif h == "PIN":
                p = rest.split()
                cur_pin = Pin(int(p[0]), int(p[1]), p[2].upper() if len(p) > 2 else "NONE",
                              int(p[3]) if len(p) > 3 else 8)
                sym.pins.append(cur_pin)
            elif h == "PINATTR" and cur_pin is not None:
                k, _, v = rest.partition(" ")
                cur_pin.attrs[k] = v
        except (ValueError, IndexError):
            continue
    return sym


# ------------------------------------------------------------------ librería
class SymbolLibrary:
    """Índice insensible a mayúsculas de todos los .asy disponibles.

    Los nombres en los .asc pueden traer subcarpeta con barra invertida
    (p. ej. ``OpAmps\\LT1001``); aquí se resuelven también en Linux."""

    def __init__(self, sym_roots: list[str]):
        self.roots = [r for r in sym_roots if os.path.isdir(r)]
        self.by_rel: dict[str, str] = {}     # "opamps/lt1001" -> ruta
        self.by_base: dict[str, str] = {}    # "lt1001" -> ruta (primera)
        self.tree: dict[str, list[str]] = {}  # "OpAmps" -> ["LT1001", ...]
        self._cache: dict[str, Symbol] = {}
        self.extra_dirs: list[str] = []
        self._dir_cache: dict[str, dict] = {}
        self._resolve_cache: dict = {}
        self._index()

    def _index(self) -> None:
        for root in self.roots:
            for dirpath, dirnames, files in os.walk(root):
                dirnames.sort(key=str.lower)
                rel_dir = os.path.relpath(dirpath, root)
                rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
                for f in sorted(files, key=str.lower):
                    if not f.lower().endswith(".asy"):
                        continue
                    base = f[:-4]
                    full = os.path.join(dirpath, f)
                    rel = (rel_dir + "/" + base) if rel_dir else base
                    key = rel.lower()
                    if key in self.by_rel:
                        continue
                    self.by_rel[key] = full
                    self.by_base.setdefault(base.lower(), full)
                    self.tree.setdefault(rel_dir, []).append(base)

    def directories(self) -> list[str]:
        return sorted(self.tree.keys(), key=lambda s: (s != "", s.lower()))

    def invalidate_dir(self, directory: str | None = None) -> None:
        """Olvida los listados en caché (p. ej. tras guardar un .asy nuevo)."""
        self._dir_cache.clear()
        self._resolve_cache.clear()

    def _listing(self, d: str) -> dict:
        lst = self._dir_cache.get(d)
        if lst is None:
            try:
                lst = {e.lower(): e for e in os.listdir(d)}
            except OSError:
                lst = {}
            self._dir_cache[d] = lst
        return lst

    def _local(self, d: str, rel: str) -> str | None:
        cur = d
        for part in rel.split("/"):
            hit = self._listing(cur).get(part.lower())
            if hit is None:
                return None
            cur = os.path.join(cur, hit)
        return cur if os.path.isfile(cur) else None

    def resolve(self, name: str, near: list[str] | None = None) -> str | None:
        key = (name, tuple(near or ()))
        if key in self._resolve_cache:
            return self._resolve_cache[key]
        n = re.sub(r"[\\/]+", "/", name.strip())
        if n.lower().endswith(".asy"):
            n = n[:-4]
        res = None
        # Primero junto al esquemático (símbolos propios / jerárquicos)
        for d in (near or []) + self.extra_dirs:
            res = self._local(d, n + ".asy")
            if res:
                break
        if not res:
            res = self.by_rel.get(n.lower()) or self.by_base.get(n.split("/")[-1].lower())
        self._resolve_cache[key] = res
        return res

    def get(self, name: str, near: list[str] | None = None) -> Symbol | None:
        path = self.resolve(name, near)
        if not path:
            return None
        sym = self._cache.get(path)
        if sym is None:
            try:
                text, _ = read_text(path)
            except OSError:
                return None
            sym = parse_asy(text, name, path)
            self._cache[path] = sym
        return sym

    def all_names(self) -> list[str]:
        out = []
        for d, names in self.tree.items():
            for b in names:
                out.append(f"{d}/{b}" if d else b)
        return out


def ltspice_name(rel: str) -> str:
    """Nombre como lo escribe LTspice en el .asc (barra invertida)."""
    return rel.replace("/", "\\")


__all__ = ["Symbol", "Pin", "Shape", "SymText", "Window", "SymbolLibrary", "parse_asy",
           "xform", "compose", "text_layout", "ORIENTATIONS", "WINDOW_ATTR",
           "ATTR_WINDOW", "ltspice_name"]
