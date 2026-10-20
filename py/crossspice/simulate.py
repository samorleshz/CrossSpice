"""Ejecución de simulaciones (incluye .step param, que ngspice no tiene).

ngspice corre en un proceso hijo (ver simworker.py) para que un fallo del
simulador nunca cierre la interfaz."""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from .netlist import Netlist
from .units import fmt, spice_num

KIND_BY_PREFIX = {"tran": "tran", "ac": "ac", "dc": "dc", "op": "op", "noise": "noise",
                  "tf": "tf", "sp": "sp", "disto": "disto", "pz": "pz", "sens": "sens"}


@dataclass
class Plot:
    name: str                       # tran1, ac1...
    kind: str                       # tran, ac, dc, op, noise, tf
    scale: str                      # vector de la escala (time, frequency...)
    vectors: dict                   # nombre -> np.ndarray
    step_label: str = ""

    @property
    def x(self) -> np.ndarray | None:
        return self.vectors.get(self.scale)


@dataclass
class SimResult:
    plots: list = field(default_factory=list)
    log: str = ""
    ok: bool = False
    elapsed: float = 0.0
    measurements: list = field(default_factory=list)   # (step, nombre, valor)
    errors: list = field(default_factory=list)
    version: str = ""

    def kinds(self) -> list[str]:
        out = []
        for p in self.plots:
            if p.kind not in out:
                out.append(p.kind)
        return out

    def plots_of(self, kind: str) -> list[Plot]:
        return [p for p in self.plots if p.kind == kind]


_ERR_RX = re.compile(r"\berror\b|undefined parameter|unknown (?:parameter|subckt|model)|"
                     r"unable to find|no such|timestep too small|singular matrix|"
                     r"expression err|could not find", re.I)
_SCALES = ("time", "frequency", "v-sweep", "i-sweep", "temp-sweep", "res-sweep", "sweep")


def _scale_of(vecs: dict, kind: str) -> str:
    for s in _SCALES:
        if s in vecs:
            return s
    for k in vecs:
        if k.endswith("-sweep"):
            return k
    return next(iter(vecs), "")


def _with_param(text: str, assigns: dict) -> str:
    """Inserta .param nombre=valor y quita definiciones previas del mismo nombre."""
    names = {k.lower() for k in assigns}
    out = []
    for line in text.split("\n"):
        if line.lower().startswith(".param"):
            parts = re.findall(r"(\w+)\s*=\s*(\{[^}]*\}|\S+)", line[6:])
            keep = [f"{k}={v}" for k, v in parts if k.lower() not in names]
            if not keep:
                continue
            line = ".param " + " ".join(keep)
        if line.strip().lower() == ".end":
            for k, v in assigns.items():
                out.append(f".param {k}={spice_num(v)}")
        out.append(line)
    return "\n".join(out)


