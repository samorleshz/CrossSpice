"""Conversión de un esquemático .asc a netlist SPICE, con las mismas reglas
que usa LTspice, y traducción de las extensiones de LTspice a ngspice."""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field

from . import adevices
from .models import ModelDB
from .schematic import Schematic, load_asc
from .symbols import SymbolLibrary, xform
from .textio import read_text
from .units import parse_value, spice_num

GROUND = "0"
DEFAULT_MODELS = {  # modelos por omisión de LTspice cuando Value es el tipo
    "D": "D", "NPN": "NPN", "PNP": "PNP", "NJF": "NJF", "PJF": "PJF",
    "NMOS": "NMOS", "PMOS": "PMOS", "SW": "SW", "CSW": "CSW",
}


# ------------------------------------------------------------- conectividad
class _UF:
    def __init__(self):
        self.p: dict = {}

    def find(self, a):
        p = self.p
        p.setdefault(a, a)
        root = a
        while p[root] != root:
            root = p[root]
        while p[a] != root:
            p[a], a = root, p[a]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _on_segment(px, py, x1, y1, x2, y2) -> bool:
    if (px, py) in ((x1, y1), (x2, y2)):
        return False
    if (x2 - x1) * (py - y1) != (y2 - y1) * (px - x1):
        return False
    return min(x1, x2) <= px <= max(x1, x2) and min(y1, y2) <= py <= max(y1, y2)


@dataclass
class PinRef:
    comp_index: int
    pin_index: int
    x: int
    y: int


@dataclass
class Connectivity:
    net_of: dict = field(default_factory=dict)        # (x, y) -> nombre de red
    wire_net: list = field(default_factory=list)      # índice de cable -> red
    pin_nets: dict = field(default_factory=dict)      # comp_index -> [red por pin]
    names: list = field(default_factory=list)         # redes con nombre de usuario
    warnings: list = field(default_factory=list)

    def net_at(self, x: int, y: int) -> str | None:
        return self.net_of.get((x, y))


def pin_positions(comp, sym) -> list[tuple[int, int]]:
    out = []
    for p in sym.pins:
        dx, dy = xform(comp.orient, p.x, p.y)
        out.append((comp.x + int(dx), comp.y + int(dy)))
    return out


def lt_wire_order(wires: list) -> list:
    """Orden en que LTspice guarda los cables: primer punto = el mayor en
    (y, x) y lista ordenada por ese punto."""
    def key(w):
        a, b = (w.y1, w.x1), (w.y2, w.x2)
        return max(a, b)
    return sorted(wires, key=key)


def build_connectivity(sch: Schematic, lib: SymbolLibrary, near: list[str],
                       nc_prefix: str = "NC_") -> Connectivity:
    uf = _UF()
    con = Connectivity()
    points: set = set()
    for w in sch.wires:
        a, b = (w.x1, w.y1), (w.x2, w.y2)
        uf.union(a, b)
        points.add(a)
        points.add(b)
    comp_pins: dict[int, list] = {}
    for i, c in enumerate(sch.components):
        sym = lib.get(c.symbol, near)
        if sym is None:
            continue
        pts = pin_positions(c, sym)
        comp_pins[i] = pts
        for p in pts:
            uf.find(p)
            points.add(p)
    for f in sch.flags:
        uf.find((f.x, f.y))
        points.add((f.x, f.y))

    # Puntos que tocan un cable en medio (uniones en T)
    horiz: dict[int, list] = {}
    vert: dict[int, list] = {}
    diag: list = []
    for w in sch.wires:
        if w.y1 == w.y2:
            horiz.setdefault(w.y1, []).append(w)
        elif w.x1 == w.x2:
            vert.setdefault(w.x1, []).append(w)
        else:
            diag.append(w)
    for (px, py) in points:
        for w in horiz.get(py, ()):
            if min(w.x1, w.x2) < px < max(w.x1, w.x2):
                uf.union((px, py), (w.x1, w.y1))
        for w in vert.get(px, ()):
            if min(w.y1, w.y2) < py < max(w.y1, w.y2):
                uf.union((px, py), (w.x1, w.y1))
        for w in diag:
            if _on_segment(px, py, w.x1, w.y1, w.x2, w.y2):
                uf.union((px, py), (w.x1, w.y1))

    # Etiquetas con el mismo nombre conectan redes distintas
    by_name: dict[str, tuple] = {}
    for f in sch.flags:
        key = f.name if f.name == GROUND else f.name.upper()
        pt = (f.x, f.y)
        if key in by_name:
            uf.union(by_name[key], pt)
        else:
            by_name[key] = pt

    root_name: dict = {}
    for f in sch.flags:
        r = uf.find((f.x, f.y))
        prev = root_name.get(r)
        if prev is None:
            root_name[r] = f.name
        elif prev != f.name:
            if f.name == GROUND or prev == GROUND:
                other = f.name if prev == GROUND else prev
                con.warnings.append(f"La red '{other}' está conectada a tierra.")
                root_name[r] = GROUND
            elif prev.upper() != f.name.upper():
                con.warnings.append(f"Las etiquetas '{prev}' y '{f.name}' están en la misma red.")
    con.names = sorted(set(root_name.values()))

    # Pines sin conexión: cada uno en su propia red
    pin_count: dict = {}
    for pts in comp_pins.values():
        for p in pts:
            r = uf.find(p)
            pin_count[r] = pin_count.get(r, 0) + 1
    wired_roots = {uf.find((w.x1, w.y1)) for w in sch.wires}
    flagged = set(root_name)

    # Nombres automáticos N001, N002... ordenados por posición
    # Igual que LTspice: las redes se numeran según el primer cable que las
    # contiene (en el orden del archivo); las demás, por posición.
    first: dict = {}
    for i, w in enumerate(lt_wire_order(sch.wires)):
        r = uf.find((w.x1, w.y1))
        if r not in first:
            first[r] = (0, i, 0)
    for p in points:
        r = uf.find(p)
        k = (1, p[1], p[0])
        if r not in first or (first[r][0] == 1 and k < first[r]):
            first[r] = k
    roots = sorted(first, key=lambda r: first[r])
    auto = 0
    nc = 0
    used = {n.upper() for n in root_name.values()}
    for r in roots:
        if r in root_name:
            continue
        if r not in wired_roots and r not in flagged and pin_count.get(r, 0) <= 1:
            nc += 1
            root_name[r] = f"{nc_prefix}{nc:02d}"
            continue
        while True:
            auto += 1
            name = f"N{auto:03d}"
            if name not in used:
                break
        root_name[r] = name
    for p in points:
        con.net_of[p] = root_name[uf.find(p)]
    con.wire_net = [con.net_of[(w.x1, w.y1)] for w in sch.wires]
    for i, pts in comp_pins.items():
        con.pin_nets[i] = [con.net_of[p] for p in pts]
    return con


# ------------------------------------------------------------ utilidades
_KV = re.compile(r"(\w+)\s*=\s*(\{[^}]*\}|\"[^\"]*\"|[^\s]+)")
_SAFE_NODE = re.compile(r"[\s(),=]")


def spice_node(name: str) -> str:
    return _SAFE_NODE.sub("_", name) or "N"


