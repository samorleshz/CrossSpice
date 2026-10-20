"""Modelo de datos del esquemático y lectura/escritura del formato .asc."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

from .symbols import Window
from .textio import read_text, write_text


@dataclass
class Wire:
    x1: int
    y1: int
    x2: int
    y2: int


@dataclass
class Flag:
    x: int
    y: int
    name: str
    iopin: str | None = None      # In / Out / BiDir


@dataclass
class Component:
    symbol: str                   # tal como aparece en el .asc (p. ej. OpAmps\LT1001)
    x: int
    y: int
    orient: str = "R0"
    attrs: dict = field(default_factory=dict)      # InstName, Value, ...
    windows: dict = field(default_factory=dict)    # num -> Window (o just="Invisible")

    @property
    def name(self) -> str:
        return self.attrs.get("InstName", "")


@dataclass
class Text:
    x: int
    y: int
    just: str
    size: int
    text: str                     # incluye el "!" (directiva) o ";" (comentario)

    @property
    def is_directive(self) -> bool:
        return self.text.startswith("!")

    @property
    def body(self) -> str:
        t = self.text[1:] if self.text[:1] in "!;" else self.text
        return t.replace("\\n", "\n")


@dataclass
class Graphic:
    kind: str                     # LINE, RECTANGLE, CIRCLE, ARC
    coords: list
    style: int = 0


@dataclass
class Schematic:
    wires: list = field(default_factory=list)
    flags: list = field(default_factory=list)
    components: list = field(default_factory=list)
    texts: list = field(default_factory=list)
    graphics: list = field(default_factory=list)
    extra: list = field(default_factory=list)      # líneas no interpretadas
    sheet: tuple = (1, 880, 680)
    version: str = "4"
    encoding: str | None = None
    path: str = ""

    def clone(self) -> "Schematic":
        return copy.deepcopy(self)

    # ------------------------------------------------------------- nombres
    def next_name(self, prefix: str) -> str:
        p = prefix[:1].upper() or "X"
        if p == "X":
            p = "U"
        used = {c.name.upper() for c in self.components}
        i = 1
        while f"{p}{i}".upper() in used:
            i += 1
        return f"{p}{i}"

    @property
    def directives(self) -> list:
        return [t for t in self.texts if t.is_directive]


# ------------------------------------------------------------------ lectura
def parse_asc(text: str) -> Schematic:
    sch = Schematic()
    cur: Component | None = None
    last_flag: Flag | None = None
    for raw in text.split("\n"):
        line = raw.rstrip()
        if not line.strip():
            continue
        head, _, rest = line.strip().partition(" ")
        h = head.upper()
        try:
            if h == "VERSION":
                sch.version = rest.strip()
            elif h == "SHEET":
                p = rest.split()
                sch.sheet = tuple(int(v) for v in p[:3])
            elif h == "WIRE":
                p = [int(v) for v in rest.split()[:4]]
                sch.wires.append(Wire(*p))
                cur = None
            elif h == "FLAG":
                p = rest.split(None, 2)
                last_flag = Flag(int(p[0]), int(p[1]), p[2] if len(p) > 2 else "")
                sch.flags.append(last_flag)
                cur = None
            elif h == "IOPIN":
                p = rest.split()
                for f in reversed(sch.flags):
                    if f.x == int(p[0]) and f.y == int(p[1]):
                        f.iopin = p[2] if len(p) > 2 else "BiDir"
                        break
            elif h == "SYMBOL":
                p = rest.split()
                cur = Component(p[0], int(p[1]), int(p[2]), p[3] if len(p) > 3 else "R0")
                sch.components.append(cur)
            elif h == "WINDOW" and cur is not None:
                p = rest.split()
                cur.windows[int(p[0])] = Window(int(p[1]), int(p[2]), p[3],
                                                int(p[4]) if len(p) > 4 else 2)
            elif h == "SYMATTR" and cur is not None:
                k, _, v = rest.partition(" ")
                cur.attrs[k] = v
            elif h == "TEXT":
                p = rest.split(None, 4)
                sch.texts.append(Text(int(p[0]), int(p[1]), p[2], int(p[3]),
                                      p[4] if len(p) > 4 else ""))
                cur = None
            elif h in ("LINE", "RECTANGLE", "CIRCLE", "ARC"):
                p = rest.split()
                nums = [int(v) for v in p[1:]]
                need = 8 if h == "ARC" else 4
                sch.graphics.append(Graphic(h, nums[:need],
                                            nums[need] if len(nums) > need else 0))
                cur = None
            else:
                sch.extra.append(line)
        except (ValueError, IndexError):
            sch.extra.append(line)
    return sch


def load_asc(path: str) -> Schematic:
    text, enc = read_text(path)
    sch = parse_asc(text)
    sch.encoding = enc
    sch.path = path
    return sch


# ---------------------------------------------------------------- escritura
def dump_asc(sch: Schematic) -> str:
    out = [f"Version {sch.version}", "SHEET " + " ".join(str(v) for v in sch.sheet)]
    # Mismo orden y orientación que usa LTspice (afecta a la numeración N001...)
    def norm(w):
        if (w.y1, w.x1) >= (w.y2, w.x2):
            return (w.x1, w.y1, w.x2, w.y2)
        return (w.x2, w.y2, w.x1, w.y1)
    for x1, y1, x2, y2 in sorted((norm(w) for w in sch.wires), key=lambda t: (t[1], t[0])):
        out.append(f"WIRE {x1} {y1} {x2} {y2}")
    for f in sch.flags:
        out.append(f"FLAG {f.x} {f.y} {f.name}")
        if f.iopin:
            out.append(f"IOPIN {f.x} {f.y} {f.iopin}")
    for c in sch.components:
        out.append(f"SYMBOL {c.symbol} {c.x} {c.y} {c.orient}")
        for n, w in c.windows.items():
            out.append(f"WINDOW {n} {w.x} {w.y} {w.just} {w.size}")
        for k, v in c.attrs.items():
            out.append(f"SYMATTR {k} {v}")
    for t in sch.texts:
        out.append(f"TEXT {t.x} {t.y} {t.just} {t.size} {t.text}")
    for g in sch.graphics:
        coords = " ".join(str(v) for v in g.coords)
        tail = f" {g.style}" if g.style else ""
        out.append(f"{g.kind} Normal {coords}{tail}")
    out += sch.extra
    return "\n".join(out) + "\n"


def save_asc(sch: Schematic, path: str) -> None:
    write_text(path, dump_asc(sch), sch.encoding if sch.encoding else None)
    sch.path = path
