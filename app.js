/* CrossSpice Web: controlador principal. */
import { Editor, compose } from "./editor.js";
import { Plot, si } from "./plot.js";
import * as store from "./store.js";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const SIM_TIMEOUT_MS = 120000;

// ================================================================= workers
class Rpc {
  constructor(worker, onStatus) {
    this.w = worker;
    this.n = 0;
    this.pending = new Map();
    worker.onmessage = (e) => {
      const m = e.data;
      if (m.type === "status") { onStatus?.(m.text); return; }
      const p = this.pending.get(m.id);
      if (!p) return;
      this.pending.delete(m.id);
      clearTimeout(p.timer);
      if (m.ok) p.resolve(m.result);
      else { const err = new Error(m.error); err.dead = m.dead; p.reject(err); }
    };
    worker.onerror = (e) => {
      for (const p of this.pending.values()) { clearTimeout(p.timer); p.reject(new Error(e.message || "Error en el worker")); }
      this.pending.clear();
    };
  }
  call(op, args = {}, { timeout = 0, transfer = [] } = {}) {
    const id = ++this.n;
    return new Promise((resolve, reject) => {
      const p = { resolve, reject };
      if (timeout) p.timer = setTimeout(() => {
        this.pending.delete(id);
        const err = new Error("La simulación tardó demasiado y se detuvo.");
        err.timeout = true;
        reject(err);
      }, timeout);
      this.pending.set(id, p);
      this.w.postMessage({ id, op, args }, transfer);
    });
  }
  terminate() {
    this.w.terminate();
    for (const p of this.pending.values()) { clearTimeout(p.timer); const e = new Error("Simulación detenida."); e.stopped = true; p.reject(e); }
    this.pending.clear();
  }
}

/* ngspice en su propio worker; si se cuelga o falla, se descarta y se crea otro. */
class SpiceEngine {
  constructor() { this.rpc = null; this.ready = null; this.version = ""; }
  ensure() {
    if (!this.rpc) {
      this.rpc = new Rpc(new Worker("spice-worker.js", { type: "module" }));
      this.ready = this.rpc.call("init").then((r) => { this.version = r.version; return r; });
      this.ready.catch(() => this.kill());
    }
    return this.ready;
  }
  async run(text) {
    await this.ensure();
    try {
      return await this.rpc.call("run", { text }, { timeout: SIM_TIMEOUT_MS });
    } catch (e) {
      if (e.dead || e.timeout) { this.kill(); this.ensure().catch(() => {}); }
      throw e;
    }
  }
  kill() { this.rpc?.terminate(); this.rpc = null; this.ready = null; }
}

const core = new Rpc(new Worker("core-worker.js"), (t) => splash(t));
const spice = new SpiceEngine();

// ================================================================= estado
const state = {
  scene: null, name: "Sin título.asc", dirty: false, tool: "select",
  results: null, running: false, lib: { zip: false }, tree: null,
};
let editor, plot;

// ================================================================= utilidades UI
let toastTimer;
function toast(msg, { error = false, ms = 3500 } = {}) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.toggle("error", error);
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), ms);
}
function hint(text) {
  $("hint").innerHTML = text ? `<span>${esc(text)}</span>` : "";
}
let splashSteps = 0;
function splash(text, done = false) {
  if (text) $("splash-status").textContent = text;
  splashSteps++;
  $("splash-bar").style.width = Math.min(95, 8 + splashSteps * 12) + "%";
  if (done) {
    $("splash-bar").style.width = "100%";
    setTimeout(() => $("splash").classList.add("hide"), 150);
    setTimeout(() => { $("splash").hidden = true; }, 600);
  }
}

/* Diálogo genérico. buttons: [{label, value, cls}] -> promesa con value (null si se cancela). */
function dialog({ title, body, buttons = [{ label: "Cerrar", value: null }], onOpen, wide = false }) {
  const dlg = $("dlg"), form = $("dlg-form");
  form.innerHTML = "";
  dlg.style.width = wide ? "min(820px, calc(100vw - 24px))" : "";
  const head = document.createElement("div"); head.className = "dlg-head"; head.textContent = title;
  const bd = document.createElement("div"); bd.className = "dlg-body";
  if (typeof body === "string") bd.innerHTML = body; else if (body) bd.appendChild(body);
  const foot = document.createElement("div"); foot.className = "dlg-foot";
  form.append(head, bd, foot);
  return new Promise((resolve) => {
    let done = false;
    const close = (v) => { if (done) return; done = true; dlg.close(); resolve(v); };
    for (const b of buttons) {
      if (b.spacer) { const s = document.createElement("span"); s.className = "grow"; foot.appendChild(s); continue; }
      const btn = document.createElement("button");
      btn.type = "button"; btn.className = "btn " + (b.cls || ""); btn.textContent = b.label;
      btn.onclick = () => close(b.value);
      foot.appendChild(btn);
    }
    dlg.oncancel = (e) => { e.preventDefault(); close(null); };
    form.onsubmit = (e) => { e.preventDefault(); const def = buttons.find((b) => b.cls?.includes("primary")); close(def ? def.value : null); };
    dlg.closeFn = close;
    dlg.showModal();
    onOpen?.(bd, close);
  });
}
function field(label, value = "", { id, type = "text", placeholder = "", list, attrs = "" } = {}) {
  return `<div class="field"><label for="${id}">${esc(label)}</label>
    <input id="${id}" type="${type}" value="${esc(value)}" placeholder="${esc(placeholder)}" autocomplete="off"
      autocapitalize="off" spellcheck="false" ${list ? `list="${list}"` : ""} ${attrs}></div>`;
}
async function ask(title, label, value = "", extra = "") {
  const v = await dialog({
    title, body: field(label, value, { id: "ask-in" }) + extra,
    buttons: [{ label: "Cancelar", value: null }, { label: "Aceptar", value: "ok", cls: "primary" }],
    onOpen: (bd) => { const i = bd.querySelector("#ask-in"); i.focus(); i.select(); },
  });
  return v === "ok" ? $("ask-in").value.trim() : null;
}
async function confirmBox(title, text, ok = "Aceptar", danger = false) {
  return (await dialog({ title, body: `<p>${esc(text)}</p>`,
    buttons: [{ label: "Cancelar", value: false }, { label: ok, value: true, cls: danger ? "primary danger" : "primary" }] })) === true;
}

