"""Base de datos de modelos de LTspice.

* lib/cmp/standard_*.db  (SQLite, una tabla con columna "Model") o, si no
  existe, los archivos de texto lib/cmp/standard.*
* lib/sub/*.lib|*.sub  (subcircuitos; muchos de ADI vienen cifrados)
"""
from __future__ import annotations

import json
import os
import re
import sqlite3

from . import config
from .textio import is_probably_encrypted, read_text

# Familias de la base de datos estándar: extensión -> (tipos aceptados)
FAMILIES = {
    "res": "Resistor", "cap": "Capacitor", "ind": "Inductor", "dio": "Diode",
    "bjt": "BJT", "mos": "MOSFET", "jft": "JFET", "bead": "Ferrite bead",
}
PREFIX_FAMILY = {"R": "res", "C": "cap", "L": "ind", "D": "dio", "Q": "bjt",
                 "M": "mos", "J": "jft", "Z": "mos"}

_MODEL_RE = re.compile(r"^\s*\.model\s+(\S+)\s+([A-Za-z]+)", re.I)
_SUBCKT_RE = re.compile(r"^\s*\.subckt\s+(\S+)", re.I | re.M)


def _is_utf8(b: bytes) -> bool:
    try:
        b.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _join_continuations(text: str) -> list[str]:
    out: list[str] = []
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith("+") and out:
            out[-1] += " " + s[1:].strip()
        elif s:
            out.append(s)
    return out


class ModelEntry:
    __slots__ = ("name", "mtype", "text", "family", "attrs")

    def __init__(self, name: str, mtype: str, text: str, family: str):
        self.name = name
        self.mtype = mtype.upper()
        self.text = text
        self.family = family
        self.attrs = _model_attrs(text)

    @property
    def manufacturer(self) -> str:
        return self.attrs.get("mfg", "")


def _model_attrs(text: str) -> dict:
    out = {}
    for k, v in re.findall(r"(\w+)\s*=\s*([^\s()]+|\"[^\"]*\")", text):
        out[k.lower()] = v.strip('"')
    return out