def _worker_cmd(job: str, out: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--simworker", job, out]
    return [sys.executable, "-m", "crossspice.simworker", job, out]


def expand_runs(netlist: Netlist) -> list[tuple[str, str]]:
    if not netlist.steps:
        return [("", netlist.text)]
    combos: list[dict] = [{}]
    for spec in netlist.steps[:3]:
        combos = [dict(c, **{spec.param: v}) for c in combos for v in spec.values]
    return [(", ".join(f"{k}={fmt(v)}" for k, v in c.items()), _with_param(netlist.text, c))
            for c in combos[:2000]]


def run(netlist: Netlist, on_output=None, should_stop=None, timeout=None,
        on_progress=None) -> SimResult:
    """Ejecuta la netlist en un proceso aparte y recoge los resultados."""
    res = SimResult()
    t0 = time.time()
    runs = expand_runs(netlist)
    work = tempfile.mkdtemp(prefix="crossspice_run_")
    job = os.path.join(work, "job.json")
    out = os.path.join(work, "result.npz")
    with open(job, "w", encoding="utf-8") as fh:
        json.dump({"runs": runs, "timeout": timeout}, fh)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    flags = 0x08000000 if sys.platform.startswith("win") else 0   # CREATE_NO_WINDOW
    proc = subprocess.Popen(_worker_cmd(job, out), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=env, cwd=work,
                            creationflags=flags)
    lines: queue.Queue = queue.Queue()

    def reader():
        for raw in iter(proc.stdout.readline, b""):
            lines.put(raw.decode("utf-8", "replace").rstrip("\r\n"))
        lines.put(None)

    threading.Thread(target=reader, daemon=True).start()
    log: list[str] = []
    done = stopped = False
    while True:
        try:
            line = lines.get(timeout=0.1)
        except queue.Empty:
            if should_stop and not stopped and should_stop():
                stopped = True
                proc.kill()
            continue
        if line is None:
            break
        if line.startswith("@@"):
            if line.startswith("@@DONE"):
                done = True
            elif line.startswith("@@FATAL"):
                res.errors.append(line[8:].replace("\\n", "\n"))
            elif line.startswith("@@PROGRESS") and on_progress:
                a, b = line.split()[1:3]
                on_progress(int(a), int(b))
            elif line.startswith("@@VERSION"):
                res.version = line[10:]
            continue
        log.append(line)
        if on_output:
            on_output(line)
    proc.wait()
    if stopped:
        log.append("Simulación detenida por el usuario.")
    elif not done and not res.errors:
        res.errors.append(f"ngspice terminó de forma inesperada (código {proc.returncode}). "
                          "Suele deberse a un modelo que ngspice no soporta.")
    if os.path.isfile(out):
        try:
            data = np.load(out)
            meta = json.loads(bytes(data["__meta__"]).decode("utf-8"))
            for m in meta:
                vecs = {name: data[key] for name, key in m["vectors"]}
                kind = KIND_BY_PREFIX.get(re.sub(r"\d+$", "", m["plot"]), m["plot"])
                res.plots.append(Plot(m["plot"], kind, _scale_of(vecs, kind), vecs,
                                      m["label"]))
            data.close()
        except (OSError, ValueError, KeyError) as exc:
            res.errors.append(f"No se pudieron leer los resultados: {exc}")
    shutil.rmtree(work, ignore_errors=True)

    label = ""
    for line in log:
        if line.startswith("──── .step "):
            label = line[11:].rstrip(" ─")
            continue
        low = line.lstrip().lower()
        m = re.match(r"^\s*(\w+)\s*=\s*([-+0-9.eE]+)(\s|$)", line)
        if m and not low.startswith(("warning", "error", "note", "no.")):
            try:
                res.measurements.append((label, m.group(1), float(m.group(2))))
            except ValueError:
                pass
        if _ERR_RX.search(line) and "no error" not in low:
            res.errors.append(line.strip())
    # Los mensajes genéricos de ngspice no ayudan si hay otros más concretos
    generic = ("cannot recover", "aren't any circuits", "circuit not parsed",
               "please check your input", "error on line")
    specific = [e for e in res.errors if not any(g in e.lower() for g in generic)]
    if specific:
        res.errors = list(dict.fromkeys(specific))
    res.log = "\n".join(log)
    res.ok = bool(res.plots)
    res.elapsed = time.time() - t0
    return res


# ---------------------------------------------------------------- nombres
_Q = {"i": "I", "id": "Id", "ic": "Ic", "ib": "Ib", "ie": "Ie", "is": "Is", "ig": "Ig",
      "current": "I", "p": "P"}


def display_name(vec: str) -> str:
    """Nombre de vector de ngspice -> notación de LTspice: V(out), I(R1)..."""
    m = re.match(r"^@([a-z][\w.]*)\[(\w+)\]$", vec, re.I)
    if m:
        return f"{_Q.get(m.group(2).lower(), m.group(2))}({m.group(1).upper()})"
    m = re.match(r"^([\w.]+)#branch$", vec, re.I)
    if m:
        return f"I({m.group(1).upper()})"
    if vec.lower().startswith(("v(", "i(")):
        return vec[0].upper() + vec[1:]
    if vec in _SCALES or vec.endswith("-sweep"):
        return vec
    return f"V({vec})"


def is_internal(vec: str) -> bool:
    """Vectores auxiliares (nodos internos de parásitos) que no se listan."""
    v = vec.lower()
    if re.search(r"_n\d+$|_rs$", v):
        return True
    if re.match(r"^@\w+_(ser|par)\[", v) or "#branch" in v and "_ser" in v:
        return True
    return False