// ================================================================= documento
async function op(name, args = {}) {
  try {
    const sc = await core.call(name, args);
    if (name === "delete") editor.selection = null;
    applyScene(sc, !["scene", "open", "new", "save"].includes(name));
    return sc;
  } catch (e) {
    toast(e.message, { error: true, ms: 6000 });
    throw e;
  }
}
function applyScene(sc, changed) {
  state.scene = sc;
  editor.setScene(sc);
  $("btn-undo").disabled = !sc.undo;
  $("btn-redo").disabled = !sc.redo;
  $("analysis-label").textContent = sc.analysis ? sc.analysis.split(/\s+/)[0] : "—";
  $("btn-analysis").title = sc.analysis || "Sin comando de simulación";
  if (changed) { state.dirty = true; autosave(); }
  showName();
}
function showName() {
  $("docname").textContent = (state.dirty ? "• " : "") + state.name;
}
let saveTimer;
function autosave() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    try {
      const { text } = await core.call("save");
      await store.kv.set("current", { name: state.name, text, dirty: state.dirty });
    } catch (e) { /* sin espacio: no es grave */ }
  }, 400);
}
async function openText(text, name, { dirty = false } = {}) {
  await core.call("rename", { name });
  state.name = name;
  await op("open", { text, name });
  state.dirty = dirty;
  showName();
  closeResults();
  setTool("select");
  requestAnimationFrame(() => editor.fit());
  autosave();
}
async function saveAs(name) {
  name = name.trim();
  if (!name) return false;
  if (!/\.asc$/i.test(name)) name += ".asc";
  const existing = await store.files.get(name);
  if (existing && name !== state.name &&
      !await confirmBox("Guardar", `Ya existe «${name}». ¿Reemplazarlo?`, "Reemplazar")) return false;
  await core.call("rename", { name });
  state.name = name;
  const { text } = await core.call("save");
  await store.files.put(name, text);
  state.dirty = false;
  showName();
  autosave();
  toast(`Guardado «${name}» en la tableta.`);
  return true;
}
async function save() {
  if (/^Sin título/i.test(state.name)) {
    const n = await ask("Guardar", "Nombre del archivo", "");
    if (n) await saveAs(n);
    return;
  }
  await saveAs(state.name);
}
async function exportFile() {
  // Misma codificación que LTspice (latin-1 o UTF-16LE, CRLF)
  const { b64 } = await core.call("export");
  const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  const file = new File([bytes], state.name, { type: "text/plain" });
  if (navigator.canShare?.({ files: [file] }) && matchMedia("(pointer: coarse)").matches) {
    try { await navigator.share({ files: [file], title: state.name }); return; } catch (e) { if (e.name === "AbortError") return; }
  }
  const a = document.createElement("a");
  a.href = URL.createObjectURL(file);
  a.download = state.name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  toast(`Descargado «${state.name}».`);
}
async function decodeAsc(buf) {
  const b = new Uint8Array(buf);
  let enc = "utf-8";
  if (b[0] === 0xff && b[1] === 0xfe) enc = "utf-16le";
  else if (b.length > 4 && b[1] === 0 && b[3] === 0) enc = "utf-16le";
  else { try { new TextDecoder("utf-8", { fatal: true }).decode(b); } catch (e) { enc = "latin1"; } }
  return new TextDecoder(enc).decode(b).replace(/^\ufeff/, "");
}

// ================================================================= herramientas
const PARTS = { res: "res", cap: "cap", ind: "ind", diode: "diode", voltage: "voltage" };
const HINTS = {
  select: "Toque para elegir · mantenga presionado para editar · arrastre para mover",
  wire: "Toque donde empieza el cable (o arrastre de un punto a otro)",
  delete: "Toque lo que quiera borrar",
  probe: "Toque un cable para ver su voltaje, o una resistencia o fuente para ver su corriente",
};
function markTool(tool) {
  for (const b of document.querySelectorAll(".tool[data-tool]")) {
    b.classList.toggle("active", b.dataset.tool === tool);
  }
  $("btn-probe").classList.toggle("active", tool === "probe");
}
async function setTool(tool, placing = null) {
  state.tool = tool;
  markTool(tool);
  if (tool === "place") {
    editor.setMode("place", placing);
    hint(placing.type === "comp" ? `Toque donde colocar ${placing.label || placing.symbol} (Rotar gira el siguiente)`
      : placing.type === "flag" ? (placing.name === "0" ? "Toque un pin o un cable para poner la tierra"
        : `Toque un pin o un cable para poner la etiqueta «${placing.name}»`) : "Toque donde colocar el texto");
  } else {
    editor.setMode(tool);
    hint(HINTS[tool] || "");
  }
}
async function startPart(symbol, label, tool) {
  try {
    const preview = await core.call("symbol_preview", { name: symbol, orient: "R0" });
    await setTool("place", { type: "comp", symbol, orient: "R0", preview, label });
    markTool(tool);
  } catch (e) { toast(e.message, { error: true }); }
}
async function rotateOrMirror(opName) {
  const p = editor.mode === "place" ? editor.placing : null;
  if (p && p.type === "comp") {
    p.orient = compose(p.orient, opName);
    p.preview = await core.call("symbol_preview", { name: p.symbol, orient: p.orient });
  }
  const sel = editor.selection;
  if (sel && sel[0] !== "graphic") await op("transform", { items: [sel], op: opName });
  else if (!p) toast("Primero toque un componente o cable para elegirlo.");
}
async function onTool(tool) {
  if (tool === "rotate") return rotateOrMirror("R90");
  if (tool === "mirror") return rotateOrMirror("M0");
  if (tool === "delete") {
    if (editor.selection) { await op("delete", { items: [editor.selection] }); return; }
    return setTool("delete");
  }
  if (tool === "select" || tool === "wire") return setTool(tool);
  if (tool === "ground") return setTool("place", { type: "flag", name: "0" });
  if (tool === "label") {
    const names = [...new Set(state.scene.flags.filter((f) => f.name !== "0").map((f) => f.name))];
    const extra = names.length ? `<div class="muted">Etiquetas en uso (mismo nombre = misma red):</div>
      <div class="chips">${names.map((n) => `<button type="button" class="chip" data-n="${esc(n)}">${esc(n)}</button>`).join("")}</div>` : "";
    const p = dialog({
      title: "Etiqueta de red", body: field("Nombre de la red", "", { id: "ask-in", placeholder: "p. ej. out, vin, vcc" }) + extra,
      buttons: [{ label: "Cancelar", value: null }, { label: "Colocar", value: "ok", cls: "primary" }],
      onOpen: (bd, close) => {
        bd.querySelector("#ask-in").focus();
        bd.querySelectorAll("[data-n]").forEach((b) => b.onclick = () => { $("ask-in").value = b.dataset.n; close("ok"); });
      },
    });
    const v = await p;
    const name = v === "ok" ? $("ask-in").value.trim() : "";
    if (!name) return;
    if (/\s/.test(name)) { toast("El nombre no puede tener espacios.", { error: true }); return; }
    return setTool("place", { type: "flag", name });
  }
  if (PARTS[tool]) return startPart(PARTS[tool], tool === "voltage" ? "V" : tool[0].toUpperCase(), tool);
  if (tool === "component") {
    const name = await pickComponent();
    if (name) await startPart(name, name.split("/").pop(), "component");
  }
}

