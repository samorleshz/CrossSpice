"""Configuración y búsqueda de recursos (librería de LTspice y ngspice)."""
from __future__ import annotations

import glob
import json
import os
import sys

APP_NAME = "CrossSpice"
IS_WINDOWS = sys.platform.startswith("win")


def config_dir() -> str:
    if IS_WINDOWS:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    d = os.path.join(base, APP_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def data_dir() -> str:
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    d = os.path.join(base, APP_NAME)
    os.makedirs(d, exist_ok=True)
    return d


_SETTINGS_FILE = None
_settings: dict | None = None


def settings() -> dict:
    global _settings, _SETTINGS_FILE
    if _settings is None:
        _SETTINGS_FILE = os.path.join(config_dir(), "settings.json")
        try:
            with open(_SETTINGS_FILE, encoding="utf-8") as fh:
                _settings = json.load(fh)
        except (OSError, ValueError):
            _settings = {}
    return _settings


def save_settings() -> None:
    s = settings()
    tmp = _SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(s, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, _SETTINGS_FILE)


def _is_lib_dir(path: str) -> bool:
    return bool(path) and os.path.isdir(os.path.join(path, "sym"))


def candidate_lib_dirs() -> list[str]:
    """Posibles ubicaciones de la carpeta lib/ de LTspice (sym, sub, cmp)."""
    home = os.path.expanduser("~")
    c: list[str] = []
    env = os.environ.get("CROSSSPICE_LTSPICE_LIB")
    if env:
        c.append(env)
    if IS_WINDOWS:
        la = os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
        docs = os.path.join(home, "Documents")
        c += [
            os.path.join(la, "LTspice", "lib"),
            os.path.join(docs, "LTspiceXVII", "lib"),
            os.path.join(docs, "LTspice", "lib"),
            r"C:\Program Files\LTC\LTspiceXVII\lib",
            r"C:\Program Files\ADI\LTspice\lib",
        ]
    else:
        user = os.environ.get("USER", "")
        wine = os.environ.get("WINEPREFIX", os.path.join(home, ".wine"))
        c += [
            os.path.join(data_dir(), "lib"),
            os.path.join(wine, "drive_c", "users", user, "AppData", "Local", "LTspice", "lib"),
            os.path.join(wine, "drive_c", "users", user, "Local Settings",
                         "Application Data", "LTspice", "lib"),
            os.path.join(wine, "drive_c", "users", user, "Documents", "LTspiceXVII", "lib"),
            os.path.join(wine, "drive_c", "Program Files", "LTC", "LTspiceXVII", "lib"),
            os.path.join(home, "Documents", "LTspiceXVII", "lib"),
            os.path.join(home, "LTspice", "lib"),
            "/usr/share/ltspice/lib",
        ]
    # Copia incluida junto a la aplicación (opcional)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    c.append(os.path.join(here, "lib"))
    return c


def find_lib_dirs() -> list[str]:
    """Carpetas lib/ en orden de prioridad. La primera es la principal;
    las demás (p. ej. Documents/LTspiceXVII/lib del usuario) se añaden."""
    s = settings()
    out: list[str] = []
    for p in s.get("lib_dirs", []):
        if _is_lib_dir(p) and p not in out:
            out.append(os.path.normpath(p))
    if not out:
        for p in candidate_lib_dirs():
            p = os.path.normpath(p)
            if _is_lib_dir(p) and p not in out:
                out.append(p)
    return out


def ngspice_candidates() -> list[str]:
    s = settings()
    c: list[str] = []
    if s.get("ngspice_library"):
        c.append(s["ngspice_library"])
    env = os.environ.get("NGSPICE_LIBRARY_PATH")
    if env:
        c.append(env)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if IS_WINDOWS:
        c += [
            os.path.join(here, "ngspice", "bin", "ngspice.dll"),
            os.path.join(here, "Spice64_dll", "dll-vs", "ngspice.dll"),
            os.path.join(data_dir(), "ngspice", "Spice64_dll", "dll-vs", "ngspice.dll"),
            r"C:\Spice64_dll\dll-vs\ngspice.dll",
            r"C:\Program Files\Spice64_dll\dll-vs\ngspice.dll",
        ]
        for pf in (r"C:\Program Files\KiCad", r"C:\Program Files (x86)\KiCad"):
            c += sorted(glob.glob(os.path.join(pf, "*", "bin", "ngspice.dll")), reverse=True)
    elif sys.platform == "darwin":
        c += ["/opt/homebrew/lib/libngspice.dylib", "/usr/local/lib/libngspice.dylib",
              "/Applications/KiCad/KiCad.app/Contents/Frameworks/libngspice.dylib"]
    else:
        c += ["libngspice.so.0", "libngspice.so",
              "/usr/lib/x86_64-linux-gnu/libngspice.so.0",
              "/usr/lib/libngspice.so.0", "/usr/lib64/libngspice.so.0",
              "/usr/local/lib/libngspice.so.0", "/usr/local/lib/libngspice.so"]
    return c


def codemodel_dirs(dll_path: str) -> list[str]:
    """Carpetas donde buscar los code models (*.cm) de ngspice."""
    d = os.path.dirname(dll_path) if dll_path else ""
    c = []
    if d:
        c += [os.path.join(d, "..", "lib", "ngspice"), os.path.join(d, "ngspice"),
              os.path.join(d, "..", "lib")]
    if not IS_WINDOWS:
        c += ["/usr/lib/x86_64-linux-gnu/ngspice", "/usr/lib/ngspice", "/usr/lib64/ngspice",
              "/usr/local/lib/ngspice"]
    return [os.path.normpath(x) for x in c if os.path.isdir(x)]