def split_kv(text: str) -> tuple[str, dict]:
    """Separa 'valor k=v k2=v2' en ('valor', {k: v}) respetando paréntesis."""
    params: dict = {}
    rest = []
    depth = 0
    i = 0
    tokens = []
    cur = ""
    while i < len(text):
        ch = text[i]
        if ch in "({":
            depth += 1
        elif ch in ")}":
            depth -= 1
        if ch.isspace() and depth <= 0:
            if cur:
                tokens.append(cur)
                cur = ""
        else:
            cur += ch
        i += 1
    if cur:
        tokens.append(cur)
    # unir "k = v" separados por espacios
    merged: list[str] = []
    for t in tokens:
        if merged and (t.startswith("=") or merged[-1].endswith("=")):
            merged[-1] += t
        else:
            merged.append(t)
    for t in merged:
        m = re.match(r"^([A-Za-z_]\w*)=(.+)$", t)
        if m and depth <= 0:
            params[m.group(1)] = m.group(2)
        else:
            rest.append(t)
    return " ".join(rest), params


_TRAIL_DOT = re.compile(r"(?<=\d)\.(?=[\s)*/+\-,}=<>]|$)")
_LT_FUNCS = [
    # uplim/dnlim(x, límite, z): límites suaves de LTspice (aquí, duros)
    (re.compile(r"(?<![\w.])uplim\s*\(", re.I), "__uplim("),
    (re.compile(r"(?<![\w.])dnlim\s*\(", re.I), "__dnlim("),
    # if(cond, a, b) de LTspice -> operador condicional de ngspice
    (re.compile(r"(?<![\w.])if\s*\(", re.I), "__if("),
    # mc(x, tol): uniforme entre x(1-tol) y x(1+tol)
    (re.compile(r"\bmc\s*\(", re.I), "__mc("),
    (re.compile(r"\bflat\s*\(", re.I), "__flat("),
    (re.compile(r"\bgauss\s*\(", re.I), "__gauss("),
    # table(x, x1, y1, x2, y2...): interpolación lineal
    (re.compile(r"\btable\s*\((?!\s*\))", re.I), "__table("),
]


def _table_expr(x: str, pts: list[str]) -> str:
    """table() de LTspice como condicionales anidados (ngspice no la tiene)."""
    pairs = [(pts[i], pts[i + 1]) for i in range(0, len(pts) - 1, 2)]
    if not pairs:
        return "0"
    x = f"({x})"
    expr = f"({pairs[-1][1]})"
    for i in range(len(pairs) - 1, 0, -1):
        (x0, y0), (x1, y1) = pairs[i - 1], pairs[i]
        seg = f"(({y0})+(({y1})-({y0}))*({x}-({x0}))/(({x1})-({x0})))"
        expr = f"({x}<({x1}) ? {seg} : {expr})"
    return f"({x}<({pairs[0][0]}) ? ({pairs[0][1]}) : {expr})"


def fix_param_line(line: str) -> str:
    """.param de LTspice -> ngspice: acepta 'X expr' sin '=' y expresiones
    con espacios; cada valor se encierra en llaves."""
    m = re.match(r"^(\s*\.params?)\s+(.*)$", line, re.I)
    if not m:
        return line
    body = m.group(2).strip()
    # LTspice permite '.param X table(...)' sin signo igual
    mm = re.match(r"^(\w+)\s+(?![=<>!])(\S.*)$", body)
    if mm and "=" not in body.split("(")[0]:
        body = f"{mm.group(1)}={mm.group(2)}"
    pos = [(m2.start(), m2.end(), m2.group(1))
           for m2 in re.finditer(r"(?<![<>!=\w])(\w+)\s*=(?!=)", body)]
    # Solo asignaciones a nivel 0 de paréntesis/llaves
    def depth_at(i):
        d = 0
        for ch in body[:i]:
            d += ch in "({"
            d -= ch in ")}"
        return d
    pos = [p for p in pos if depth_at(p[0]) == 0]
    if not pos:
        return line
    out = []
    for k, (s, e, name) in enumerate(pos):
        end = pos[k + 1][0] if k + 1 < len(pos) else len(body)
        val = body[e:end].strip()
        if not val:
            continue
        if not (val.startswith("{") and val.endswith("}")):
            if _is_plain_number(val):
                out.append(f"{name}={val}")
                continue
            val = "{" + val.strip("{}") + "}"
        out.append(f"{name}={val}")
    return ".param " + " ".join(out)


def _is_plain_number(v: str) -> bool:
    return parse_value(v) is not None


def _normalize_numbers(s: str) -> str:
    s = s.replace("µ", "u").replace("μ", "u").replace("Ω", "")
    s = _TRAIL_DOT.sub(".0", s)
    if "(" in s:
        s = _translate_funcs(s)
    return s


def _translate_funcs(s: str) -> str:
    """Funciones de LTspice sin equivalente directo en ngspice."""
    out = s
    for rx, name in _LT_FUNCS:
        if rx.search(out):
            out = rx.sub(name, out)
    if "__" not in out:
        return out

    def args_at(text, i):
        depth = 0
        start = i
        for k in range(i, len(text)):
            if text[k] == "(":
                depth += 1
            elif text[k] == ")":
                depth -= 1
                if depth == 0:
                    inner = text[start + 1:k]
                    parts, d, cur = [], 0, ""
                    for ch in inner:
                        if ch == "," and d == 0:
                            parts.append(cur)
                            cur = ""
                            continue
                        d += ch == "("
                        d -= ch == ")"
                        cur += ch
                    parts.append(cur)
                    return [p.strip() for p in parts], k
        return None, -1

    for fn in ("__uplim", "__dnlim", "__if", "__table", "__mc", "__flat", "__gauss"):
        while True:
            i = out.find(fn + "(")
            if i < 0:
                break
            args, end = args_at(out, i + len(fn))
            if args is None:
                break
            if fn in ("__uplim", "__dnlim") and len(args) >= 2:
                op = "min" if fn == "__uplim" else "max"
                rep = f"{op}(({args[0]}),({args[1]}))"
            elif fn in ("__uplim", "__dnlim"):
                break
            elif fn == "__if" and len(args) == 3:
                rep = f"(({args[0]}) ? ({args[1]}) : ({args[2]}))"
            elif fn == "__if":
                break
            elif fn == "__table" and len(args) >= 3:
                rep = _table_expr(args[0], args[1:])
            elif fn == "__table":
                break
            elif fn == "__mc" and len(args) >= 2:
                rep = f"aunif(({args[0]}),({args[0]})*({args[1]}))"
            elif fn == "__flat":
                rep = f"aunif(0,({args[0]}))"
            else:
                rep = f"agauss(0,({args[0]}),1)"
            out = out[:i] + rep + out[end + 1:]
    return out


# Parámetros de .model que son solo metadatos en LTspice
MODEL_META = {"mfg", "type", "vceo", "icrating", "iave", "vpk", "ipk", "ppk", "irms", "pn",
              "vbrmax"}
# En los VDMOS de LTspice estos son solo datos de hoja técnica; en otros
# modelos (SW, diodo ideal) Ron es un parámetro eléctrico real.
VDMOS_META = {"vds", "ron", "qg"}
# Opciones exclusivas de LTspice
LT_OPTIONS = {"plotwinsize", "numdgt", "measdgt", "plotreltol", "plotvntol", "plotabstol",
              "baudrate", "topology", "nomarch", "sstol", "sstol", "cshunt", "fastaccess",
              "meascplxfmt", "spice2poly", "ptrantau", "ptranmax", "thev_induc",
              "trtol_digital", "flagloads", "noopiter", "minclock"}