// ================================================================= selector de componentes
async function pickComponent() {
  if (!state.tree) state.tree = await core.call("symbol_tree");
  const tree = state.tree;
  const wrap = document.createElement("div");
  wrap.innerHTML = `
    ${field("Buscar", "", { id: "pk-q", placeholder: "p. ej. LT1001, 2N3904, opamp, npn" })}
    <div class="picker"><div class="col" id="pk-list"></div>
    <div class="preview"><svg id="pk-svg"></svg><b id="pk-name"></b><span class="muted" id="pk-desc"></span></div></div>
    <p class="muted">${state.lib.zip ? `Librería de LTspice cargada (${state.lib.symbols} símbolos).`
      : "Solo la librería básica. Para tener todos los componentes de LTspice, importe su lib.zip (menú Archivo → Librería)."}</p>`;
  let chosen = null;
  const total = tree.reduce((n, [, names]) => n + names.length, 0);
  const render = (q) => {
    const list = wrap.querySelector("#pk-list");
    q = q.trim().toLowerCase();
    let html = "", n = 0;
    for (const [dir, names] of tree) {
      const hits = q ? names.filter((s) => (dir + "/" + s).toLowerCase().includes(q)) : names;
      if (!hits.length) continue;
      const open = q || dir === "" || total < 80;
      html += `<div class="dir" data-dir="${esc(dir)}">${esc(dir || "Básicos")} <span class="muted">(${hits.length})</span></div>`;
      if (open) for (const s of hits.slice(0, 400)) {
        const full = dir ? dir + "/" + s : s;
        html += `<div class="sym" data-s="${esc(full)}">${esc(s)}</div>`;
        if (++n > 600) break;
      }
      if (n > 600) break;
    }
    list.innerHTML = html || `<p class="muted" style="padding:12px">No hay coincidencias.</p>`;
  };
  const preview = async (name) => {
    chosen = name;
    wrap.querySelectorAll(".sym").forEach((e) => e.classList.toggle("sel", e.dataset.s === name));
    const d = await core.call("symbol_preview", { name });
    const svg = wrap.querySelector("#pk-svg");
    const [x0, y0, x1, y1] = d.bb;
    const pad = 24;
    svg.setAttribute("viewBox", `${x0 - pad} ${y0 - pad} ${x1 - x0 + 2 * pad} ${y1 - y0 + 2 * pad}`);
    svg.innerHTML = d.shapes.map(([p, , f]) => f ? `<path d="${p}" class="boxfill"/>` : "").join("") +
      d.shapes.map(([p]) => `<path d="${p}" class="shape"/>`).join("");
    wrap.querySelector("#pk-name").textContent = name.split("/").pop();
    wrap.querySelector("#pk-desc").textContent = d.description || "";
  };
  const v = await dialog({
    title: "Componente", body: wrap, wide: true,
    buttons: [{ label: "Cancelar", value: null }, { label: "Colocar", value: "ok", cls: "primary" }],
    onOpen: (bd, close) => {
      render("");
      const q = bd.querySelector("#pk-q");
      q.oninput = () => render(q.value);
      bd.querySelector("#pk-list").onclick = (e) => {
        const s = e.target.closest(".sym"), d = e.target.closest(".dir");
        if (s) { if (chosen === s.dataset.s) close("ok"); else preview(s.dataset.s); }
        else if (d) { q.value = d.dataset.dir + "/"; render(q.value); }
      };
    },
  });
  return v === "ok" ? chosen : null;
}

// ================================================================= edición de objetos
const SOURCE_TEMPLATES = [["DC", "5"], ["Seno", "SINE(0 1 1k)"], ["Pulso", "PULSE(0 5 0 1u 1u 0.5m 1m)"],
  ["PWL", "PWL(0 0 1m 5)"], ["Exp", "EXP(0 5 0 1m 2m 1m)"]];
async function editItem(hit) {
  const [k, i] = hit;
  if (k === "comp") return editComp(i);
  if (k === "flag") {
    const f = state.scene.flags[i];
    if (f.name === "0") {
      const v = await dialog({ title: "Tierra", body: "<p>Nodo 0 (referencia).</p>",
        buttons: [{ label: "Borrar", value: "del", cls: "danger" }, { spacer: true }, { label: "Cerrar", value: null }] });
      if (v === "del") await op("delete", { items: [hit] });
      return;
    }
    const v = await dialog({ title: "Etiqueta de red", body: field("Nombre de la red", f.name, { id: "ask-in" }),
      buttons: [{ label: "Borrar", value: "del", cls: "danger" }, { spacer: true }, { label: "Cancelar", value: null },
        { label: "Aceptar", value: "ok", cls: "primary" }],
      onOpen: (bd) => bd.querySelector("#ask-in").select() });
    if (v === "del") await op("delete", { items: [hit] });
    else if (v === "ok") {
      const name = $("ask-in").value.trim();
      if (name && !/\s/.test(name)) await op("set_flag", { i, name, io: f.io });
      else toast("El nombre no puede estar vacío ni tener espacios.", { error: true });
    }
    return;
  }
  if (k === "text") {
    const t = state.scene.texts[i];
    if (t.directive && /^\s*\.(tran|ac|dc|op)\b/i.test(t.text) && !t.text.trim().includes("\n")) return editAnalysis();
    const v = await dialog({
      title: t.directive ? "Directiva SPICE" : "Comentario",
      body: `<div class="field"><label for="tx-in">Texto</label><textarea id="tx-in" spellcheck="false">${esc(t.text)}</textarea></div>
        <label><input type="checkbox" id="tx-dir" ${t.directive ? "checked" : ""}> Es una directiva SPICE (empieza con punto, p. ej. .param, .model)</label>`,
      buttons: [{ label: "Borrar", value: "del", cls: "danger" }, { spacer: true }, { label: "Cancelar", value: null },
        { label: "Aceptar", value: "ok", cls: "primary" }],
    });
    if (v === "del") await op("delete", { items: [hit] });
    else if (v === "ok") await op("set_text", { i, text: $("tx-in").value, directive: $("tx-dir").checked });
    return;
  }
  if (k === "wire") {
    const v = await dialog({ title: "Cable", body: `<p>Red: <b>${esc(state.scene.wires[i][4] || "—")}</b></p>
      <p class="muted">Arrastre el cable para moverlo o, ya elegido, arrastre un extremo para alargarlo.</p>`,
      buttons: [{ label: "Borrar", value: "del", cls: "danger" }, { spacer: true }, { label: "Cerrar", value: null }] });
    if (v === "del") await op("delete", { items: [hit] });
  }
}

