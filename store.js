/* Almacenamiento en la tableta (IndexedDB): esquemáticos guardados, el
   documento abierto y el lib.zip de LTspice importado. */
const DB_NAME = "crossspice";
let dbp = null;

function db() {
  if (!dbp) {
    dbp = new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, 1);
      req.onupgradeneeded = () => {
        const d = req.result;
        if (!d.objectStoreNames.contains("files")) d.createObjectStore("files", { keyPath: "name" });
        if (!d.objectStoreNames.contains("kv")) d.createObjectStore("kv");
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }
  return dbp;
}

async function tx(store, mode, fn) {
  const d = await db();
  return new Promise((resolve, reject) => {
    const t = d.transaction(store, mode);
    const s = t.objectStore(store);
    const req = fn(s);
    t.oncomplete = () => resolve(req ? req.result : undefined);
    t.onerror = () => reject(t.error);
    t.onabort = () => reject(t.error || new Error("transacción cancelada"));
  });
}

export const files = {
  list: () => tx("files", "readonly", (s) => s.getAll())
    .then((a) => a.sort((x, y) => y.mtime - x.mtime)),
  get: (name) => tx("files", "readonly", (s) => s.get(name)),
  put: (name, text) => tx("files", "readwrite", (s) => s.put({ name, text, mtime: Date.now() })),
  remove: (name) => tx("files", "readwrite", (s) => s.delete(name)),
};

export const kv = {
  get: (key) => tx("kv", "readonly", (s) => s.get(key)),
  set: (key, value) => tx("kv", "readwrite", (s) => s.put(value, key)),
  remove: (key) => tx("kv", "readwrite", (s) => s.delete(key)),
};

/* Pide al navegador que no borre los datos si falta espacio. */
export async function persist() {
  try { return await navigator.storage?.persist?.(); } catch (e) { return false; }
}
