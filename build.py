"""Prepara la carpeta web/ para publicarse como PWA.

1. Copia el núcleo Python de CrossSpice (crossspice/*.py, sin la interfaz Qt)
   a web/py/crossspice/, que es lo que carga Pyodide.
2. Regenera la librería básica (web/lib/basic).
3. Escribe web/precache.json: la lista de archivos que el service worker
   guarda para funcionar sin conexión, con una versión que cambia cuando
   cambia cualquiera de ellos.

Uso:  python web/build.py      (correrlo después de cambiar el núcleo o la web)
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

WEB = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.join(os.path.dirname(WEB), "crossspice")
CORE_FILES = ["__init__.py", "config.py", "textio.py", "units.py", "symbols.py",
              "schematic.py", "models.py", "adevices.py", "netlist.py", "simulate.py"]
SKIP_DIRS = {"__pycache__"}
SKIP_FILES = {"build.py", "precache.json", "README.md", "make_basic.py", ".nojekyll"}
SKIP_PREFIX = ("vendor/pyodide/pyodide.mjs", "vendor/pyodide/LICENSE", "vendor/eecircuit/LICENSE")


def main():
    dest = os.path.join(WEB, "py", "crossspice")
    os.makedirs(dest, exist_ok=True)
    for f in CORE_FILES:
        shutil.copy2(os.path.join(CORE, f), os.path.join(dest, f))
    subprocess.run([sys.executable, os.path.join(WEB, "lib", "make_basic.py")], check=True)
    # GitHub Pages (Jekyll) omite los archivos que empiezan con "_" como
    # __init__.py; este archivo vacío le dice que publique todo tal cual.
    open(os.path.join(WEB, ".nojekyll"), "w").close()

    files = []
    h = hashlib.sha256()
    for dirpath, dirnames, names in os.walk(WEB):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for n in sorted(names):
            p = os.path.join(dirpath, n)
            rel = os.path.relpath(p, WEB).replace(os.sep, "/")
            if n in SKIP_FILES and os.path.dirname(rel) in ("", "lib") or \
                    rel.startswith(SKIP_PREFIX) or rel == "sw.js":
                continue
            files.append(rel)
            with open(p, "rb") as fh:
                h.update(rel.encode() + fh.read())
    sw_path = os.path.join(WEB, "sw.js")
    with open(sw_path, encoding="utf-8") as fh:
        sw = fh.read()
    h.update(re.sub(r'const VERSION = "[^"]*";', "", sw).encode())
    version = h.hexdigest()[:12]
    with open(sw_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(re.sub(r'const VERSION = "[^"]*";', f'const VERSION = "{version}";', sw))
    with open(os.path.join(WEB, "precache.json"), "w", encoding="utf-8") as fh:
        json.dump({"version": version, "files": ["./"] + files}, fh, indent=1)
    size = sum(os.path.getsize(os.path.join(WEB, f)) for f in files)
    print(f"{len(files)} archivos, {size / 1e6:.1f} MB, versión {version}")


if __name__ == "__main__":
    main()
