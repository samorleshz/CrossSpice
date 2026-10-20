/* Graficador de formas de onda (canvas), al estilo del visor de LTspice.
   - .tran / .dc: eje x lineal; .ac: frecuencia logarítmica, magnitud en dB
     (línea continua) y fase en grados (punteada, eje derecho).
   - Tocar: cursor con los valores. Pellizcar o rueda: zoom en x.
     Arrastrar: desplazar en x. Doble toque: ver todo.
   - Varias corridas (.step) se dibujan con el mismo color. */

export const TRACE_COLORS = ["#008300", "#0000ff", "#ff0000", "#00aeaf", "#ff00ff", "#808080",
  "#008000", "#0000af", "#af8000", "#800080", "#800000", "#afaf00"];

const PREFIX = [[1e12, "T"], [1e9, "G"], [1e6, "M"], [1e3, "k"], [1, ""], [1e-3, "m"],
  [1e-6, "µ"], [1e-9, "n"], [1e-12, "p"], [1e-15, "f"]];

export function si(v, unit = "", digits = 4) {
  if (!isFinite(v)) return String(v);
  if (v === 0) return "0" + unit;
  const a = Math.abs(v);
  for (const [f, p] of PREFIX) {
    if (a >= f * 0.9995) return +(v / f).toPrecision(digits) + p + unit;
  }
  return v.toExponential(digits - 1) + unit;
}

function niceStep(span, n) {
  const raw = span / n;
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  const m = raw / p;
  return (m < 1.5 ? 1 : m < 3.5 ? 2 : m < 7.5 ? 5 : 10) * p;
}

export class Plot {
  constructor(canvas, hooks = {}) {
    this.canvas = canvas;
    this.hooks = hooks;    // {onLegend(traceIndex)}
    this.ctx = canvas.getContext("2d");
    this.runs = [];        // [{label, x: Float64Array, complex}]
    this.kind = "tran";
    this.traces = [];      // [{label, unit, color, ys: [run -> {re, im}]}]
    this.xr = null;        // [x0, x1] (en log10 si ac)
    this.cursor = null;
    this.pointers = new Map();
    this._bind();
    new ResizeObserver(() => this.draw()).observe(canvas);
  }

  setRuns(runs, kind) {
    this.runs = runs;
    this.kind = kind;
    this.traces = [];
    this.xr = null;
    this.cursor = null;
    this.draw();
  }

  addTrace(label, unit, perRun) {
    const old = this.traces.findIndex((t) => t.label === label);
    if (old >= 0) return false;
    const used = new Set(this.traces.map((t) => t.color));
    const color = TRACE_COLORS.find((c) => !used.has(c)) || TRACE_COLORS[this.traces.length % TRACE_COLORS.length];
    this.traces.push({ label, unit, color, ys: perRun });
    this.draw();
    return true;
  }
  removeTrace(i) { this.traces.splice(i, 1); this.draw(); }
  clear() { this.traces = []; this.draw(); }

  get isAC() { return this.kind === "ac"; }
  _tx(x) { return this.isAC ? Math.log10(Math.max(x, 1e-30)) : x; }

  _xFull() {
    let a = Infinity, b = -Infinity;
    for (const r of this.runs) {
      if (!r.x.length) continue;
      a = Math.min(a, this._tx(r.x[0]), this._tx(r.x[r.x.length - 1]));
      b = Math.max(b, this._tx(r.x[0]), this._tx(r.x[r.x.length - 1]));
    }
    if (!isFinite(a)) return [0, 1];
    return a === b ? [a - 1, b + 1] : [a, b];
  }

  _yValue(t, run, i) {
    const d = t.ys[run];
    if (!d) return NaN;
    if (this.isAC) {
      const re = d.re[i], im = d.im ? d.im[i] : 0;
      return 20 * Math.log10(Math.max(Math.hypot(re, im), 1e-30));
    }
    return d.re[i];
  }
  _phase(t, run, i) {
    const d = t.ys[run];
    if (!d) return NaN;
    return Math.atan2(d.im ? d.im[i] : 0, d.re[i]) * 180 / Math.PI;
  }