def sanitize_model(line: str) -> str:
    m = re.match(r"^(\s*\.model\s+\S+\s+)([A-Za-z]+)\s*(.*)$", line, re.I | re.S)
    if not m:
        return line
    head, mtype, rest = m.groups()
    body = rest.strip()
    has_paren = body.startswith("(")
    if has_paren:
        body = body[1:]
        if body.rstrip().endswith(")"):
            body = body.rstrip()[:-1]
    toks = re.split(r"(\w+\s*=\s*\{[^}]*\}|\w+\s*=\s*\"[^\"]*\"|\w+\s*=\s*[^\s=]+)", body)
    keep = []
    for t in toks:
        t = t.strip()
        if not t:
            continue
        mm = re.match(r"^(\w+)\s*=", t)
        if mm and mm.group(1).lower() in MODEL_META:
            continue
        if mm and mtype.upper() == "VDMOS" and mm.group(1).lower() in VDMOS_META:
            continue
        if not mm and t.lower() == "noiseless":
            continue
        # Interruptores: ngspice solo conoce estos (LTspice agrega level=2,
        # Vser, Ilimit, epsilon... que ngspice ignoraba y la versión 45 rechaza)
        if mtype.upper() in ("SW", "CSW") and not (mm and mm.group(1).lower() in
                                                     ("vt", "vh", "it", "ih", "ron", "roff")):
            continue
        # Ionización por impacto de los JFET de LTspice: ngspice no la tiene
        # (la ignora) y la versión 45 hasta se cae al leerla.
        if mm and mtype.upper() in ("NJF", "PJF") and mm.group(1).lower() in ("alpha", "vk"):
            continue
        keep.append(t)
    body = _normalize_numbers(" ".join(keep))
    body = re.sub(r"\s*=\s*", "=", body)      # 'Vk= 80' -> 'Vk=80' (ngspice 45 lo exige)
    if mtype.upper() == "LPNP":
        mtype = "PNP"            # PNP lateral de LTspice
    if mtype.upper() == "D":
        keys = {k.lower() for k in re.findall(r"(\w+)\s*=", body)}
        if {"ron", "vfwd", "vrev"} & keys and "roff" not in keys:
            # Diodo ideal de LTspice: Roff = 1/Gmin por omisión
            body = (body + " Roff=1e12").strip()
    return f"{head}{mtype}({body})" if (has_paren or body) else f"{head}{mtype}"


# ------------------------------------------------------------- resultado
@dataclass
class StepSpec:
    param: str
    values: list


@dataclass
class Netlist:
    text: str = ""
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    steps: list = field(default_factory=list)          # [StepSpec]
    analyses: list = field(default_factory=list)       # ".tran ..." etc.
    connectivity: Connectivity | None = None
    current_vectors: dict = field(default_factory=dict)  # "R1" -> "@r1[i]"
    temp_dir: str = ""


