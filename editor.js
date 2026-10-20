/* Editor táctil del esquemático (SVG).

   El esquemático vive en el núcleo Python (core-worker.js); aquí solo se
   dibuja la escena que devuelve y se traducen los gestos a operaciones:
   - tocar: seleccionar / colocar / poner cable / sondear
   - mantener presionado (o doble clic): editar
   - arrastrar con un dedo: mover lo seleccionado o desplazar la vista
   - dos dedos: pellizcar para zoom y arrastrar para desplazar */

const NS = "http://www.w3.org/2000/svg";
export const GRID = 16;
const SIZE_MULT = [0.625, 1.0, 1.5, 2.0, 2.5, 3.5, 5.0, 7.0];
const FONT_BASE = 28;
const LONG_PRESS_MS = 520;
const MOVE_PX = 9;

const snap = (v) => Math.round(v / GRID) * GRID;

// ------------------------------------------------- orientaciones (symbols.py)
export function xform(orient, x, y) {
  const o = orient.toUpperCase();
  const rot = (parseInt(o.slice(1), 10) || 0) % 360;
  if (rot === 90) [x, y] = [-y, x];
  else if (rot === 180) [x, y] = [-x, -y];
  else if (rot === 270) [x, y] = [y, -x];
  if (o.startsWith("M")) x = -x;
  return [x + 0, y + 0];
}
const ORIENTS = ["R0", "R90", "R180", "R270", "M0", "M90", "M180", "M270"];
export function compose(orient, extra) {
  const mat = (o) => [xform(o, 1, 0), xform(o, 0, 1)].flat().join(",");
  const a = xform(orient, 1, 0), b = xform(orient, 0, 1);
  const want = [xform(extra, ...a), xform(extra, ...b)].flat().join(",");
  return ORIENTS.find((o) => mat(o) === want) || orient;
}

function el(tag, attrs, parent) {
  const e = document.createElementNS(NS, tag);
  for (const k in attrs) if (attrs[k] !== undefined && attrs[k] !== null) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}

/* Texto anclado como en LTspice (gui/items.py: text_rect). */
function drawText(parent, text, x, y, just, size, vertical, cls) {
  const fs = FONT_BASE * SIZE_MULT[Math.max(0, Math.min(7, size))] / 1.5;
  const lh = fs * 1.16;
  const lines = String(text).split("\n");
  const h = lh * lines.length;
  const g = el("g", { class: cls, transform: `translate(${x} ${y})${vertical ? " rotate(-90)" : ""}` }, parent);
  const anchor = just === "Left" ? "start" : just === "Right" ? "end" : "middle";
  const top = just === "Top" ? 0 : just === "Bottom" ? -h : just === "Center" ? -h / 2 : -lh / 2;
  lines.forEach((ln, k) => {
    let s = ln, over = false;
    if (cls && cls.includes("pin") && s.startsWith("_") && s.length > 1) { s = s.slice(1); over = true; }
    const t = el("text", { x: 0, y: (top + lh * (k + 0.5)).toFixed(2), "font-size": fs.toFixed(2),
      "text-anchor": anchor, "dominant-baseline": "central",
      "text-decoration": over ? "overline" : null }, g);
    t.textContent = s;
  });
  return g;
}

function segDist(px, py, x1, y1, x2, y2) {
  const dx = x2 - x1, dy = y2 - y1;
  const L = dx * dx + dy * dy;
  let t = L ? ((px - x1) * dx + (py - y1) * dy) / L : 0;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy));
}

export class Editor {
  constructor(svg, hooks) {
    this.svg = svg;
    this.hooks = hooks;   // {op(name,args), onEdit(hit), onProbe(hit), onStatus(text), onSelect(hit)}
    this.scene = null;
    this.view = { s: 1, tx: 0, ty: 0 };
    this.mode = "select";
    this.placing = null;  // {type:"comp", symbol, orient, preview} | {type:"flag", name}
    this.selection = null;
    this.wireStart = null;
    this.pointers = new Map();
    this.gesture = null;
    this.items = new Map(); // "kind:i" -> elemento SVG
    this._build();
    this._bind();
  }