async function editComp(i) {
  const info = await core.call("comp_info", { i });
  const a = info.attrs;
  const L = info.letter;
  let body = `<p class="muted">${esc(info.symbol.replace(/\\/g, "/"))}${info.description ? " — " + esc(info.description) : ""}</p>`;
  body += `<div class="row">${field("Nombre", info.name, { id: "c-name" })}`;
  if ("RCL".includes(L) && info.npins === 2) {
    const ph = { R: "p. ej. 1k, 4.7k, 10meg", C: "p. ej. 100n, 1u, 10p", L: "p. ej. 10m, 100u" }[L];
    body += field("Valor", a.Value, { id: "c-Value", placeholder: ph }) + `</div>`;
    body += field("Parámetros extra (opcional)", a.SpiceLine, { id: "c-SpiceLine",
      placeholder: L === "C" ? "Rser=0.01" : L === "L" ? "Rser=0.1" : "tol=1" });
    body += `<p class="muted">Sufijos: f p n u m k meg g (meg = millón; m = mili).</p>`;
  } else if ("VI".includes(L) && info.npins === 2) {
    body += `</div>` + field(L === "V" ? "Voltaje (DC o forma de onda)" : "Corriente (DC o forma de onda)", a.Value,
      { id: "c-Value", placeholder: "p. ej. 5, SINE(0 1 1k), PULSE(0 5 0 1u 1u 1m 2m)" });
    body += `<div class="chips">${SOURCE_TEMPLATES.map(([n, v]) => `<button type="button" class="chip" data-tpl="${esc(v)}">${n}</button>`).join("")}</div>`;
    body += field("Señal pequeña para .ac (opcional)", a.Value2, { id: "c-Value2", placeholder: "AC 1" });
    body += `<p class="muted">SINE(offset amplitud frecuencia) · PULSE(v1 v2 retardo subida bajada ancho periodo)</p>`;
  } else if ("DQMJ".includes(L)) {
    body += field("Modelo", a.Value, { id: "c-Value", list: "c-models" }) + `</div>`;
    body += `<datalist id="c-models">${info.models.map(([n, t, m]) => `<option value="${esc(n)}">${esc(t)} ${esc(m)}</option>`).join("")}</datalist>`;
    if (info.models.length) {
      body += `<div class="field"><label>Modelos disponibles (${info.models.length})</label>
        <input id="c-filter" placeholder="Filtrar…" autocomplete="off"></div><ul class="list" id="c-mlist" style="max-height:34vh;overflow:auto"></ul>`;
    }
    if (a.SpiceLine) body += field("Parámetros extra", a.SpiceLine, { id: "c-SpiceLine" });
  } else {
    body += field("Valor", a.Value, { id: "c-Value" }) + `</div>`;
    for (const key of ["Value2", "SpiceLine", "SpiceLine2", "SpiceModel"]) {
      if (a[key]) body += field(key, a[key], { id: "c-" + key });
    }
  }
  const v = await dialog({
    title: `Editar ${info.name}`, body,
    buttons: [{ label: "Borrar", value: "del", cls: "danger" }, { spacer: true }, { label: "Cancelar", value: null },
      { label: "Aceptar", value: "ok", cls: "primary" }],
    onOpen: (bd) => {
      bd.querySelectorAll("[data-tpl]").forEach((b) => b.onclick = () => { $("c-Value").value = b.dataset.tpl; $("c-Value").focus(); });
      const ml = bd.querySelector("#c-mlist");
      if (ml) {
        const draw = (q) => {
          q = (q || "").toLowerCase();
          ml.innerHTML = info.models.filter(([n, , m]) => !q || n.toLowerCase().includes(q) || m.toLowerCase().includes(q))
            .slice(0, 300).map(([n, t, m]) => `<li class="pick" data-m="${esc(n)}"><span class="name">${esc(n)}<span class="sub">${esc(t)} · ${esc(m)}</span></span></li>`).join("");
        };
        draw("");
        bd.querySelector("#c-filter").oninput = (e) => draw(e.target.value);
        ml.onclick = (e) => { const li = e.target.closest("[data-m]"); if (li) { $("c-Value").value = li.dataset.m; ml.querySelectorAll("li").forEach((x) => x.classList.toggle("sel", x === li)); } };
      }
      const first = bd.querySelector("#c-Value");
      if (first && matchMedia("(pointer: fine)").matches) first.focus();
    },
  });
  if (v === "del") { await op("delete", { items: [["comp", i]] }); return; }
  if (v !== "ok") return;
  const attrs = { InstName: $("c-name").value.trim() };
  for (const key of ["Value", "Value2", "SpiceLine", "SpiceLine2", "SpiceModel"]) {
    const e = $("c-" + key);
    if (e) attrs[key] = e.value;
  }
  await op("set_comp", { i, attrs });
}