  draw() {
    const c = this.canvas, dpr = window.devicePixelRatio || 1;
    const W = c.clientWidth, H = c.clientHeight;
    if (!W || !H) return;
    if (c.width !== Math.round(W * dpr) || c.height !== Math.round(H * dpr)) {
      c.width = Math.round(W * dpr); c.height = Math.round(H * dpr);
    }
    const g = this.ctx;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    const css = getComputedStyle(c);
    const bg = css.getPropertyValue("--wave-bg").trim() || "#fff";
    const fg = css.getPropertyValue("--wave-fg").trim() || "#000";
    const grid = css.getPropertyValue("--wave-grid").trim() || "#999";
    g.fillStyle = bg; g.fillRect(0, 0, W, H);
    // Leyenda (en varias filas si hace falta); con cursor muestra los valores
    g.font = "bold 14px system-ui, sans-serif";
    this.legend = [];
    let lx = 62, row = 0;
    const items = this.traces.map((t, i) => {
      let label = t.label;
      if (this.cursor) { const v = this._valueAt(t, this.cursor.x); if (v) label += " = " + v; }
      const w = g.measureText(label).width + 22;
      if (lx > 62 && lx + w > W - 8) { lx = 62; row++; }
      const it = { t, i, label, x: lx, y: 15 + row * 22, w };
      lx += w + 12;
      return it;
    });
    const legendH = this.traces.length ? 30 + row * 22 : 0;
    const ac = this.isAC;
    const unitsR = !ac && this.traces.some((t) => t.unit !== this.traces[0].unit);
    const L = 62, R = ac || unitsR ? 62 : 16, T = 10 + legendH, B = 30;
    this.box = { L, R, T, B, W, H, legendH };
    const pw = W - L - R, ph = H - T - B;
    g.font = "13px system-ui, sans-serif";
    if (!this.runs.length) return;
    if (!this.xr) this.xr = this._xFull();
    const [x0, x1] = this.xr;
    // Rango y de lo visible. Como en LTspice, si hay trazas de otra unidad
    // (p. ej. V y A) van en un segundo eje a la derecha.
    const unitL = ac ? "dB" : (this.traces[0]?.unit || "");
    const unitR = ac ? null : (this.traces.find((t) => t.unit !== unitL)?.unit ?? null);
    const range = (pick, val) => {
      let lo = Infinity, hi = -Infinity;
      for (const t of this.traces) {
        if (!pick(t)) continue;
        this.runs.forEach((r, k) => {
          for (let i = 0; i < r.x.length; i++) {
            const xv = this._tx(r.x[i]);
            if (xv < x0 || xv > x1) continue;
            const y = val(t, k, i);
            if (isFinite(y)) { if (y < lo) lo = y; if (y > hi) hi = y; }
          }
        });
      }
      if (!isFinite(lo)) { lo = -1; hi = 1; }
      if (hi - lo < 1e-12 * Math.max(1, Math.abs(hi))) { const m = Math.abs(lo) * 0.1 || 1; lo -= m; hi += m; }
      const st = niceStep(hi - lo, Math.max(3, Math.floor(ph / 45)));
      return [Math.floor(lo / st) * st, Math.ceil(hi / st) * st, st];
    };
    const mag = (t, k, i) => this._yValue(t, k, i);
    const [y0, y1, ys] = range((t) => ac || t.unit === unitL, mag);
    const yrR = unitR !== null ? range((t) => t.unit === unitR, mag) : null;
    let p0 = 0, p1 = 0;
    if (ac) {
      [p0, p1] = range(() => true, (t, k, i) => this._phase(t, k, i));
      p0 = Math.floor(p0 / 30) * 30; p1 = Math.ceil(p1 / 30) * 30; if (p1 === p0) p1 = p0 + 30;
    }
    this.yr = [y0, y1];
    const X = (v) => L + (v - x0) / (x1 - x0) * pw;
    const Y = (v) => T + (1 - (v - y0) / (y1 - y0)) * ph;
    const YR = yrR ? (v) => T + (1 - (v - yrR[0]) / (yrR[1] - yrR[0])) * ph : Y;
    const YP = (v) => T + (1 - (v - p0) / (p1 - p0)) * ph;
    this.X = X;
    // rejilla y ejes
    g.strokeStyle = grid; g.lineWidth = 1; g.setLineDash([2, 3]);
    g.fillStyle = fg; g.textAlign = "right"; g.textBaseline = "middle";
    const ticks = (lo, hi, st, fn) => {
      for (let k = 0; lo + k * st <= hi + st * 0.01; k++) {
        let v = lo + k * st;
        if (Math.abs(v) < st * 1e-6) v = 0;
        fn(v);
      }
    };
    ticks(y0, y1, ys, (v) => {
      const y = Y(v);
      g.beginPath(); g.moveTo(L, y); g.lineTo(L + pw, y); g.stroke();
      g.fillText(ac ? +v.toPrecision(4) + "dB" : si(v, unitL, 3), L - 6, y);
    });
    g.textAlign = "left";
    if (ac) {
      const st = [15, 30, 45, 90, 180].find((v) => (p1 - p0) / v <= 8) || 180;
      for (let v = Math.ceil(p0 / st) * st; v <= p1 + 0.01; v += st) {
        g.fillText(Math.round(v) + "°", L + pw + 6, YP(v));
      }
    } else if (yrR) {
      ticks(yrR[0], yrR[1], yrR[2], (v) => g.fillText(si(v, unitR, 3), L + pw + 6, YR(v)));
    }
    g.textAlign = "center"; g.textBaseline = "top";
    if (ac) {
      for (let dec = Math.floor(x0); dec <= Math.ceil(x1); dec++) {
        for (let m = 1; m < 10; m++) {
          const v = dec + Math.log10(m);
          if (v < x0 || v > x1) continue;
          const x = X(v);
          g.strokeStyle = grid; g.globalAlpha = m === 1 ? 1 : 0.35;
          g.beginPath(); g.moveTo(x, T); g.lineTo(x, T + ph); g.stroke();
          g.globalAlpha = 1;
          if (m === 1 || (x1 - x0 < 1.3 && (x1 - x0 < 0.6 || m % 2 === 0)))
            g.fillText(si(m * Math.pow(10, dec), "Hz", 3), x, T + ph + 6);
        }
      }
    } else {
      const xs = niceStep(x1 - x0, Math.max(3, Math.floor(pw / 90)));
      const unitX = this.kind === "tran" ? "s" : this.xUnit || "";
      for (let v = Math.ceil(x0 / xs) * xs; v <= x1 + xs * 1e-6; v += xs) {
        const x = X(v);
        g.beginPath(); g.moveTo(x, T); g.lineTo(x, T + ph); g.stroke();
        g.fillText(si(Math.abs(v) < xs * 1e-6 ? 0 : v, unitX, 3), x, T + ph + 6);
      }
    }
    g.setLineDash([]);
    g.strokeStyle = fg; g.strokeRect(L, T, pw, ph);
    // trazas
    g.save();
    g.beginPath(); g.rect(L, T, pw, ph); g.clip();
    for (const t of this.traces) {
      g.strokeStyle = t.color; g.lineWidth = 1.6;
      for (const pass of ac ? ["mag", "phase"] : ["mag"]) {
        g.setLineDash(pass === "phase" ? [5, 4] : []);
        this.runs.forEach((r, k) => {
          if (!t.ys[k]) return;
          g.beginPath();
          let pen = false;
          for (let i = 0; i < r.x.length; i++) {
            const xv = this._tx(r.x[i]);
            const yv = pass === "phase" ? YP(this._phase(t, k, i)) : (t.unit === unitR && !ac ? YR : Y)(this._yValue(t, k, i));
            if (!isFinite(yv)) { pen = false; continue; }
            const xp = X(xv);
            if (pen) g.lineTo(xp, yv); else { g.moveTo(xp, yv); pen = true; }
          }
          g.stroke();
        });
      }
    }
    g.restore();
    g.setLineDash([]);
    // leyenda
    g.textAlign = "left"; g.textBaseline = "middle"; g.font = "bold 14px system-ui, sans-serif";
    for (const it of items) {
      g.fillStyle = it.t.color; g.fillRect(it.x, it.y - 7, 14, 14);
      g.fillStyle = fg; g.fillText(it.label, it.x + 18, it.y);
      this.legend.push([it.x, it.x + it.w, it.i, it.y]);
    }
    // cursor
    if (this.cursor) {
      const x = X(this.cursor.x);
      if (x >= L && x <= L + pw) {
        g.strokeStyle = fg; g.setLineDash([4, 3]);
        g.beginPath(); g.moveTo(x, T); g.lineTo(x, T + ph); g.stroke();
        g.setLineDash([]);
        const xv = ac ? Math.pow(10, this.cursor.x) : this.cursor.x;
        const lbl = si(xv, ac ? "Hz" : this.kind === "tran" ? "s" : this.xUnit || "", 4);
        g.font = "13px system-ui, sans-serif";
        const w = g.measureText(lbl).width + 10;
        g.fillStyle = fg; g.fillRect(Math.min(x - w / 2, L + pw - w), T + ph - 20, w, 18);
        g.fillStyle = bg; g.textAlign = "center";
        g.fillText(lbl, Math.min(x, L + pw - w / 2), T + ph - 11);
      }
    }
  }