class Netlister:
    KEEP_TEMP = 4

    def __init__(self, lib: SymbolLibrary, models: ModelDB):
        self.lib = lib
        self.models = models
        self._temps: list[str] = []
        import atexit
        atexit.register(self.cleanup)

    def cleanup(self, keep: int = 0) -> None:
        """Borra las carpetas temporales con modelos traducidos (deja las
        `keep` más recientes, que pueden estar en uso por una simulación)."""
        import shutil
        while len(self._temps) > keep:
            shutil.rmtree(self._temps.pop(0), ignore_errors=True)

    # ------------------------------------------------------------------
    def build(self, sch: Schematic, title: str = "") -> Netlist:
        nl = Netlist()
        base_dir = os.path.dirname(sch.path) if sch.path else os.getcwd()
        self._near = [base_dir]
        self._nl = nl
        self._includes: list[str] = []     # rutas originales
        self._inc_seen: set = set()
        self._models_needed: dict[str, str] = {}
        self._subckts: list[str] = []
        self._sub_done: set = set()
        self._user_models: set = set()
        self._user_subckts: set = set()
        self._x_params: dict[str, dict] = {}
        self._translated: list[str] = []
        self._src = ""
        self._cur_sub = ""
        self._sub_feats: dict[str, set] = {}
        self._sub_deps: dict[str, set] = {}
        self._top_calls: list[str] = []
        self._lt_only_reported: set = set()
        self._temp = tempfile.mkdtemp(prefix="crossspice_")
        nl.temp_dir = self._temp
        self._temps.append(self._temp)
        self.cleanup(keep=self.KEEP_TEMP)

        directives = self._directive_lines(sch)
        for d in directives:
            m = re.match(r"^\.model\s+(\S+)", d, re.I)
            if m:
                self._user_models.add(m.group(1).lower())
            m = re.match(r"^\.subckt\s+(\S+)", d, re.I)
            if m:
                self._user_subckts.add(m.group(1).lower())

        body = self._circuit_lines(sch, nl, top=True)
        out = [f"* {title or sch.path or 'CrossSpice'}"]
        if not any(re.match(r"^\.param.*\bpi\s*=", d, re.I) for d in directives):
            out.append(".param pi=3.141592653589793")
        out += body
        out += self._subckts
        out += list(self._models_needed.values())
        for d in directives:
            t = self._translate_directive(d, nl)
            if t:
                out.append(t)
        for inc in self._includes:
            out.append(f'.include "{inc}"')
        self._patch_subckt_params()
        self._check_features()
        out = self._expand_coupling(out)
        cur = [v for v in nl.current_vectors.values() if v and v.startswith("@")]
        out.append(".save all")
        for i in range(0, len(cur), 16):
            out.append(".save " + " ".join(cur[i:i + 16]))
        out.append(".end")
        nl.text = "\n".join(out) + "\n"
        if not nl.analyses:
            nl.warnings.append("No hay ningún comando de simulación (.tran, .ac, .dc, .op...).")
        return nl

    # ------------------------------------------------------------------
    def _directive_lines(self, sch: Schematic) -> list[str]:
        lines: list[str] = []
        for t in sch.directives:
            for raw in t.body.split("\n"):
                s = _strip_comment(raw).strip()
                if not s:
                    continue
                if s.startswith("+") and lines:
                    lines[-1] += " " + s[1:]
                else:
                    lines.append(s)
        return lines

    def _circuit_lines(self, sch: Schematic, nl: Netlist, top: bool,
                       prefix_warn: str = "") -> list[str]:
        con = build_connectivity(sch, self.lib, self._near)
        if top:
            nl.connectivity = con
        nl.warnings += [prefix_warn + w for w in con.warnings]
        lines: list[str] = []
        for i, c in enumerate(sch.components):
            sym = self.lib.get(c.symbol, self._near)
            if sym is None:
                nl.errors.append(f"{prefix_warn}No se encontró el símbolo '{c.symbol}'.")
                continue
            if not sym.pins:
                continue           # símbolos decorativos
            nets = con.pin_nets.get(i, [])
            try:
                lines += self._element(c, sym, nets, nl, top, prefix_warn)
            except Exception as exc:  # noqa: BLE001 — no detener todo el netlist
                nl.errors.append(f"{prefix_warn}{c.name}: {exc}")
        return lines

    # ------------------------------------------------------------------
    def _element(self, c, sym, nets, nl: Netlist, top: bool, pw: str) -> list[str]:
        attrs = dict(sym.attrs)
        for k, v in c.attrs.items():
            attrs[k] = v
        prefix = attrs.get("Prefix", "") or ""
        inst = c.attrs.get("InstName") or sym.attrs.get("InstName") or "U?"
        order = sorted(range(len(sym.pins)), key=lambda k: (sym.pins[k].order, k))
        nodes = [spice_node(nets[k]) if k < len(nets) else "0" for k in order]

        # Bloque jerárquico: subcircuito a partir de otro .asc
        if sym.symtype == "BLOCK" and not prefix:
            sub = self._block_subckt(c, sym, nl, pw)
            name = inst if inst.upper().startswith("X") else "X" + inst
            return [f"{name} {' '.join(nodes)} {sub}"] if sub else []
        if not prefix:
            nl.warnings.append(f"{pw}{inst}: el símbolo no tiene Prefix; se omite.")
            return []
        letter = prefix[0].upper()
        name = inst if inst[:1].upper() == letter else letter + inst
        value = _normalize_numbers(_unquote_empty(attrs.get("Value", "")))
        value2 = _normalize_numbers(_unquote_empty(attrs.get("Value2", "")))
        sl = _normalize_numbers(" ".join(x for x in (attrs.get("SpiceLine", ""),
                                                       attrs.get("SpiceLine2", "")) if x))
        spice_model = attrs.get("SpiceModel", "") or ""
        model_file = attrs.get("ModelFile", "") or ""
        if top:
            nl.current_vectors[inst.upper()] = _current_vector(letter, name)

        for f in (model_file,):
            if f:
                self._need_file(f, nl, pw + inst)

        if letter == "X":
            # Reglas de LTspice: si SpiceModel es un archivo se incluye y el
            # nombre del subcircuito es Value2 (o Value); si no es un archivo,
            # SpiceModel es el propio nombre del subcircuito.
            sm_file = self.models.find_lib_file(spice_model, self._near) if spice_model \
                else None
            if sm_file:
                self._include(sm_file, nl, pw + inst)
                call = value2 or value
            elif spice_model:
                call = " ".join(x for x in (spice_model, value2) if x)
                if not model_file:
                    self._need_subckt(spice_model, nl, pw + inst)
            else:
                call = " ".join(x for x in (value, value2) if x)
                if call and not model_file:
                    self._need_subckt(call.split()[0], nl, pw + inst)
            call = " ".join(x for x in (call, sl) if x)
            if not call:
                nl.errors.append(f"{pw}{inst}: falta el nombre del subcircuito (Value).")
                return []
            self._note_x_params(call)
            return [f"{name} {' '.join(nodes)} {call}"]

        if letter == "A":
            # Terminales sueltas van al común (8); el común suelto va a tierra
            nn = list(nodes) + ["0"] * (8 - len(nodes))
            com = nn[7] if not nn[7].startswith("NC_") else "0"
            nn = [com if x.startswith("NC_") else x for x in nn[:7]] + [com]
            mdl = spice_model or value
            line = f"{name} {' '.join(nn)} {mdl} {' '.join(x for x in (value2, sl) if x)}"
            lines, kind = adevices.translate(_normalize_numbers(line))
            if lines is None:
                nl.errors.append(f"{pw}{inst}: el dispositivo especial '{kind}' de LTspice "
                                 "no existe en ngspice.")
                return []
            return lines

        if letter == "J" and len(nodes) == 2 and not value:
            # Puente (Misc\jumper): une dos redes con nombres distintos
            return [f"V{name}_jmp {nodes[0]} {nodes[1]} 0"]

        full = " ".join(x for x in (value, value2, sl) if x).strip()
        if letter in "EG" and re.search(r"\blaplace\s*=", full, re.I):
            nl.errors.append(f"{pw}{inst}: las fuentes con 'Laplace=' son exclusivas de "
                             "LTspice.")
            return []
        if letter in "RCL":
            return self._passive(letter, name, nodes, full)
        if letter in "VI":
            return self._source(letter, name, nodes, full)
        if letter in "DQJMZ":
            return self._semiconductor(letter, prefix, name, nodes, value, value2, sl,
                                       spice_model, nl, pw + inst)
        if letter in "SW":
            mdl = value.split()[0] if value else ""
            if mdl:
                self._need_model(mdl, nl, pw + inst, letter)
        if letter == "B":
            full = _translate_b(full)
        return [f"{name} {' '.join(nodes)} {full}".rstrip()]

    # ------------------------------------------------------------------
    def _passive(self, letter, name, nodes, text) -> list[str]:
        noiseless = bool(re.search(r"\bnoiseless\b", text, re.I))
        text = re.sub(r"\bnoiseless\b", "", text, flags=re.I)
        val, kv = split_kv(text)
        if noiseless and letter == "R":
            kv["noisy"] = "0"
        a, b = nodes[0], nodes[1]
        low = {k.lower(): (k, v) for k, v in kv.items()}
        allowed = {"R": {"tc1", "tc2", "tc", "temp", "dtemp", "m", "noisy", "ac", "r"},
                   "C": {"ic", "m", "temp", "dtemp", "tc1", "tc2", "c"},
                   "L": {"ic", "m", "temp", "dtemp", "tc1", "tc2", "nt", "l"}}[letter]
        keep = [f"{k}={v}" for kl, (k, v) in low.items() if kl in allowed]
        if letter == "L" and {"hc", "bs", "br"} & set(low):
            self._nl.warnings.append(f"{name}: el núcleo magnético no lineal (Hc, Bs, Br) "
                                     "es exclusivo de LTspice; ngspice lo simula como "
                                     "inductor lineal.")
        rser = low.get("rser", (None, None))[1]
        lser = low.get("lser", (None, None))[1]
        rpar = low.get("rpar", (None, None))[1]
        cpar = low.get("cpar", (None, None))[1]
        if letter == "L" and rser is None:
            rser = "1m"       # LTspice añade 1 mΩ en serie por omisión
        if letter == "R":
            rser = lser = None
        if not val and letter.lower() in low:
            val = ""
        out = []
        n1 = a
        chain = []
        if rser not in (None, "0") and parse_value(rser) != 0:
            chain.append(("R", f"R{name}_ser", rser))
        if lser not in (None, "0") and parse_value(lser) != 0:
            chain.append(("L", f"L{name}_ser", lser))
        k = 0
        for kind, ename, v in chain:
            k += 1
            n2 = f"{name}_n{k}"
            out.append(f"{ename} {n1} {n2} {v}")
            n1 = n2
        out.insert(0, f"{name} {n1} {b} {val} {' '.join(keep)}".rstrip())
        if rpar not in (None, "0") and parse_value(rpar) != 0:
            out.append(f"R{name}_par {a} {b} {rpar}")
        if cpar not in (None, "0") and parse_value(cpar) != 0:
            out.append(f"C{name}_par {a} {b} {cpar}")
        return out

    def _source(self, letter, name, nodes, text) -> list[str]:
        val, kv = split_kv(text)
        low = {k.lower(): v for k, v in kv.items()}
        val = _translate_source_value(val, self._near)
        out = []
        a, b = nodes[0], nodes[1]
        rser = low.get("rser")
        cpar = low.get("cpar")
        keep = [f"{k}={v}" for k, v in kv.items() if k.lower() in ("m", "dtemp", "temp")]
        if letter == "V" and rser and parse_value(rser) not in (0, None):
            mid = f"{name}_rs"
            out.append(f"{name} {mid} {b} {val} {' '.join(keep)}".rstrip())
            out.append(f"R{name}_ser {a} {mid} {rser}")
        else:
            out.append(f"{name} {a} {b} {val} {' '.join(keep)}".rstrip())
        if cpar and parse_value(cpar) not in (0, None):
            out.append(f"C{name}_par {a} {b} {cpar}")
        return out

    def _semiconductor(self, letter, prefix, name, nodes, value, value2, sl,
                       spice_model, nl, who) -> list[str]:
        mdl_txt = value or spice_model
        parts = mdl_txt.split()
        model = parts[0] if parts else ""
        rest = " ".join(parts[1:] + ([value2] if value2 else []) + ([sl] if sl else []))
        kind = prefix[1:].upper() if len(prefix) > 1 else ""
        entry = self.models.find_model(model) if model else None
        if letter == "Z" and ("IGBT" in model.upper() or
                              (entry and "IGBT" in entry.mtype)):
            self._lt_only("transistores IGBT", who)
            return []
        if model:
            model = self._need_model(model, nl, who, letter, kind, len(nodes))
        return [f"{name} {' '.join(nodes)} {model} {rest}".rstrip()]

    # ------------------------------------------------------------------
    def _need_model(self, model: str, nl, who, letter="", kind="", npins=0) -> str:
        key = model.lower()
        if key in self._user_models or key in self._models_needed:
            return model
        e = self.models.find_model(model)
        if e is not None:
            self._models_needed[key] = sanitize_model(e.text)
            return model
        up = model.upper()
        if up in DEFAULT_MODELS or (letter == "D" and up == "D"):
            mtype = DEFAULT_MODELS.get(up, "D")
            if letter == "M" and npins == 3:
                alias = f"{up}_VD"
                pch = " (pchan)" if up == "PMOS" else ""
                self._models_needed[alias.lower()] = f".model {alias} VDMOS{pch}"
                return alias
            self._models_needed[key] = f".model {model} {mtype}"
            return model
        # Puede venir de un .lib/.include del usuario: no avisar todavía
        if not self._includes and not self._user_subckts:
            nl.warnings.append(f"{who}: no encontré el modelo '{model}' en la base de datos.")
        return model

    def _need_subckt(self, name: str, nl, who) -> None:
        key = name.lower()
        if key in self._user_subckts:
            return
        p = self.models.subckt_index().get(key)
        if p:
            self._need_file(p, nl, who)

    def _need_file(self, fname: str, nl, who, quiet_missing=False) -> None:
        path = self.models.find_lib_file(fname, self._near)
        if not path:
            if not quiet_missing:
                nl.warnings.append(f"{who}: no se encontró el archivo de modelos '{fname}'.")
            else:
                # SpiceModel a veces es el nombre del subcircuito, no un archivo
                self._need_subckt(os.path.splitext(fname)[0], nl, who)
            return
        self._include(path, nl, who)

    def _include(self, path: str, nl, who) -> str | None:
        """Incluye (una sola vez) un archivo de modelos ya traducido."""
        norm = os.path.normcase(os.path.abspath(path))
        if norm in self._inc_seen:
            return None
        self._inc_seen.add(norm)
        if self.models.is_encrypted(path):
            nl.errors.append(f"{who}: el modelo '{os.path.basename(path)}' está cifrado "
                             "por Analog Devices; solo LTspice puede simularlo.")
            return None
        out = self._translate_file(path, nl)
        if out:
            self._includes.append(out)
        return out

    def _translated_path(self, path: str) -> str:
        return os.path.join(self._temp, f"{abs(hash(os.path.normcase(path))):x}_"
                            + os.path.basename(path))

    def _translate_file(self, path: str, nl) -> str | None:
        try:
            text, _ = read_text(path)
        except OSError as exc:
            nl.errors.append(f"No se pudo leer {path}: {exc}")
            return None
        old_near, old_src = self._near, self._src
        self._near = [os.path.dirname(path)] + old_near
        self._src = os.path.basename(path)
        logical: list[str] = []
        for ln in text.split("\n"):
            s = ln.rstrip()
            if s.lstrip().startswith("+") and logical:
                logical[-1] += " " + s.lstrip()[1:]
            else:
                logical.append(s)
        self._lt_switches = lt_switch_models(logical)
        conv = []
        for ln in logical:
            low = ln.strip().lower()
            if low.startswith(".subckt"):
                parts = ln.split()
                self._cur_sub = parts[1].lower() if len(parts) > 1 else ""
            conv.append(self._translate_lib_line(ln, nl))
            if low.startswith(".ends"):
                self._cur_sub = ""
        self._cur_sub = ""
        if self._lt_switches:
            # I(S1) de un interruptor emulado -> corriente de su fuente de 0 V
            names = {ln.split()[0] for ln in logical
                     if ln.strip()[:1].lower() == "s" and len(ln.split()) > 5 and
                     ln.split()[5].lower() in self._lt_switches}
            for nm in names:
                rx = re.compile(r"\bI\(\s*" + re.escape(nm) + r"\s*\)", re.I)
                conv = [rx.sub(f"I(V_{nm})", c) for c in conv]
        out = self._expand_coupling(conv)
        self._near, self._src = old_near, old_src
        dest = self._translated_path(path)
        with open(dest, "w", encoding="utf-8", errors="replace") as fh:
            fh.write("\n".join(out) + "\n")
        self._translated.append(dest)
        return dest

    def _translate_lib_line(self, ln: str, nl) -> str:
        s = ln.strip()
        if not s or s[0] in "*;":
            return ln
        low = s.lower()
        if low.startswith(".model"):
            if re.match(r"^\.model\s+\S+\s+[np]igbt\b", low):
                self._lt_only("transistores IGBT", self._src or "esquemático")
            return sanitize_model(s)
        if low.startswith(".param"):
            return fix_param_line(_normalize_numbers(_strip_comment(s)))
        if low.startswith((".lib", ".inc")):
            return self._translate_include(s, nl) or "* " + s
        if low.startswith((".backanno", ".machine", ".endmachine", ".wave", ".state",
                           ".rule", ".output")):
            return "* " + s
        if low.startswith("."):
            return _normalize_numbers(s)
        c0 = low[0]
        if c0 == "s" and getattr(self, "_lt_switches", None):
            t = translate_lt_switch(s, self._lt_switches)
            if t:
                return t
        if c0 == "a":
            lines, kind = adevices.translate(_normalize_numbers(s))
            if lines is None:
                self._feature(f"el dispositivo especial '{kind}' de LTspice")
                return "* " + s
            return "\n".join(lines)
        if c0 in "eg" and re.search(r"\blaplace\s*=", low):
            self._feature("fuentes con Laplace=")
            return "* " + s
        if c0 in "rcl" and "noiseless" in low:
            s = re.sub(r"\bnoiseless\b", "noisy=0" if c0 == "r" else "", s, flags=re.I)
        if c0 in "eg":
            t = _translate_table(s)
            if t:
                return t
        if c0 in "cl":
            toks = s.split(None, 3)
            if len(toks) >= 4 and re.search(r"\b(rser|lser|rpar|cpar)\s*=", toks[3], re.I):
                return "\n".join(self._passive(toks[0][0].upper(), toks[0],
                                               toks[1:3], _normalize_numbers(toks[3])))
        if c0 in "vi":
            toks = s.split(None, 3)
            if len(toks) >= 4:
                return "\n".join(self._source(toks[0][0].upper(), toks[0], toks[1:3],
                                              _normalize_numbers(toks[3])))
        if c0 == "b":
            return _translate_b(_normalize_numbers(s))
        if c0 == "x":
            self._note_x_params(" ".join(s.split()[1:]), from_sub=True)
        return _normalize_numbers(s)

    def _translate_include(self, s: str, nl) -> str | None:
        toks = s.split(None, 2)
        kw = toks[0].lower()
        if len(toks) < 2:
            return None
        fname = toks[1].strip('"')
        path = self.models.find_lib_file(fname, self._near)
        if not path:
            nl.warnings.append(f"No se encontró '{fname}' (de {toks[0]}).")
            return None
        if self.models.is_encrypted(path):
            nl.errors.append(f"'{os.path.basename(path)}' está cifrado por Analog Devices; "
                             "solo LTspice puede simularlo.")
            return None
        norm = os.path.normcase(os.path.abspath(path))
        if norm in self._inc_seen:
            return None                      # ya incluido
        self._inc_seen.add(norm)
        dest = self._translate_file(path, nl)
        if not dest:
            return None
        if kw.startswith(".lib") and len(toks) > 2:
            return f'.lib "{dest}" {toks[2]}'
        return f'.include "{dest}"'

    # ------------------------------------------------------------------
    def _feature(self, what: str) -> None:
        """Anota algo exclusivo de LTspice encontrado en una librería; solo es
        un error si el subcircuito que lo contiene se usa de verdad."""
        if self._cur_sub:
            self._sub_feats.setdefault(self._cur_sub, set()).add(what)
        else:
            self._lt_only(what, self._src or "esquemático")

    def _check_features(self) -> None:
        seen = set()
        stack = [c.lower() for c in self._top_calls]
        while stack:
            sname = stack.pop()
            if sname in seen:
                continue
            seen.add(sname)
            for what in sorted(self._sub_feats.get(sname, ())):
                self._nl.errors.append(f"El subcircuito '{sname}' usa {what}, que ngspice "
                                       "no tiene.")
            stack += list(self._sub_deps.get(sname, ()))

    def _note_x_params(self, call: str, from_sub: bool = False) -> None:
        """Recuerda los parámetros pasados a cada subcircuito: LTspice no exige
        declararlos en el .subckt, ngspice sí."""
        toks = call.split()
        if not toks:
            return
        if from_sub:
            # línea X dentro de una librería: nodos... nombre [parámetros]
            plain = [t for t in toks if "=" not in t and t.lower() != "params:"]
            if plain:
                target = plain[-1]
                if self._cur_sub:
                    self._sub_deps.setdefault(self._cur_sub, set()).add(target.lower())
                toks = toks[toks.index(target):]
        else:
            self._top_calls.append(toks[0])
        _rest, kv = split_kv(" ".join(toks[1:]))
        if kv:
            d = self._x_params.setdefault(toks[0].lower(), {})
            for k, v in kv.items():
                d.setdefault(k.lower(), v)

    def _fix_subckt_line(self, line: str) -> str:
        m = re.match(r"^(\s*\.subckt\s+)(\S+)(.*)$", line, re.I)
        if not m:
            return line
        head, name, rest = m.groups()
        rest = re.sub(r"\bparams:\s*", " ", rest, flags=re.I)
        nodes_part, kv = split_kv(rest)
        for k, v in self._x_params.get(name.lower(), {}).items():
            if k not in {x.lower() for x in kv}:
                kv[k] = v
        if not kv:
            return line
        params = " ".join(f"{k}={v}" for k, v in kv.items())
        return f"{head}{name} {nodes_part} params: {params}"

    def _patch_subckt_params(self) -> None:
        for path in list(self._translated):
            try:
                with open(path, encoding="utf-8") as fh:
                    lines = fh.read().split("\n")
            except OSError:
                continue
            new = self._fix_subckt_blocks(lines)
            if new != lines:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(new))
        self._subckts = self._fix_subckt_blocks(self._subckts)

    def _fix_subckt_blocks(self, lines: list[str]) -> list[str]:
        """En LTspice los parámetros de la instancia ganan sobre los .param
        locales del subcircuito; en ngspice es al revés.  Se mueven esos .param
        locales a los valores por omisión de 'params:'."""
        out: list[str] = []
        i = 0
        while i < len(lines):
            ln = lines[i]
            if not ln.lstrip().lower().startswith(".subckt"):
                out.append(ln)
                i += 1
                continue
            j = i + 1
            depth = 1
            while j < len(lines) and depth:
                lw = lines[j].lstrip().lower()
                if lw.startswith(".subckt"):
                    depth += 1
                elif lw.startswith(".ends"):
                    depth -= 1
                j += 1
            body = lines[i + 1:j]
            head = self._fix_subckt_line(ln)
            m = re.search(r"\bparams:\s*(.*)$", head, re.I)
            declared = {}
            if m:
                declared = {k.lower(): v for k, v in
                            re.findall(r"(\w+)\s*=\s*(\{[^}]*\}|\S+)", m.group(1))}
            new_body = []
            depth = 0
            for b in body:
                lw = b.lstrip().lower()
                if lw.startswith(".subckt"):
                    depth += 1
                elif lw.startswith(".ends"):
                    depth -= 1
                if depth == 0 and lw.startswith(".param") and declared:
                    pairs = re.findall(r"(\w+)\s*=\s*(\{[^}]*\}|[^\s{]+)", b.strip()[6:])
                    keep = []
                    for k, v in pairs:
                        if k.lower() in declared:
                            if k.lower() not in self._x_params.get(
                                    head.split()[1].lower(), {}):
                                declared[k.lower()] = v
                        else:
                            keep.append(f"{k}={v}")
                    if keep:
                        new_body.append(".param " + " ".join(keep))
                    continue
                new_body.append(b)
            if declared:
                pre = re.sub(r"\s*\bparams:.*$", "", head, flags=re.I)
                head = pre + " params: " + " ".join(f"{k}={v}" for k, v in declared.items())
            out.append(head)
            out += self._fix_subckt_blocks(new_body[:-1]) + new_body[-1:] if new_body \
                else []
            i = j
        return out

    @staticmethod
    def _expand_coupling(lines: list[str]) -> list[str]:
        """K1 L1 L2 L3 1  ->  K1_1 L1 L2 1 / K1_2 L1 L3 1 / K1_3 L2 L3 1"""
        out = []
        for ln in lines:
            toks = ln.split()
            if len(toks) > 4 and toks[0][:1].upper() == "K" and \
                    all(t[:1].upper() == "L" for t in toks[1:-1]):
                inds, k = toks[1:-1], toks[-1]
                n = 0
                for i in range(len(inds)):
                    for j in range(i + 1, len(inds)):
                        n += 1
                        out.append(f"{toks[0]}_{n} {inds[i]} {inds[j]} {k}")
            else:
                out.append(ln)
        return out

    def _lt_only(self, what: str, src: str) -> None:
        key = (what, src)
        if key not in self._lt_only_reported:
            self._lt_only_reported.add(key)
            self._nl.errors.append(f"'{src}' usa {what}, exclusivo de LTspice.")

    # ------------------------------------------------------------------
    def _block_subckt(self, c, sym, nl, pw) -> str | None:
        base = os.path.splitext(os.path.basename(sym.path))[0]
        asc = None
        for d in [os.path.dirname(sym.path)] + self._near:
            cand = os.path.join(d, base + ".asc")
            if os.path.isfile(cand):
                asc = cand
                break
        if not asc:
            nl.errors.append(f"{pw}{c.name}: no encontré la hoja '{base}.asc' del bloque.")
            return None
        key = base.lower()
        if key in self._sub_done:
            return base
        self._sub_done.add(key)
        if len(self._sub_done) > 50:
            nl.errors.append("Jerarquía demasiado profunda.")
            return None
        sub = load_asc(asc)
        ports = [spice_node(p.name) for p in sorted(sym.pins, key=lambda p: p.order)]
        old = self._near
        self._near = [os.path.dirname(asc)] + old
        body = self._circuit_lines(sub, nl, top=False, prefix_warn=f"[{base}] ")
        sub_dirs = self._directive_lines(sub)
        self._near = old
        lines = [f".subckt {base} {' '.join(ports)}"] + body
        for d in sub_dirs:
            t = self._translate_directive(d, nl)
            if t and not re.match(r"^\.(tran|ac|dc|op|noise|tf|four|step)\b", t, re.I):
                lines.append(t)
        lines.append(f".ends {base}")
        self._subckts += lines
        return base

    # ------------------------------------------------------------------
    def _translate_directive(self, d: str, nl: Netlist) -> str | None:
        s = _normalize_numbers(d.strip())
        low = s.lower()
        if not low.startswith("."):
            # Línea de elemento escrita a mano como directiva
            return self._translate_lib_line(s, nl)
        kw = low.split()[0]
        if kw in (".tran", ".ac", ".dc", ".op", ".noise", ".tf"):
            s = _translate_analysis(s)
            nl.analyses.append(s)
            return s
        if kw in (".param", ".params"):
            return fix_param_line(s)
        if kw == ".step":
            spec = _parse_step(s)
            if spec:
                nl.steps.append(spec)
            else:
                nl.warnings.append(f"No se entiende '{s}' (solo .step param ...).")
            return None
        if kw in (".lib", ".include", ".inc"):
            return self._translate_include(s, nl)
        if kw == ".model":
            return sanitize_model(s)
        if kw == ".options" or kw == ".option":
            toks = s.split()[1:]
            keep = [t for t in toks if t.split("=")[0].lower() not in LT_OPTIONS]
            return ".options " + " ".join(keep) if keep else None
        if kw in (".backanno", ".wave", ".savebias", ".loadbias", ".net", ".save",
                  ".machine", ".endmachine", ".ferret", ".text"):
            if kw not in (".backanno", ".save"):
                nl.warnings.append(f"'{kw}' es exclusivo de LTspice y se ignora.")
            return None
        if kw == ".end":
            return None
        return s