// ================================================================= comando de simulación
function parseAnalysis(line) {
  const t = (line || "").trim().split(/\s+/);
  const kind = (t[0] || ".tran").slice(1).toLowerCase();
  const nums = t.slice(1).filter((x) => !/^(uic|startup|steady|nodiscard|noopiter)$/i.test(x));
  const r = { kind };
  if (kind === "tran") {
    if (nums.length === 1) r.tstop = nums[0];
    else { r.tstep = nums[0] || ""; r.tstop = nums[1] || ""; r.tstart = nums[2] || ""; r.dtmax = nums[3] || ""; }
  } else if (kind === "ac") {
    [r.type, r.n, r.f1, r.f2] = nums;
  } else if (kind === "dc") {
    [r.src, r.v1, r.v2, r.dv] = nums;
  }
  return r;
}
async function editAnalysis() {
  const cur = parseAnalysis(state.scene.analysis || ".tran 1m");
  const srcs = state.scene.comps.filter((c) => "VI".includes(c.prefix)).map((c) => c.name);
  const wrap = document.createElement("div");
  wrap.innerHTML = `
    <div class="tabs">${[["tran", "Transitorio"], ["ac", "AC"], ["dc", "Barrido DC"], ["op", "Punto de op."]]
      .map(([k, n]) => `<button type="button" class="chip" data-k="${k}">${n}</button>`).join("")}</div>
    <div data-p="tran">${field("Tiempo final", cur.tstop || "1m", { id: "a-tstop", placeholder: "p. ej. 10m" })}
      <div class="row">${field("Empezar a guardar en (opcional)", cur.tstart || "", { id: "a-tstart", placeholder: "0" })}
      ${field("Paso máximo (opcional)", cur.dtmax || "", { id: "a-dtmax", placeholder: "automático" })}</div></div>
    <div data-p="ac"><div class="field"><label for="a-type">Barrido</label><select id="a-type">
      <option value="dec">Décadas</option><option value="oct">Octavas</option><option value="lin">Lineal</option></select></div>
      <div class="row">${field("Puntos (por década)", cur.n || "100", { id: "a-n" })}
      ${field("Frecuencia inicial", cur.f1 || "10", { id: "a-f1" })}${field("Frecuencia final", cur.f2 || "100k", { id: "a-f2" })}</div>
      <p class="muted">Ponga «AC 1» en la señal pequeña de la fuente de entrada.</p></div>
    <div data-p="dc">${field("Fuente a barrer", cur.src || srcs[0] || "V1", { id: "a-src", list: "a-srcs" })}
      <datalist id="a-srcs">${srcs.map((s) => `<option value="${esc(s)}">`).join("")}</datalist>
      <div class="row">${field("Desde", cur.v1 || "0", { id: "a-v1" })}${field("Hasta", cur.v2 || "5", { id: "a-v2" })}
      ${field("Paso", cur.dv || "0.01", { id: "a-dv" })}</div></div>
    <div data-p="op"><p>Calcula los voltajes y corrientes de corriente directa (capacitores abiertos, inductores en corto).</p></div>
    <p class="muted">Comando: <code id="a-cmd"></code></p>`;
  let kind = ["tran", "ac", "dc", "op"].includes(cur.kind) ? cur.kind : "tran";
  const build = () => {
    const g = (id) => $(id).value.trim();
    if (kind === "tran") {
      const ts = g("a-tstart"), dm = g("a-dtmax");
      return ts || dm ? `.tran 0 ${g("a-tstop")} ${ts || 0}${dm ? " " + dm : ""}` : `.tran ${g("a-tstop")}`;
    }
    if (kind === "ac") return `.ac ${g("a-type")} ${g("a-n")} ${g("a-f1")} ${g("a-f2")}`;
    if (kind === "dc") return `.dc ${g("a-src")} ${g("a-v1")} ${g("a-v2")} ${g("a-dv")}`;
    return ".op";
  };
  const v = await dialog({
    title: "Comando de simulación", body: wrap,
    buttons: [{ label: "Cancelar", value: null }, { label: "Aceptar", value: "ok", cls: "primary" }],
    onOpen: (bd) => {
      if (cur.type) $("a-type").value = cur.type.toLowerCase();
      const show = () => {
        bd.querySelectorAll("[data-k]").forEach((b) => b.classList.toggle("active", b.dataset.k === kind));
        bd.querySelectorAll("[data-p]").forEach((p) => p.hidden = p.dataset.p !== kind);
        $("a-cmd").textContent = build();
      };
      bd.querySelectorAll("[data-k]").forEach((b) => b.onclick = () => { kind = b.dataset.k; show(); });
      bd.oninput = show; bd.onchange = show;
      show();
    },
  });
  if (v !== "ok") return;
  await op("set_analysis", { cmd: build() });
}

// ================================================================= simulación
function analysisKind(nl) {
  for (const a of nl.analyses || []) {
    const m = /^\s*\.(\w+)/.exec(a);
    if (m) return m[1].toLowerCase();
  }
  const m = /^\s*\.(tran|ac|dc|op|noise|tf)\b/im.exec(nl.runs[0]?.text || "");
  return m ? m[1].toLowerCase() : "tran";
}
function vecMap(res) {
  const m = new Map();
  for (const v of res.vectors) m.set(v.name.toLowerCase(), v);
  return m;
}
async function simulate() {
  if (state.running) { spice.kill(); spice.ensure().catch(() => {}); return; }
  let nl;
  try { nl = await core.call("netlist"); } catch (e) { toast(e.message, { error: true }); return; }
  if (nl.errors.length) { await problems("No se puede simular", nl); return; }
  if (!nl.runs.length) return;
  const kind = analysisKind(nl);
  if (!["tran", "ac", "dc", "op"].includes(kind)) {
    toast(`El análisis .${kind} todavía no se grafica en la versión para tableta.`, { error: true });
  }
  state.running = true;
  const btn = $("btn-run");
  btn.classList.add("busy");
  btn.lastElementChild.textContent = "Detener";
  const t0 = performance.now();
  const runs = [];
  try {
    for (let k = 0; k < nl.runs.length; k++) {
      hint(nl.runs.length > 1 ? `Simulando ${k + 1} de ${nl.runs.length} (${nl.runs[k].label})…` : "Simulando…");
      const res = await spice.run(nl.runs[k].text);
      runs.push({ label: nl.runs[k].label, res, vecs: vecMap(res) });
    }
  } catch (e) {
    hint("");
    if (e.stopped) toast("Simulación detenida.");
    else await problems("La simulación falló", { errors: [e.message], warnings: nl.warnings, runs: nl.runs });
    return;
  } finally {
    state.running = false;
    btn.classList.remove("busy");
    btn.lastElementChild.textContent = "Simular";
  }
  const secs = (performance.now() - t0) / 1000;
  state.results = { kind, runs, nl };
  showResults(kind, runs, secs);
  if (nl.warnings.length) toast(`${nl.warnings.length} aviso(s): ${nl.warnings[0]}`, { ms: 6000 });
}

