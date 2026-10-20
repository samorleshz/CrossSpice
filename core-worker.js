/* Worker del núcleo: Pyodide + núcleo Python de CrossSpice (web/py).
   Recibe {id, op, args} y responde {id, ok, result | error}.
   Mensajes especiales: "init" y "load_zip" (args.bytes = Uint8Array). */
importScripts("vendor/pyodide/pyodide.js");

const PY_FILES = ["__init__.py", "config.py", "textio.py", "units.py", "symbols.py",
  "schematic.py", "models.py", "adevices.py", "netlist.py", "simulate.py"];

let pyodide = null;
let api = null;

const status = (text) => postMessage({ type: "status", text });

async function fetchText(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`No se pudo cargar ${url} (${r.status})`);
  return r.text();
}

async function init() {
  status("Cargando Python…");
  const t0 = performance.now();
  pyodide = await loadPyodide({ indexURL: new URL("vendor/pyodide/", location.href).href });
  const FS = pyodide.FS;
  FS.mkdirTree("/home/pyodide/crossspice");
  status("Cargando el núcleo de CrossSpice…");
  await Promise.all(PY_FILES.map(async (f) =>
    FS.writeFile(`/home/pyodide/crossspice/${f}`, await fetchText(`py/crossspice/${f}`))));
  FS.writeFile("/home/pyodide/webcore.py", await fetchText("py/webcore.py"));
  // Librería básica incluida
  const files = JSON.parse(await fetchText("lib/basic/index.json"));
  await Promise.all(files.map(async (rel) => {
    const dest = "/basic/" + rel;
    FS.mkdirTree(dest.slice(0, dest.lastIndexOf("/")));
    FS.writeFile(dest, await fetchText("lib/basic/" + rel));
  }));
  pyodide.runPython("import sys; sys.path.insert(0, '/home/pyodide')\nimport webcore");
  api = pyodide.runPython("webcore.start()");
  return { seconds: (performance.now() - t0) / 1000,
           python: pyodide.runPython("import sys; sys.version.split()[0]") };
}

function call(op, args) {
  return JSON.parse(api.call(op, JSON.stringify(args || {})));
}

self.onmessage = async (ev) => {
  const { id, op, args } = ev.data;
  try {
    let result;
    if (op === "init") {
      result = await init();
    } else if (op === "load_zip") {
      pyodide.FS.writeFile("/ltlib.zip", args.bytes);
      result = call("load_zip");
    } else {
      result = call(op, args);
    }
    postMessage({ id, ok: true, result });
  } catch (e) {
    let msg = String(e && e.message || e);
    // Errores de Python: quedarse con la última línea (la útil)
    if (msg.includes("Traceback")) {
      const lines = msg.trim().split("\n");
      msg = lines[lines.length - 1].replace(/^\w+Error: /, "");
    }
    postMessage({ id, ok: false, error: msg });
  }
};