# ----------------------------------------------------------------- helpers
def _strip_comment(line: str) -> str:
    """Quita comentarios ';' de LTspice (fuera de llaves y comillas)."""
    depth = 0
    quote = False
    for i, ch in enumerate(line):
        if ch == '"':
            quote = not quote
        elif not quote and ch == "{":
            depth += 1
        elif not quote and ch == "}":
            depth -= 1
        elif ch == ";" and not quote and depth <= 0:
            return line[:i]
    return line


def _current_vector(letter: str, name: str) -> str:
    n = name.lower()
    if letter in "VEH":
        return f"{n}#branch"
    if letter == "L":
        return f"@{n}[i]"
    if letter in "RCB":
        return f"@{n}[i]"
    if letter == "I":
        return f"@{n}[current]"
    if letter == "D":
        return f"@{n}[id]"
    if letter == "Q":
        return f"@{n}[ic]"
    if letter in "MJ":
        return f"@{n}[id]"
    return ""


def _translate_source_value(val: str, near: list[str]) -> str:
    v = re.sub(r"\bSINE\s*\(", "SIN(", val, flags=re.I)
    # SIN de LTspice admite Ncycles (7º parámetro); ngspice no.
    def fix_sin(m):
        args = m.group(1).split()
        return "SIN(" + " ".join(args[:6]) + ")"
    v = re.sub(r"\bSIN\(([^)]*)\)", fix_sin, v, flags=re.I)
    # PWL desde archivo: se incrusta el contenido
    m = re.search(r"\bPWL\s+file\s*=\s*\"?([^\"\s]+)\"?", v, re.I)
    if m:
        fname = m.group(1)
        pts = []
        for d in near:
            p = os.path.join(d, fname)
            if os.path.isfile(p):
                for line in read_text(p)[0].split("\n"):
                    t = line.replace(",", " ").split()
                    if len(t) >= 2:
                        pts += t[:2]
                break
        v = v[:m.start()] + "PWL(" + " ".join(pts) + ")" + v[m.end():]
    v = re.sub(r"\b(tripdv|tripdt)\s*=\s*\S+", "", v, flags=re.I)
    v = re.sub(r"\bload\b", "", v, flags=re.I)
    return v.strip()