async function problems(title, nl) {
  const list = (a, cls) => a.length ? `<ul class="msgs ${cls}">${a.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : "";
  const v = await dialog({ title,
    body: list(nl.errors || [], "err") + (nl.warnings?.length ? "<b>Avisos</b>" + list(nl.warnings, "") : ""),
    buttons: [{ label: "Ver netlist", value: "nl" }, { spacer: true }, { label: "Cerrar", value: null, cls: "primary" }] });
  if (v === "nl") showNetlist(nl);
}
async function showNetlist(nl) {
  if (!nl) { try { nl = await core.call("netlist"); } catch (e) { toast(e.message, { error: true }); return; } }
  const pre = document.createElement("pre");
  pre.className = "netlist";
  pre.textContent = nl.runs.length ? nl.runs[0].text : "(vacía)";
  const div = document.createElement("div");
  if (nl.runs.length > 1) div.innerHTML = `<p class="muted">${nl.runs.length} corridas (.step); se muestra la primera.</p>`;
  div.appendChild(pre);
  await dialog({ title: "Netlist para ngspice", body: div, wide: true });
}

function showResults(kind, runs, secs) {
  const panel = $("results");
  const wasOpen = !panel.hidden;
  panel.hidden = false;
  const steps = runs.length > 1 ? ` · ${runs.length} pasos` : "";
  $("results-title").textContent = `${state.scene.analysis || "." + kind} — ${secs.toFixed(2)} s${steps}`;
  const isOp = kind === "op";
  $("plot").hidden = isOp;
  $("optable").hidden = !isOp;
  $("btn-clear-traces").hidden = isOp;
  if (isOp) {
    renderOpTable(runs);
    if (!wasOpen) requestAnimationFrame(() => editor.fit());
    setTool("select");
    hint("");
    return;
  }
  const plotRuns = runs.map((r) => ({ label: r.label, x: r.res.vectors[0].re, complex: r.res.complex }));
  plot.setRuns(plotRuns, kind);
  if (kind === "dc") {
    const src = (state.results.nl.analyses[0] || "").split(/\s+/)[1] || "";
    plot.xUnit = /^i/i.test(src) ? "A" : "V";
  }
  setTool("probe");
  requestAnimationFrame(() => { plot.draw(); if (!wasOpen) editor.fit(); });
}
function renderOpTable(runs) {
  const names = [];
  for (const r of runs) for (const v of r.res.vectors) if (!names.includes(v.name)) names.push(v.name);
  const pretty = (n) => {
    const l = n.toLowerCase();
    let m = /^i\((.+)\)$/.exec(l) || /^(.+)#branch$/.exec(l);
    if (m) return ["I(" + m[1].toUpperCase() + ")", "A"];
    m = /^v\((.+)\)$/.exec(l);
    return ["V(" + (m ? m[1] : l) + ")", "V"];
  };
  const rows = names.filter((n) => !/#|^@|^v\(.*[_#]n\d+\)$/i.test(n) || /#branch$/i.test(n))
    .map((n) => [n, ...pretty(n)]).sort((a, b) => (a[2] > b[2] ? -1 : a[2] < b[2] ? 1 : a[1].localeCompare(b[1])));
  const head = runs.length > 1 ? `<tr><th></th>${runs.map((r) => `<th>${esc(r.label)}</th>`).join("")}</tr>` : "";
  $("optable").innerHTML = `<table>${head}${rows.map(([n, label, unit]) => `<tr><td>${esc(label)}</td>${runs.map((r) => {
    const v = r.vecs.get(n.toLowerCase());
    return `<td class="num">${v ? si(v.re[0], unit, 5) : "—"}</td>`;
  }).join("")}</tr>`).join("")}</table>`;
}
function closeResults() {
  const was = !$("results").hidden;
  $("results").hidden = true;
  if (was) requestAnimationFrame(() => editor.fit());
  state.results = null;
  if (state.tool === "probe") setTool("select");
}

// ------------------------------------------------------------- sondas
function parseSpiceNumber(s) {
  s = String(s || "").trim().toLowerCase().replace(/µ/g, "u");
  let m = /^([+-]?\d+)(meg|[tgkmunpfr])(\d+)$/.exec(s);    // 4k7, 2r2
  if (m) s = `${m[1]}.${m[3]}${m[2] === "r" ? "" : m[2]}`;
  m = /^([+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?)(meg|mil|[tgkmunpf])?[a-z]*$/.exec(s);
  if (!m) return NaN;
  const mult = { t: 1e12, g: 1e9, meg: 1e6, k: 1e3, m: 1e-3, u: 1e-6, n: 1e-9, p: 1e-12, f: 1e-15, mil: 25.4e-6 };
  return parseFloat(m[1]) * (m[2] ? mult[m[2]] : 1);
}
function nodeVec(r, net) {
  if (net === "0") {
    const n = r.res.vectors[0].re.length;
    return { re: new Float64Array(n), im: r.res.complex ? new Float64Array(n) : null };
  }
  return r.vecs.get(`v(${net})`) || r.vecs.get(net) || null;
}
function probe(hit) {
  const R = state.results;
  if (!R) return;
  const [k, i] = hit;
  const sc = state.scene;
  let net = null;
  if (k === "wire") net = sc.wires[i][4];
  else if (k === "flag") net = sc.flags[i].net;
  if (net) {
    if (net === "0") { toast("Ese cable es tierra (0 V)."); return; }
    const per = R.runs.map((r) => nodeVec(r, net));
    if (!per.some(Boolean)) { toast(`No hay resultados para la red ${net}.`, { error: true }); return; }
    const label = `V(${net})`;
    if (!plot.addTrace(label, "V", per)) toast(`${label} ya está en la gráfica (toque su nombre para quitarla).`);
    return;
  }
  if (k === "comp") {
    const c = sc.comps[i];
    const L = c.prefix;
    if ("VI".includes(L) && L === "V") {
      const name = c.name.toLowerCase();
      const per = R.runs.map((r) => r.vecs.get(`i(${name})`) || r.vecs.get(`${name}#branch`) || null);
      if (per.some(Boolean)) { plot.addTrace(`I(${c.name})`, "A", per); return; }
    }
    if (L === "R" && c.nets.length === 2) {
      const ohms = parseSpiceNumber(c.value);
      if (!isFinite(ohms) || ohms === 0) { toast(`No pude leer el valor de ${c.name} (${c.value}).`, { error: true }); return; }
      const per = R.runs.map((r) => {
        const a = nodeVec(r, c.nets[0]), b = nodeVec(r, c.nets[1]);
        if (!a || !b) return null;
        const re = a.re.map((v, j) => (v - b.re[j]) / ohms);
        const im = a.im ? a.im.map((v, j) => (v - (b.im ? b.im[j] : 0)) / ohms) : null;
        return { re, im };
      });
      if (!plot.addTrace(`I(${c.name})`, "A", per)) toast(`I(${c.name}) ya está en la gráfica.`);
      return;
    }
    toast("ngspice para tableta no entrega la corriente de este componente; toque un cable para ver voltajes.");
  }
}