  _valueAt(t, xv) {
    const out = [];
    this.runs.forEach((r, k) => {
      if (!t.ys[k] || !r.x.length) return;
      const n = r.x.length;
      let lo = 0, hi = n - 1;
      const xs = (i) => this._tx(r.x[i]);
      if (xv <= xs(0)) hi = 0; else if (xv >= xs(n - 1)) lo = hi = n - 1;
      else while (hi - lo > 1) { const m = (lo + hi) >> 1; if (xs(m) <= xv) lo = m; else hi = m; }
      const f = hi === lo ? 0 : (xv - xs(lo)) / (xs(hi) - xs(lo));
      const lerp = (fn) => fn(lo) + f * (fn(hi) - fn(lo));
      if (this.isAC) {
        const db = lerp((i) => this._yValue(t, k, i));
        const ph = lerp((i) => this._phase(t, k, i));
        out.push(`${db.toFixed(2)}dB ∠${ph.toFixed(1)}°`);
      } else {
        out.push(si(lerp((i) => this._yValue(t, k, i)), t.unit, 4));
      }
    });
    if (out.length > 3) return out.slice(0, 3).join(", ") + "…";
    return out.join(", ");
  }

  // ------------------------------------------------------------ gestos
  _bind() {
    const c = this.canvas;
    c.addEventListener("pointerdown", (e) => {
      try { c.setPointerCapture(e.pointerId); } catch (err) { /* puntero sintético */ }
      this.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (this.pointers.size === 2) {
        const [a, b] = [...this.pointers.values()];
        this.pinch = { d: Math.abs(a.x - b.x) || 1, m: (a.x + b.x) / 2, xr: this.xr && [...this.xr] };
        this.g = null;
      } else if (this.pointers.size === 1) {
        this.g = { x: e.clientX, y: e.clientY, xr: this.xr && [...this.xr], moved: false };
      }
    });
    c.addEventListener("pointermove", (e) => {
      if (!this.pointers.has(e.pointerId)) return;
      this.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      const rect = c.getBoundingClientRect();
      if (this.pinch && this.pointers.size >= 2 && this.pinch.xr) {
        const [a, b] = [...this.pointers.values()];
        const d = Math.abs(a.x - b.x) || 1;
        const p = this.pinch, pw = rect.width - this.box.L - this.box.R;
        const span = (p.xr[1] - p.xr[0]) * p.d / d;
        const xm = p.xr[0] + (p.m - rect.left - this.box.L) / pw * (p.xr[1] - p.xr[0]);
        const m = (a.x + b.x) / 2;
        const lo = xm - (m - rect.left - this.box.L) / pw * span;
        this.xr = [lo, lo + span];
        this.draw();
        return;
      }
      const g = this.g;
      if (!g || !g.xr) return;
      if (!g.moved && Math.hypot(e.clientX - g.x, e.clientY - g.y) < 8) return;
      g.moved = true;
      const pw = rect.width - this.box.L - this.box.R;
      const dx = (e.clientX - g.x) / pw * (g.xr[1] - g.xr[0]);
      this.xr = [g.xr[0] - dx, g.xr[1] - dx];
      this.draw();
    });
    const up = (e) => {
      this.pointers.delete(e.pointerId);
      if (this.pinch) { if (this.pointers.size < 2) this.pinch = null; this.g = null; return; }
      const g = this.g;
      this.g = null;
      if (!g || g.moved) return;
      const rect = c.getBoundingClientRect();
      const px = e.clientX - rect.left, py = e.clientY - rect.top;
      const now = performance.now();
      if (this.lastTap && now - this.lastTap < 320) { this.xr = null; this.cursor = null; this.lastTap = 0; this.draw(); return; }
      this.lastTap = now;
      if (py < this.box.T) {
        const hit = (this.legend || []).find(([a, b, , y]) => px >= a && px <= b && Math.abs(py - y) < 12);
        if (hit) { this.hooks.onLegend?.(hit[2]); return; }
      }
      if (!this.xr) return;
      const pw = rect.width - this.box.L - this.box.R;
      const f = (px - this.box.L) / pw;
      if (f < 0 || f > 1) { this.cursor = null; this.draw(); return; }
      this.cursor = { x: this.xr[0] + f * (this.xr[1] - this.xr[0]) };
      this.draw();
    };
    c.addEventListener("pointerup", up);
    c.addEventListener("pointercancel", up);
    c.addEventListener("wheel", (e) => {
      if (!this.xr) return;
      e.preventDefault();
      const rect = c.getBoundingClientRect();
      const pw = rect.width - this.box.L - this.box.R;
      const f = Math.max(0, Math.min(1, (e.clientX - rect.left - this.box.L) / pw));
      const xm = this.xr[0] + f * (this.xr[1] - this.xr[0]);
      const k = Math.exp(e.deltaY * 0.0015);
      this.xr = [xm - (xm - this.xr[0]) * k, xm + (this.xr[1] - xm) * k];
      this.draw();
    }, { passive: false });
  }
}