def _translate_table(line: str) -> str | None:
    """E/G de LTspice con TABLE(x1 y1 x2 y2...) -> fuente B con pwl()."""
    m = re.match(r"^([EGeg]\S*)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+table\s*\((.*)\)\s*$",
                 line.strip(), re.I)
    if not m:
        return None
    name, a, b, cp, cn, pts = m.groups()
    vals = [v for v in re.split(r"[\s,]+", pts.strip()) if v]
    ctrl = f"V({cp})" if cn == "0" else f"V({cp},{cn})"
    q = "V" if name[0] in "Ee" else "I"
    return f"B{name} {a} {b} {q}=pwl({ctrl},{','.join(vals)})"


def _unquote_empty(v) -> str:
    v = (v or "").strip()
    return "" if v in ('""', "''") else v


def _translate_b(line: str) -> str:
    # Funciones de LTspice con otro nombre en ngspice
    return re.sub(r"\buramp\s*\(", "u2(", line, flags=re.I)


def _translate_analysis(s: str) -> str:
    toks = s.split()
    kw = toks[0].lower()
    if kw == ".tran":
        flags = [t for t in toks[1:] if not _isnum(t)]
        nums = [t for t in toks[1:] if _isnum(t)]
        keep_flags = [f for f in flags if f.lower() == "uic"]
        # 'startup' de LTspice enciende las fuentes desde cero (lo que hace
        # arrancar a los osciladores); lo más parecido en ngspice es 'uic'.
        if any(f.lower() == "startup" for f in flags) and not keep_flags:
            keep_flags = ["uic"]
        if len(nums) == 1:
            stop = parse_value(nums[0]) or 1e-3
            nums = [spice_num(stop / 1000), nums[0]]
        elif len(nums) >= 2 and (parse_value(nums[0]) or 0) == 0:
            stop = parse_value(nums[1]) or 1e-3
            nums[0] = spice_num(stop / 1000)
        return " ".join([".tran"] + nums + keep_flags)
    if kw in (".ac", ".noise"):
        low = [t.lower() for t in toks]
        if "list" in low:
            i = low.index("list")
            freqs = [parse_value(t) for t in toks[i + 1:]]
            freqs = [f for f in freqs if f]
            if freqs:
                # ngspice no tiene 'list': barrido que cubre las frecuencias
                if len(freqs) == 1:
                    sweep = ["lin", "1", toks[i + 1], toks[i + 1]]
                else:
                    sweep = ["lin", str(len(freqs)), spice_num(min(freqs)),
                             spice_num(max(freqs))]
                return " ".join(toks[:i] + sweep)
    return s