class ModelDB:
    def __init__(self, lib_dirs: list[str], extra_dirs: list[str] | None = None):
        self.lib_dirs = lib_dirs
        self.models: dict[str, ModelEntry] = {}      # nombre en minúsculas
        self.by_family: dict[str, list[ModelEntry]] = {f: [] for f in FAMILIES}
        self.parts: dict[str, list[dict]] = {f: [] for f in FAMILIES}
        self.sub_dirs = [os.path.join(d, "sub") for d in lib_dirs
                         if os.path.isdir(os.path.join(d, "sub"))]
        self.sub_dirs += [d for d in (extra_dirs or []) if os.path.isdir(d)]
        self._subckt_index: dict[str, str] | None = None
        self._encrypted: dict[str, bool] = {}
        self._load_standard()

    # -------------------------------------------------------- estándar
    def _load_standard(self) -> None:
        for d in self.lib_dirs:
            cmp_dir = os.path.join(d, "cmp")
            if not os.path.isdir(cmp_dir):
                continue
            for fam in FAMILIES:
                lines: list[str] = []
                db = os.path.join(cmp_dir, f"standard_{fam}.db")
                txt = os.path.join(cmp_dir, f"standard.{fam}")
                if os.path.isfile(db):
                    try:
                        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                        tables = [r[0] for r in con.execute(
                            "select name from sqlite_master where type='table' "
                            "and name not like 'sqlite_%'")]
                        for t in tables:
                            cols = [r[1] for r in con.execute(f'pragma table_info("{t}")')]
                            if "Model" in cols:
                                for (m,) in con.execute(f'select "Model" from "{t}"'):
                                    if m:
                                        lines += _join_continuations(str(m))
                            else:
                                # Tabla de partes pasivas (R, C, L, perlas)
                                con.text_factory = lambda b: b.decode("utf-8", "replace") \
                                    if _is_utf8(b) else b.decode("latin-1")
                                cur = con.execute(f'select * from "{t}"')
                                names = [c[0] for c in cur.description]
                                for row in cur:
                                    rec = {k: v for k, v in zip(names, row)
                                           if k != "id" and v is not None}
                                    self.parts[fam].append(rec)
                        con.close()
                    except sqlite3.Error:
                        lines = []
                if not lines and os.path.isfile(txt):
                    try:
                        lines = _join_continuations(read_text(txt)[0])
                    except OSError:
                        pass
                for line in lines:
                    m = _MODEL_RE.match(line)
                    if not m:
                        continue
                    key = m.group(1).lower()
                    if key in self.models:
                        continue
                    e = ModelEntry(m.group(1), m.group(2), line, fam)
                    self.models[key] = e
                    self.by_family[fam].append(e)
        for fam in self.by_family:
            self.by_family[fam].sort(key=lambda e: e.name.lower())

    def find_model(self, name: str) -> ModelEntry | None:
        return self.models.get(name.lower())

    def family_models(self, prefix: str, mtype: str | None = None) -> list[ModelEntry]:
        fam = PREFIX_FAMILY.get(prefix[:1].upper())
        if not fam:
            return []
        items = self.by_family.get(fam, [])
        if mtype:
            mt = mtype.upper()
            items = [e for e in items if e.mtype.startswith(mt[:3]) or mt.startswith(e.mtype)]
        return items

    # ----------------------------------------------------- subcircuitos
    def _index_path(self) -> str:
        return os.path.join(config.data_dir(), "subckt_index.json")

    def subckt_index(self) -> dict[str, str]:
        """nombre de subcircuito (minúsculas) -> archivo que lo define."""
        if self._subckt_index is not None:
            return self._subckt_index
        sig = {d: os.path.getmtime(d) for d in self.sub_dirs if os.path.isdir(d)}
        try:
            with open(self._index_path(), encoding="utf-8") as fh:
                cached = json.load(fh)
            if cached.get("sig") == {k: v for k, v in sig.items()}:
                self._subckt_index = cached["index"]
                self._encrypted = cached.get("encrypted", {})
                return self._subckt_index
        except (OSError, ValueError, KeyError):
            pass
        idx: dict[str, str] = {}
        enc: dict[str, bool] = {}
        for d in self.sub_dirs:
            for dirpath, _dirs, files in os.walk(d):
                for f in files:
                    if not f.lower().endswith((".lib", ".sub", ".mod", ".cir")):
                        continue
                    p = os.path.join(dirpath, f)
                    if is_probably_encrypted(p):
                        enc[p] = True
                        idx.setdefault(os.path.splitext(f)[0].lower(), p)
                        continue
                    try:
                        text, _ = read_text(p)
                    except OSError:
                        continue
                    for name in _SUBCKT_RE.findall(text):
                        idx.setdefault(name.lower(), p)
        self._subckt_index = idx
        self._encrypted = enc
        try:
            with open(self._index_path(), "w", encoding="utf-8") as fh:
                json.dump({"sig": sig, "index": idx, "encrypted": enc}, fh)
        except OSError:
            pass
        return idx

    def is_encrypted(self, path: str) -> bool:
        if path not in self._encrypted:
            self._encrypted[path] = is_probably_encrypted(path)
        return self._encrypted[path]

    def find_lib_file(self, name: str, near: list[str] | None = None) -> str | None:
        """Busca un archivo de modelo (p. ej. LTC.lib) junto al esquemático
        o en lib/sub y lib/cmp, sin distinguir mayúsculas."""
        n = name.strip().strip('"')
        if os.path.isabs(n) and os.path.isfile(n):
            return n
        dirs = list(near or []) + self.sub_dirs + [
            os.path.join(d, "cmp") for d in self.lib_dirs]
        for d in dirs:
            p = os.path.join(d, n)
            if os.path.isfile(p):
                return p
            low = n.lower().replace("\\", "/")
            try:
                for e in os.listdir(d):
                    if e.lower() == low:
                        return os.path.join(d, e)
            except OSError:
                pass
        return None

    def subckts_in(self, path: str) -> list[str]:
        try:
            if self.is_encrypted(path):
                return []
            return _SUBCKT_RE.findall(read_text(path)[0])
        except OSError:
            return []