// ================================================================= archivos y librería
async function fileMenu() {
  const v = await dialog({
    title: "Archivo",
    body: `<div class="menu">
      <button type="button" class="btn" data-v="new">Nuevo<small>Esquemático vacío</small></button>
      <button type="button" class="btn" data-v="open">Abrir…<small>Guardados, ejemplos o desde la tableta</small></button>
      <button type="button" class="btn" data-v="save">Guardar<small>En esta tableta</small></button>
      <button type="button" class="btn" data-v="saveas">Guardar como…<small>Con otro nombre</small></button>
      <button type="button" class="btn" data-v="export">Exportar .asc<small>Descargar o compartir (abre en LTspice)</small></button>
      <button type="button" class="btn" data-v="import">Importar .asc<small>Desde archivos de la tableta</small></button>
      <button type="button" class="btn" data-v="directive">Directiva SPICE…<small>.param, .model, .step, .include…</small></button>
      <button type="button" class="btn" data-v="netlist">Ver netlist<small>Lo que recibe ngspice</small></button>
      <button type="button" class="btn" data-v="lib">Librería de LTspice…<small>${state.lib.zip ? "Cargada" : "Importar lib.zip"}</small></button>
      <button type="button" class="btn" data-v="about">Acerca de<small>Versión y licencias</small></button>
    </div>`,
    onOpen: (bd, close) => bd.querySelectorAll("[data-v]").forEach((b) => b.onclick = () => close(b.dataset.v)),
  });
  if (v === "new") {
    if (state.dirty && !await confirmBox("Nuevo", "Hay cambios sin guardar en este esquemático. ¿Descartarlos?", "Descartar", true)) return;
    state.name = "Sin título.asc";
    await op("new", { name: state.name });
    state.dirty = false; showName(); closeResults(); editor.fit(); autosave();
  } else if (v === "open") await openMenu();
  else if (v === "save") await save();
  else if (v === "saveas") { const n = await ask("Guardar como", "Nombre del archivo", state.name.replace(/\.asc$/i, "")); if (n) await saveAs(n); }
  else if (v === "export") await exportFile();
  else if (v === "import") $("file-asc").click();
  else if (v === "directive") await addDirective();
  else if (v === "netlist") await showNetlist();
  else if (v === "lib") await libraryMenu();
  else if (v === "about") await about();
}

async function openMenu() {
  const saved = await store.files.list();
  let examples = [];
  try { examples = await (await fetch("examples/index.json")).json(); } catch (e) { /* sin ejemplos */ }
  const fmt = (t) => new Date(t).toLocaleString("es-CR", { dateStyle: "short", timeStyle: "short" });
  const body = `
    <b>Guardados en esta tableta</b>
    ${saved.length ? `<ul class="list">${saved.map((f) => `<li class="pick" data-open="${esc(f.name)}"><span class="name">${esc(f.name)}<span class="sub">${fmt(f.mtime)}</span></span>
      <button type="button" class="mini" data-del="${esc(f.name)}" title="Borrar">🗑</button></li>`).join("")}</ul>` : `<p class="muted">Todavía no hay esquemáticos guardados.</p>`}
    <p><b>Ejemplos</b></p>
    <ul class="list">${examples.map((x) => `<li class="pick" data-ex="${esc(x.file)}"><span class="name">${esc(x.title)}<span class="sub">${esc(x.desc || "")}</span></span></li>`).join("")}</ul>`;
  const v = await dialog({
    title: "Abrir", body,
    buttons: [{ label: "Desde la tableta…", value: "file" }, { spacer: true }, { label: "Cancelar", value: null }],
    onOpen: (bd, close) => {
      bd.onclick = async (e) => {
        const del = e.target.closest("[data-del]");
        if (del) {
          e.stopPropagation();
          await store.files.remove(del.dataset.del);
          del.closest("li").remove();
          return;
        }
        const li = e.target.closest("[data-open],[data-ex]");
        if (li) close(li.dataset.open ? "f:" + li.dataset.open : "e:" + li.dataset.ex);
      };
    },
  });
  if (!v) return;
  if (state.dirty && v !== "file" &&
      !await confirmBox("Abrir", "Hay cambios sin guardar en el esquemático actual. ¿Descartarlos?", "Descartar", true)) return;
  if (v === "file") { $("file-asc").click(); return; }
  if (v.startsWith("f:")) {
    const f = await store.files.get(v.slice(2));
    if (f) await openText(f.text, f.name);
  } else {
    const file = v.slice(2);
    const buf = await (await fetch("examples/" + file)).arrayBuffer();
    await openText(await decodeAsc(buf), file);
  }
}

async function addDirective() {
  const v = await dialog({
    title: "Directiva SPICE",
    body: `<div class="field"><label for="tx-in">Directiva</label><textarea id="tx-in" spellcheck="false" placeholder=".param R=1k&#10;.step param R list 1k 2k 5k"></textarea></div>
      <p class="muted">Después toque el esquemático donde quiera ponerla.</p>`,
    buttons: [{ label: "Cancelar", value: null }, { label: "Colocar", value: "ok", cls: "primary" }],
    onOpen: (bd) => bd.querySelector("#tx-in").focus(),
  });
  const text = v === "ok" ? $("tx-in").value.trim() : "";
  if (text) setTool("place", { type: "text", text, directive: true });
}

async function libraryMenu() {
  const info = state.lib;
  const v = await dialog({
    title: "Librería de LTspice",
    body: info.zip
      ? `<p>Cargada: <b>${info.symbols}</b> símbolos, <b>${info.models}</b> modelos y <b>${info.subs}</b> archivos de subcircuitos (${info.mb} MB).</p>
         <p class="muted">Queda guardada en esta tableta. Algunos modelos de Analog Devices vienen cifrados y solo LTspice puede simularlos.</p>`
      : `<p>CrossSpice trae una librería básica (R, C, L, diodos, fuentes, transistores, opamp ideal y algunos modelos comunes).</p>
         <p>Para usar todos los componentes de LTspice, copie a la tableta el archivo <b>lib.zip</b> de su instalación de LTspice e impórtelo aquí. En Windows está en:</p>
         <p><code>C:\\Users\\<i>usuario</i>\\AppData\\Local\\Programs\\ADI\\LTspice\\lib.zip</code></p>
         <p class="muted">La librería es de Analog Devices: por eso no se incluye en la app. Se guarda solo en su tableta.</p>`,
    buttons: info.zip
      ? [{ label: "Quitar", value: "rm", cls: "danger" }, { spacer: true }, { label: "Reemplazar…", value: "imp" }, { label: "Cerrar", value: null, cls: "primary" }]
      : [{ label: "Cancelar", value: null }, { label: "Importar lib.zip…", value: "imp", cls: "primary" }],
  });
  if (v === "imp") $("file-zip").click();
  else if (v === "rm") {
    await store.kv.remove("libzip");
    await op("unload_zip");
    state.lib = { zip: false };
    state.tree = null;
    toast("Librería de LTspice quitada.");
  }
}

async function loadLibZip(bytes, { quiet = false } = {}) {
  hint("Cargando la librería de LTspice…");
  try {
    const info = await core.call("load_zip", { bytes }, { transfer: [bytes.buffer] });
    state.lib = { zip: true, ...info };
    state.tree = null;
    await op("scene");
    if (!quiet) toast(`Librería de LTspice cargada: ${info.symbols} símbolos, ${info.models} modelos.`);
    return true;
  } catch (e) {
    toast("No se pudo cargar la librería: " + e.message, { error: true, ms: 7000 });
    return false;
  } finally { hint(HINTS[state.tool] || ""); }
}