def _isnum(t: str) -> bool:
    return parse_value(t) is not None or t.startswith("{")


def _parse_step(s: str) -> StepSpec | None:
    toks = s.split()[1:]
    mode = "lin"
    if toks and toks[0].lower() in ("lin", "dec", "oct"):
        mode = toks.pop(0).lower()
    if not toks or toks[0].lower() != "param" or len(toks) < 3:
        return None
    name = toks[1]
    rest = toks[2:]
    if rest[0].lower() == "list":
        vals = [parse_value(v) for v in rest[1:]]
        return StepSpec(name, [v for v in vals if v is not None])
    if len(rest) < 3:
        return None
    a, b, c = (parse_value(x) for x in rest[:3])
    if None in (a, b, c) or c == 0:
        return None
    vals = []
    if mode == "lin":
        n = int(round((b - a) / c))
        vals = [a + i * c for i in range(n + 1)]
    else:
        import math
        if a <= 0 or b <= 0:
            return None
        per = c
        ratio = 10 if mode == "dec" else 2
        n = int(round(math.log(b / a, ratio) * per))
        vals = [a * ratio ** (i / per) for i in range(n + 1)]
    return StepSpec(name, vals)


# ------------------------------------------------- sin code models (XSPICE)
_IDEAL_KEYS = {"ron", "roff", "vfwd", "vrev", "rrev", "epsilon", "revepsilon", "ilimit",
               "revilimit"}