  _build() {
    const svg = this.svg;
    svg.innerHTML = "";
    const defs = el("defs", {}, svg);
    const pat = el("pattern", { id: "gridpat", width: GRID, height: GRID, patternUnits: "userSpaceOnUse" }, defs);
    el("rect", { x: -0.9, y: -0.9, width: 1.8, height: 1.8, class: "griddot" }, pat);
    this.world = el("g", { class: "world" }, svg);
    this.gridRect = el("rect", { x: -1e5, y: -1e5, width: 2e5, height: 2e5, fill: "url(#gridpat)", class: "grid" }, this.world);
    this.layer = el("g", {}, this.world);
    this.overlay = el("g", { class: "overlay" }, this.world);
    this._applyView();
  }

  // ======================================================== vista
  _applyView() {
    const { s, tx, ty } = this.view;
    this.world.setAttribute("transform", `matrix(${s} 0 0 ${s} ${tx} ${ty})`);
    this.gridRect.style.display = s > 0.45 ? "" : "none";
  }
  toWorld(cx, cy) {
    const r = this.svg.getBoundingClientRect();
    return [(cx - r.left - this.view.tx) / this.view.s, (cy - r.top - this.view.ty) / this.view.s];
  }
  zoomAt(cx, cy, factor) {
    const r = this.svg.getBoundingClientRect();
    const px = cx - r.left, py = cy - r.top;
    const s = Math.max(0.05, Math.min(12, this.view.s * factor));
    const f = s / this.view.s;
    this.view.tx = px - (px - this.view.tx) * f;
    this.view.ty = py - (py - this.view.ty) * f;
    this.view.s = s;
    this._applyView();
  }
  zoom(factor) {
    const r = this.svg.getBoundingClientRect();
    this.zoomAt(r.left + r.width / 2, r.top + r.height / 2, factor);
  }
  bounds() {
    const sc = this.scene;
    if (!sc) return null;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    const add = (x, y) => { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); };
    for (const c of sc.comps) { add(c.x + c.bb[0], c.y + c.bb[1]); add(c.x + c.bb[2], c.y + c.bb[3]); }
    for (const w of sc.wires) { add(w[0], w[1]); add(w[2], w[3]); }
    for (const f of sc.flags) { add(f.x - 16, f.y - 24); add(f.x + 16, f.y + 24); }
    for (const t of sc.texts) {
      const e = this.items.get("text:" + t.i);
      try { const b = e.getBBox(); add(t.x + b.x, t.y + b.y); add(t.x + b.x + b.width, t.y + b.y + b.height); }
      catch (err) { add(t.x, t.y); }
    }
    return x0 === Infinity ? null : [x0, y0, x1, y1];
  }
  fit() {
    const b = this.bounds();
    const full = this.svg.getBoundingClientRect();
    // Zona visible: sin lo que tape el panel de resultados
    const ins = this.hooks.insets?.() || { right: 0, bottom: 0 };
    const r = { width: full.width - ins.right, height: full.height - ins.bottom };
    if (!b || !r.width) { this.view = { s: 1, tx: r.width / 2, ty: r.height / 2 }; this._applyView(); return; }
    const pad = 40;
    const w = Math.max(b[2] - b[0], 64), h = Math.max(b[3] - b[1], 64);
    const s = Math.min(2, Math.min((r.width - 2 * pad) / w, (r.height - 2 * pad) / h));
    this.view.s = s;
    this.view.tx = r.width / 2 - s * (b[0] + b[2]) / 2;
    this.view.ty = r.height / 2 - s * (b[1] + b[3]) / 2;
    this._applyView();
  }

  // ======================================================== dibujo
  setScene(scene) {
    this.scene = scene;
    if (scene.selected) this.selection = scene.selected;
    if (this.selection && !this._exists(this.selection)) this.selection = null;
    this.render();
    this.hooks.onSelect?.(this.selection);
  }
  _exists([k, i]) {
    const sc = this.scene;
    const list = { comp: sc.comps, wire: sc.wires, flag: sc.flags, text: sc.texts }[k];
    return list && i < list.length;
  }

  render() {
    const sc = this.scene;
    const L = this.layer;
    L.innerHTML = "";
    this.items.clear();
    const gG = el("g", { class: "graphics" }, L);
    sc.graphics.forEach(([d, st]) => el("path", { d, class: "gfx st" + st }, gG));
    const gW = el("g", { class: "wires" }, L);
    sc.wires.forEach((w, i) => {
      const e = el("line", { x1: w[0], y1: w[1], x2: w[2], y2: w[3], class: "wire" }, gW);
      this.items.set("wire:" + i, e);
    });
    const gC = el("g", { class: "comps" }, L);
    sc.comps.forEach((c) => this.items.set("comp:" + c.i, this._drawComp(gC, c)));
    const gF = el("g", { class: "flags" }, L);
    sc.flags.forEach((f) => this.items.set("flag:" + f.i, this._drawFlag(gF, f)));
    const gT = el("g", { class: "texts" }, L);
    sc.texts.forEach((t) => {
      let just = t.just, vert = false;
      if (just.startsWith("V")) { vert = true; just = just.slice(1); }
      if (!["Left", "Right", "Center", "Top", "Bottom"].includes(just)) just = "Left";
      const g = el("g", { transform: `translate(${t.x} ${t.y})`, class: t.directive ? "directive" : "comment" }, gT);
      drawText(g, t.text, 0, 0, just, t.size, vert, "");
      this.items.set("text:" + t.i, g);
    });
    const gJ = el("g", { class: "junctions" }, L);
    sc.junctions.forEach(([x, y]) => el("rect", { x: x - 4.5, y: y - 4.5, width: 9, height: 9, class: "junction" }, gJ));
    sc.openpins.forEach(([x, y]) => el("rect", { x: x - 3.5, y: y - 3.5, width: 7, height: 7, class: "openpin" }, gJ));
    this._drawSelection();
    this._drawOverlay();
  }

  _drawComp(parent, c, cls = "comp") {
    const g = el("g", { class: cls + (c.missing ? " missing" : ""), transform: `translate(${c.x} ${c.y})` }, parent);
    for (const [d, st, fill] of c.shapes) {
      if (fill) el("path", { d, class: "boxfill" }, g);
    }
    for (const [d, st] of c.shapes) el("path", { d, class: "shape st" + st }, g);
    for (const [t, x, y, just, size, vert, kind] of c.texts) {
      drawText(g, t, x, y, just, size, vert, kind === "symtext" ? "symtext" : kind === "pin" ? "pintext" : "attr");
    }
    return g;
  }

  _drawFlag(parent, f) {
    const g = el("g", { class: "flag", transform: `translate(${f.x} ${f.y})` }, parent);
    if (f.name === "0") {
      el("path", { d: "M-16 0L16 0L0 16Z", class: "shape" }, g);
      return g;
    }
    const d = f.dir;
    const off = f.io ? 28 : 6;
    const just = { up: "Bottom", down: "Top", left: "Right", right: "Left" }[d];
    const [x, y] = { up: [0, -off], down: [0, off], left: [-off, 0], right: [off, 0] }[d];
    if (f.io) {
      const k = f.io.toLowerCase();
      const pts = k === "in" ? [[0, 0], [-8, -8], [-8, -24], [8, -24], [8, -8], [0, 0]]
        : k === "out" ? [[0, -24], [-8, -16], [-8, 0], [8, 0], [8, -16], [0, -24]]
          : [[0, 0], [-8, -8], [-8, -16], [0, -24], [8, -16], [8, -8], [0, 0]];
      const rot = { up: "R0", right: "R90", down: "R180", left: "R270" }[d];
      el("path", { d: "M" + pts.map(([a, b]) => xform(rot, a, b).join(" ")).join("L"), class: "shape" }, g);
    }
    el("rect", { x: -2, y: -2, width: 4, height: 4, class: "flagpt" }, g);
    drawText(g, f.name, x, y, just, 2, false, "flagtext");
    return g;
  }

  _drawSelection() {
    for (const e of this.items.values()) e.classList.remove("sel");
    if (this.selection) {
      const e = this.items.get(this.selection.join(":"));
      if (e) e.classList.add("sel");
    }
  }

  _drawOverlay() {
    const O = this.overlay;
    O.innerHTML = "";
    if (this.mode === "wire" && this.wireStart) {
      const [x, y] = this.wireStart;
      el("circle", { cx: x, cy: y, r: 7, class: "wirestart" }, O);
      if (this.rubber) {
        for (const s of this.route(this.wireStart, this.rubber)) {
          el("line", { x1: s[0], y1: s[1], x2: s[2], y2: s[3], class: "rubber" }, O);
        }
      }
    }
    if (this.ghost && this.mode === "place") {
      const g = this.ghost;
      if (g.comp) this._drawComp(O, g.comp, "comp ghost");
      else if (g.flag) this._drawFlag(O, g.flag).classList.add("ghost");
    }
  }

  // ======================================================== modos
  setMode(mode, placing = null) {
    this.mode = mode;
    this.placing = placing;
    this.wireStart = null;
    this.rubber = null;
    this.ghost = null;
    this.svg.dataset.mode = mode;
    this._drawOverlay();
  }

  // ======================================================== geometría
  connectionPoints() {
    const pts = [];
    const sc = this.scene;
    if (!sc) return pts;
    for (const c of sc.comps) for (const [x, y] of c.pins) pts.push([c.x + x, c.y + y]);
    for (const w of sc.wires) { pts.push([w[0], w[1]]); pts.push([w[2], w[3]]); }
    for (const f of sc.flags) pts.push([f.x, f.y]);
    return pts;
  }
  /* Punto de rejilla; si hay un pin o extremo de cable cerca, ese punto. */
  snapPoint(wx, wy) {
    const tol = 22 / this.view.s;
    let best = null, bd = tol;
    for (const [x, y] of this.connectionPoints()) {
      const d = Math.hypot(x - wx, y - wy);
      if (d < bd) { bd = d; best = [x, y]; }
    }
    return best || [snap(wx), snap(wy)];
  }
  onWire(x, y) {
    return this.scene.wires.some((w) => segDist(x, y, w[0], w[1], w[2], w[3]) < 0.5);
  }
  route(a, b) {
    const [x0, y0] = a, [x1, y1] = b;
    if (x0 === x1 || y0 === y1) return [[x0, y0, x1, y1]];
    if (Math.abs(x1 - x0) >= Math.abs(y1 - y0)) return [[x0, y0, x1, y0], [x1, y0, x1, y1]];
    return [[x0, y0, x0, y1], [x0, y1, x1, y1]];
  }

  hitTest(wx, wy) {
    const sc = this.scene;
    if (!sc) return null;
    const tol = 16 / this.view.s;
    const inBox = (b, x, y, pad) => x >= b[0] - pad && x <= b[2] + pad && y >= b[1] - pad && y <= b[3] + pad;
    const elBox = (key, ox, oy) => {
      const e = this.items.get(key);
      if (!e) return null;
      try { const b = e.getBBox(); return [ox + b.x, oy + b.y, ox + b.x + b.width, oy + b.y + b.height]; }
      catch (err) { return null; }
    };
    // 1) etiquetas y tierras (pequeñas, encima de todo)
    for (const f of sc.flags) {
      if (Math.hypot(f.x - wx, f.y - wy) < tol) return ["flag", f.i];
      const b = elBox("flag:" + f.i, f.x, f.y);
      if (b && inBox(b, wx, wy, tol / 3)) return ["flag", f.i];
    }
    // 2) cuerpo de los componentes (el más pequeño gana)
    let best = null, area = Infinity;
    for (const c of sc.comps) {
      const b = [c.x + c.bb[0], c.y + c.bb[1], c.x + c.bb[2], c.y + c.bb[3]];
      if (inBox(b, wx, wy, tol / 2)) {
        const a = (b[2] - b[0] + 1) * (b[3] - b[1] + 1);
        if (a < area) { area = a; best = ["comp", c.i]; }
      }
    }
    // 3) cables (si el toque cae justo en un cable, gana sobre una caja grande)
    let wbest = null, wd = tol;
    sc.wires.forEach((w, i) => {
      const d = segDist(wx, wy, w[0], w[1], w[2], w[3]);
      if (d < wd) { wd = d; wbest = ["wire", i]; }
    });
    if (wbest && (!best || area > 64 * 64 || wd < tol / 3)) return wbest;
    if (best) return best;
    // 4) textos y directivas
    for (const t of sc.texts) {
      const b = elBox("text:" + t.i, t.x, t.y);
      if (b && inBox(b, wx, wy, tol / 3)) return ["text", t.i];
    }
    // 5) textos de los componentes (nombre, valor)
    for (const c of sc.comps) {
      const b = elBox("comp:" + c.i, c.x, c.y);
      if (b && inBox(b, wx, wy, 0)) return ["comp", c.i];
    }
    return null;
  }

  // ======================================================== gestos
  _bind() {
    const svg = this.svg;
    svg.addEventListener("pointerdown", (e) => this._down(e));
    svg.addEventListener("pointermove", (e) => this._move(e));
    svg.addEventListener("pointerup", (e) => this._up(e));
    svg.addEventListener("pointercancel", (e) => this._up(e, true));
    svg.addEventListener("wheel", (e) => {
      e.preventDefault();
      this.zoomAt(e.clientX, e.clientY, Math.exp(-e.deltaY * (e.deltaMode ? 0.05 : 0.0015)));
    }, { passive: false });
    svg.addEventListener("dblclick", (e) => {
      const [wx, wy] = this.toWorld(e.clientX, e.clientY);
      const hit = this.hitTest(wx, wy);
      if (hit && this.mode === "select") this.hooks.onEdit?.(hit);
    });
    svg.addEventListener("contextmenu", (e) => e.preventDefault());
  }

  _down(e) {
    try { this.svg.setPointerCapture(e.pointerId); } catch (err) { /* puntero sintético */ }
    this.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (this.pointers.size === 2) {
      this._cancelGesture();
      const [a, b] = [...this.pointers.values()];
      this.pinch = { d: Math.hypot(a.x - b.x, a.y - b.y) || 1, mx: (a.x + b.x) / 2, my: (a.y + b.y) / 2,
        view: { ...this.view } };
      return;
    }
    if (this.pointers.size > 2) return;
    if (e.pointerType === "mouse" && e.button !== 0) return;
    const g = { id: e.pointerId, x: e.clientX, y: e.clientY, moved: false, kind: null, done: false,
      mouse: e.pointerType === "mouse" };
    this.gesture = g;
    g.timer = setTimeout(() => {
      if (this.gesture === g && !g.moved) {
        g.done = true;
        navigator.vibrate?.(15);
        this._longPress(g);
      }
    }, LONG_PRESS_MS);
  }

  _move(e) {
    if (this.pointers.has(e.pointerId)) this.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (this.pinch && this.pointers.size >= 2) {
      const [a, b] = [...this.pointers.values()];
      const d = Math.hypot(a.x - b.x, a.y - b.y) || 1;
      const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
      const p = this.pinch;
      const r = this.svg.getBoundingClientRect();
      const s = Math.max(0.05, Math.min(12, p.view.s * d / p.d));
      // el punto del mundo bajo el centro inicial queda bajo el centro actual
      const wx = (p.mx - r.left - p.view.tx) / p.view.s, wy = (p.my - r.top - p.view.ty) / p.view.s;
      this.view = { s, tx: mx - r.left - wx * s, ty: my - r.top - wy * s };
      this._applyView();
      return;
    }
    const g = this.gesture;
    if (!g || g.id !== e.pointerId) {
      if (!g && e.pointerType === "mouse" && this.mode === "place") this._hover(e);
      if (!g && e.pointerType === "mouse" && this.mode === "wire" && this.wireStart) {
        const [wx, wy] = this.toWorld(e.clientX, e.clientY);
        this.rubber = this.snapPoint(wx, wy);
        this._drawOverlay();
      }
      return;
    }
    if (g.done) return;
    const dist = Math.hypot(e.clientX - g.x, e.clientY - g.y);
    if (!g.moved && dist < MOVE_PX) return;
    if (!g.moved) {
      g.moved = true;
      clearTimeout(g.timer);
      this._startDrag(g);
    }
    this._drag(g, e.clientX, e.clientY);
  }

  _up(e, cancel = false) {
    this.pointers.delete(e.pointerId);
    if (this.pinch) {
      if (this.pointers.size < 2) this.pinch = null;
      return;
    }
    const g = this.gesture;
    if (!g || g.id !== e.pointerId) return;
    clearTimeout(g.timer);
    this.gesture = null;
    if (cancel || g.done) { if (g.kind) this._endDrag(g, true); return; }
    if (g.moved) this._endDrag(g, false);
    else this._tap(g);
  }

  _cancelGesture() {
    const g = this.gesture;
    if (!g) return;
    clearTimeout(g.timer);
    if (g.kind) this._endDrag(g, true);
    this.gesture = null;
  }

  _hover(e) {
    const [wx, wy] = this.toWorld(e.clientX, e.clientY);
    this.ghost = this._ghostAt(wx, wy);
    this._drawOverlay();
  }
  _ghostAt(wx, wy) {
    const p = this.placing;
    if (!p) return null;
    if (p.type === "comp" && p.preview) {
      const [x, y] = this._compOrigin(p, wx, wy);
      return { comp: { ...p.preview, x, y } };
    }
    if (p.type === "flag") {
      const [x, y] = this.snapPoint(wx, wy);
      return { flag: { x, y, name: p.name, dir: "up", io: null } };
    }
    return null;
  }
  /* Origen del componente para que su centro quede bajo el dedo. */
  _compOrigin(p, wx, wy) {
    const bb = p.preview.bb;
    return [snap(wx - (bb[0] + bb[2]) / 2), snap(wy - (bb[1] + bb[3]) / 2)];
  }

  // ------------------------------------------------------------ toque
  async _tap(g) {
    const [wx, wy] = this.toWorld(g.x, g.y);
    const hit = this.hitTest(wx, wy);
    if (this.mode === "select") {
      this.selection = hit;
      this._drawSelection();
      this.hooks.onSelect?.(hit);
    } else if (this.mode === "delete") {
      if (hit) await this.hooks.op("delete", { items: [hit] });
    } else if (this.mode === "probe") {
      if (hit) this.hooks.onProbe?.(hit);
    } else if (this.mode === "wire") {
      await this._wireTap(this.snapPoint(wx, wy));
    } else if (this.mode === "place") {
      await this._place(wx, wy);
    }
  }

  async _wireTap(p) {
    if (!this.wireStart) {
      this.wireStart = p;
      this.rubber = null;
      this._drawOverlay();
      this.hooks.onStatus?.("Toque el punto final del cable (toque dos veces el mismo punto para terminar).");
      return;
    }
    const a = this.wireStart;
    if (p[0] === a[0] && p[1] === a[1]) {
      this.wireStart = null;
      this.rubber = null;
      this._drawOverlay();
      this.hooks.onStatus?.("Toque donde empieza el cable.");
      return;
    }
    const hitExisting = this.connectionPoints().some(([x, y]) => x === p[0] && y === p[1]) ||
      this.onWire(p[0], p[1]);
    this.wireStart = hitExisting ? null : p;
    this.rubber = null;
    await this.hooks.op("add_wires", { segs: this.route(a, p) });
    this._drawOverlay();
    this.hooks.onStatus?.(this.wireStart ? "Siga tocando para continuar el cable, o toque el mismo punto para terminar."
      : "Cable conectado. Toque donde empieza el siguiente.");
  }

  async _place(wx, wy) {
    const p = this.placing;
    if (!p) return;
    if (p.type === "comp") {
      const [x, y] = p.preview ? this._compOrigin(p, wx, wy) : [snap(wx), snap(wy)];
      await this.hooks.op("add_comp", { symbol: p.symbol, x, y, orient: p.orient, attrs: p.attrs || null });
    } else if (p.type === "flag") {
      const [x, y] = this.snapPoint(wx, wy);
      await this.hooks.op("add_flag", { x, y, name: p.name });
    } else if (p.type === "text") {
      await this.hooks.op("add_text", { x: snap(wx), y: snap(wy), text: p.text, directive: p.directive });
      this.hooks.onPlaced?.(p);
    }
  }

  _longPress(g) {
    const [wx, wy] = this.toWorld(g.x, g.y);
    if (this.mode === "wire") {
      this.wireStart = null;
      this.rubber = null;
      this._drawOverlay();
      return;
    }
    const hit = this.hitTest(wx, wy);
    if (hit) {
      this.selection = hit;
      this._drawSelection();
      this.hooks.onSelect?.(hit);
      this.hooks.onEdit?.(hit);
    }
  }

  // ------------------------------------------------------------ arrastre
  _startDrag(g) {
    const [wx, wy] = this.toWorld(g.x, g.y);
    g.w0 = [wx, wy];
    if (this.mode === "wire") {
      g.kind = "wire";
      g.a = this.wireStart || this.snapPoint(wx, wy);
      return;
    }
    if (this.mode === "select") {
      const sel = this.selection;
      // extremo de un cable seleccionado
      if (sel && sel[0] === "wire") {
        const w = this.scene.wires[sel[1]];
        const tol = 20 / this.view.s;
        for (const end of [1, 2]) {
          const [x, y] = end === 1 ? [w[0], w[1]] : [w[2], w[3]];
          if (Math.hypot(x - wx, y - wy) < tol) { g.kind = "wireend"; g.end = end; g.wire = sel[1]; return; }
        }
      }
      const hit = this.hitTest(wx, wy);
      if (hit && hit[0] !== "graphic") {
        this.selection = hit;
        this._drawSelection();
        this.hooks.onSelect?.(hit);
        g.kind = "move";
        g.item = hit;
        g.stretch = this._stretchFor(hit);
        return;
      }
    }
    g.kind = "pan";
    g.view0 = { ...this.view };
  }

  _stretchFor([k, i]) {
    const sc = this.scene;
    const moved = new Set();
    if (k === "comp") { const c = sc.comps[i]; c.pins.forEach(([x, y]) => moved.add(`${c.x + x},${c.y + y}`)); }
    else if (k === "wire") { const w = sc.wires[i]; moved.add(`${w[0]},${w[1]}`); moved.add(`${w[2]},${w[3]}`); }
    else if (k === "flag") { const f = sc.flags[i]; moved.add(`${f.x},${f.y}`); }
    const out = [];
    sc.wires.forEach((w, j) => {
      if (k === "wire" && j === i) return;
      const e1 = moved.has(`${w[0]},${w[1]}`), e2 = moved.has(`${w[2]},${w[3]}`);
      if (e1 || e2) out.push([j, e1, e2]);
    });
    return out;
  }

  _drag(g, cx, cy) {
    const [wx, wy] = this.toWorld(cx, cy);
    if (g.kind === "pan") {
      this.view.tx = g.view0.tx + (cx - g.x);
      this.view.ty = g.view0.ty + (cy - g.y);
      this._applyView();
    } else if (g.kind === "wire") {
      this.wireStart = g.a;
      this.rubber = this.snapPoint(wx, wy);
      this._drawOverlay();
    } else if (g.kind === "move") {
      const dx = snap(wx - g.w0[0]), dy = snap(wy - g.w0[1]);
      g.d = [dx, dy];
      const [k, i] = g.item;
      const e = this.items.get(k + ":" + i);
      if (k === "wire") {
        const w = this.scene.wires[i];
        e.setAttribute("x1", w[0] + dx); e.setAttribute("y1", w[1] + dy);
        e.setAttribute("x2", w[2] + dx); e.setAttribute("y2", w[3] + dy);
      } else {
        const o = k === "comp" ? this.scene.comps[i] : k === "flag" ? this.scene.flags[i] : this.scene.texts[i];
        e.setAttribute("transform", `translate(${o.x + dx} ${o.y + dy})`);
      }
      for (const [j, e1, e2] of g.stretch) {
        const w = this.scene.wires[j], le = this.items.get("wire:" + j);
        le.setAttribute("x1", w[0] + (e1 ? dx : 0)); le.setAttribute("y1", w[1] + (e1 ? dy : 0));
        le.setAttribute("x2", w[2] + (e2 ? dx : 0)); le.setAttribute("y2", w[3] + (e2 ? dy : 0));
      }
    } else if (g.kind === "wireend") {
      const [x, y] = this.snapPoint(wx, wy);
      g.p = [x, y];
      const le = this.items.get("wire:" + g.wire);
      le.setAttribute(g.end === 1 ? "x1" : "x2", x);
      le.setAttribute(g.end === 1 ? "y1" : "y2", y);
    }
  }

  async _endDrag(g, cancelled) {
    if (g.kind === "pan") return;
    if (g.kind === "wire") {
      const a = g.a, b = this.rubber;
      this.rubber = null;
      if (cancelled || !b || (a[0] === b[0] && a[1] === b[1])) { this.wireStart = null; this._drawOverlay(); return; }
      this.wireStart = null;
      await this.hooks.op("add_wires", { segs: this.route(a, b) });
      return;
    }
    if (cancelled) { this.render(); return; }
    if (g.kind === "move") {
      const [dx, dy] = g.d || [0, 0];
      if (dx || dy) await this.hooks.op("move", { items: [g.item], dx, dy, stretch: true });
      else this.render();
    } else if (g.kind === "wireend") {
      if (g.p) await this.hooks.op("move_wire_end", { i: g.wire, end: g.end, x: g.p[0], y: g.p[1] });
    }
  }
}