async function about() {
  await dialog({
    title: "CrossSpice para tabletas",
    body: `<p>Editor y simulador de circuitos compatible con los archivos .asc de LTspice.</p>
      <ul class="msgs">
        <li>Simulador: ngspice ${esc(spice.version.replace(/^.*ngspice[- ]?/i, "") || "45")} en WebAssembly (eecircuit-engine, licencia MIT; ngspice, licencia BSD).</li>
        <li>Python en el navegador: Pyodide 0.27 (licencia MPL 2.0).</li>
        <li>LTspice y su librería son de Analog Devices; esta app no los incluye.</li>
      </ul>
      <p class="muted">Limitaciones de esta versión: sin modelos XSPICE (el diodo ideal se aproxima), sin corrientes internas de los componentes (se calculan para resistencias), y solo se grafican .tran, .ac, .dc y .op.</p>`,
  });
}

// ================================================================= arranque
function bindUI() {
  $("btn-menu").onclick = fileMenu;
  $("btn-undo").onclick = () => op("undo");
  $("btn-redo").onclick = () => op("redo");
  $("btn-fit").onclick = () => editor.fit();
  $("btn-analysis").onclick = editAnalysis;
  $("btn-run").onclick = simulate;
  $("btn-probe").onclick = () => setTool(state.tool === "probe" ? "select" : "probe");
  $("btn-clear-traces").onclick = () => plot.clear();
  $("btn-close-results").onclick = closeResults;
  for (const b of document.querySelectorAll(".tool[data-tool]")) b.onclick = () => onTool(b.dataset.tool);
  $("file-asc").onchange = async (e) => {
    const f = e.target.files[0];
    e.target.value = "";
    if (!f) return;
    if (state.dirty && !await confirmBox("Abrir", "Hay cambios sin guardar en el esquemático actual. ¿Descartarlos?", "Descartar", true)) return;
    await openText(await decodeAsc(await f.arrayBuffer()), f.name);
    toast(`Abierto «${f.name}». Use Guardar para dejarlo en la tableta.`);
  };
  $("file-zip").onchange = async (e) => {
    const f = e.target.files[0];
    e.target.value = "";
    if (!f) return;
    const buf = await f.arrayBuffer();
    try { await store.kv.set("libzip", new Blob([buf])); } catch (err) { toast("No hubo espacio para guardar la librería en la tableta.", { error: true }); }
    store.persist();
    await loadLibZip(new Uint8Array(buf));
  };
  // Cambiar el alto del panel de resultados
  const grip = $("results-grip");
  grip.addEventListener("pointerdown", (e) => {
    grip.setPointerCapture(e.pointerId);
    const main = $("main").getBoundingClientRect();
    const move = (ev) => {
      const h = Math.max(140, Math.min(main.height - 60, main.bottom - ev.clientY));
      $("results").style.height = h + "px";
    };
    const up = () => { grip.removeEventListener("pointermove", move); grip.removeEventListener("pointerup", up); };
    grip.addEventListener("pointermove", move);
    grip.addEventListener("pointerup", up);
  });
  window.addEventListener("keydown", (e) => {
    if ($("dlg").open || e.target.closest?.("input,textarea")) return;
    const k = e.key.toLowerCase();
    if ((e.ctrlKey || e.metaKey) && k === "z") { e.preventDefault(); op(e.shiftKey ? "redo" : "undo"); }
    else if ((e.ctrlKey || e.metaKey) && k === "y") { e.preventDefault(); op("redo"); }
    else if ((e.ctrlKey || e.metaKey) && k === "s") { e.preventDefault(); save(); }
    else if (k === "escape") setTool("select");
    else if (k === "delete" || k === "backspace") { if (editor.selection) op("delete", { items: [editor.selection] }); }
    else if (k === "r" && !e.ctrlKey) onTool("rotate");
    else if (k === "e" && !e.ctrlKey) onTool("mirror");
    else if (k === "w" || k === "f3") onTool("wire");
    else if (k === "g") onTool("ground");
  });
}

async function main() {
  if ("serviceWorker" in navigator && location.protocol.startsWith("http")) {
    navigator.serviceWorker.register("sw.js").catch(() => {});
  }
  editor = new Editor($("schematic"), {
    op: (name, args) => op(name, args).catch(() => {}),
    onEdit: (hit) => editItem(hit),
    onProbe: (hit) => probe(hit),
    onStatus: (t) => hint(t),
    onPlaced: () => setTool("select"),
    insets: () => {
      const p = $("results");
      if (p.hidden) return { right: 0, bottom: 0 };
      const r = p.getBoundingClientRect(), m = $("main").getBoundingClientRect();
      return r.top > m.top + 4 ? { right: 0, bottom: m.bottom - r.top } : { right: m.right - r.left, bottom: 0 };
    },
  });
  plot = new Plot($("plot"), {
    onLegend: (i) => { const t = plot.traces[i]; plot.removeTrace(i); toast(`Se quitó ${t.label}.`); },
  });
  bindUI();
  splash("Cargando Python y el núcleo de CrossSpice…");
  const spiceReady = spice.ensure().catch((e) => { toast("No se pudo iniciar ngspice: " + e.message, { error: true }); });
  try {
    const r = await core.call("init");
    window.CROSSSPICE = { pythonSeconds: r.seconds };
  } catch (e) {
    $("splash-status").textContent = "Error al iniciar: " + e.message;
    return;
  }
  splash("Abriendo el esquemático…");
  // Librería de LTspice guardada en la tableta
  const libBlob = await store.kv.get("libzip").catch(() => null);
  const cur = await store.kv.get("current").catch(() => null);
  if (cur && cur.text) {
    await openText(cur.text, cur.name, { dirty: cur.dirty });
  } else {
    const buf = await (await fetch("examples/RC paso bajo.asc")).arrayBuffer();
    await openText(await decodeAsc(buf), "RC paso bajo.asc");
  }
  splash("Listo", true);
  requestAnimationFrame(() => editor.fit());
  if (libBlob) {
    splash("Cargando la librería de LTspice…");
    await loadLibZip(new Uint8Array(await libBlob.arrayBuffer()), { quiet: true });
    await op("scene");
  }
  await spiceReady;
  $("btn-run").disabled = false;
  window.CROSSSPICE.ready = true;
}

// Para pruebas automáticas desde la consola
window.CS = { core, spice, state, op, simulate, probe, openText, loadLibZip, get editor() { return editor; }, get plot() { return plot; } };
main();