def replace_ideal_diodes(text: str) -> str:
    """Para ngspice sin XSPICE (p. ej. la versión WebAssembly): el diodo ideal
    de LTspice (Ron/Roff/Vfwd/Vrev) se reemplaza por una fuente B lineal a
    tramos.  Con XSPICE, ngspice lo convierte solo en el modelo 'sidiode'."""
    models: dict[str, dict] = {}
    for m in re.finditer(r"^\s*\.model\s+(\S+)\s+D\s*\((.*?)\)\s*$", text, re.I | re.M):
        kv = {k.lower(): v for k, v in
              re.findall(r"(\w+)\s*=\s*(\{[^}]*\}|[^\s()]+)", m.group(2))}
        if _IDEAL_KEYS & set(kv):
            models[m.group(1).lower()] = kv
    if not models:
        return text

    def val(kv, k, default):
        v = kv.get(k)
        if v is None:
            return default
        return v if v.startswith("{") else f"({v})"

    out = []
    for line in text.split("\n"):
        m = re.match(r"^(D\S*)\s+(\S+)\s+(\S+)\s+(\S+)(.*)$", line.strip(), re.I)
        if m and m.group(4).lower() in models:
            kv = models[m.group(4).lower()]
            ron = val(kv, "ron", "(1)")
            roff = val(kv, "roff", "(1e12)")
            vf = val(kv, "vfwd", "(0)")
            v = f"V({m.group(2)},{m.group(3)})"
            rev = f"{v}/{roff}"
            if "vrev" in kv:
                vr = val(kv, "vrev", "(1e30)")
                rr = val(kv, "rrev", ron)
                rev = f"({v}<-{vr} ? ({v}+{vr})/{rr}-{vr}/{roff} : {v}/{roff})"
            out.append(f"B{m.group(1)} {m.group(2)} {m.group(3)} "
                       f"I=({v}>{vf} ? ({v}-{vf})/{ron}+{vf}/{roff} : {rev})")
            continue
        out.append(line)
    return "\n".join(out)



# ----------------------------------------- interruptor "nivel 2" de LTspice
def lt_switch_models(lines: list[str]) -> dict:
    """Modelos SW con extensiones de LTspice (level=2, Ilimit, Vser o
    histéresis negativa = transición suave) que ngspice no reproduce."""
    out = {}
    for ln in lines:
        m = re.match(r"^\s*\.model\s+(\S+)\s+SW\s*\((.*)\)\s*$", ln, re.I)
        if not m:
            continue
        kv = {k.lower(): v for k, v in
              re.findall(r"(\w+)\s*=\s*(\{[^}]*\}|[^\s()]+)", m.group(2))}
        vh = parse_value(kv.get("vh", "0"))
        if "ilimit" in kv or "vser" in kv or kv.get("level") == "2" or \
                (vh is not None and vh < 0):
            out[m.group(1).lower()] = kv
    return out


def translate_lt_switch(line: str, models: dict) -> str | None:
    """S n+ n- nc+ nc- modelo -> fuente B con conductancia que pasa suavemente
    de 1/Roff a 1/Ron alrededor de Vt (ancho |Vh|), con límite de corriente."""
    toks = line.split()
    if len(toks) < 6 or toks[5].lower() not in models:
        return None
    name, a, b, cp, cn = toks[:5]
    kv = models[toks[5].lower()]
    base = re.sub(r"\W", "_", name).lower()
    params = {"ron": kv.get("ron", "1"), "roff": kv.get("roff", "1e12"),
              "vt": kv.get("vt", "0"), "vh": kv.get("vh", "0")}
    lines = []
    plist = []
    for k, v in params.items():
        plist.append(f"cs_{base}_{k}={v if v.startswith('{') else '{' + v + '}'}")
    w = f"max(abs({{cs_{base}_vh}}),1e-3)"
    vc = f"V({cp})" if cn == "0" else f"V({cp},{cn})"
    vab = f"V({a},{b})"
    g = (f"(1/{{cs_{base}_roff}}+(1/{{cs_{base}_ron}}-1/{{cs_{base}_roff}})*"
         f"0.5*(1+tanh(2*({vc}-{{cs_{base}_vt}})/{w})))")
    cur = f"{g}*{vab}"
    if "ilimit" in kv:
        plist.append(f"cs_{base}_il={kv['ilimit'] if kv['ilimit'].startswith('{') else '{' + kv['ilimit'] + '}'}")
        cur = f"{{cs_{base}_il}}*tanh({cur}/{{cs_{base}_il}})"
    lines.append(".param " + " ".join(plist))
    # 0 V en serie: permite medir I(S1) como hace LTspice (-> I(V_S1))
    mid = f"cs_{base}_m"
    lines.append(f"V_{name} {a} {mid} 0")
    lines.append(f"B{name} {mid} {b} I={cur}")
    return "\n".join(lines)
