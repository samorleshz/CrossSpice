/* Worker de ngspice 45 (WebAssembly, paquete eecircuit-engine).
   Recibe {id, op:"run", args:{text}} y responde con los vectores como
   Float64Array (transferibles). Si ngspice termina sin escribir resultados
   (error en la netlist) el motor queda esperando: se detecta y se avisa; la
   interfaz entonces descarta este worker y crea otro. */
import { Simulation } from "./vendor/eecircuit/eecircuit-engine.mjs";

let sim = null;
let onIdle = null;

async function start() {
  if (sim) return;
  sim = new Simulation();
  const wait = sim.waitForNextRun;
  sim.waitForNextRun = () => {
    const ok = sim.results && sim.results.data;
    if (onIdle && !ok) onIdle("failed");
    return wait();
  };
  await sim.start();
}

function relevantInfo() {
  const info = (sim.getInfo() || "").split("\n");
  const errs = sim.getError() || [];
  const pick = info.concat(errs).filter((l) =>
    /error|unknown|undefined|unable|no such|singular|too small|could not|not found|failed/i.test(l));
  return [...new Set(pick.map((s) => s.trim()).filter(Boolean))].slice(0, 8);
}

async function run(text) {
  await start();
  sim.setNetList(text);
  if (!/^\s*\.noise\b/im.test(text)) {
    // "destroy all": si la corrida falla, que no se escriban los vectores
    // de la corrida anterior como si fueran los nuevos.
    sim.commandList = [" ", "set ngbehavior=ltpsa", "source test.cir", "destroy all", "run",
                       "write out.raw"];
  }
  try {
    const FS = sim.__getSpiceModuleForTests()?.FS;
    try { FS?.unlink("out.raw"); } catch (e) { /* no existía */ }
  } catch (e) { /* sin acceso al FS */ }
  // Si el motor vuelve a esperar la siguiente corrida sin haber leído
  // resultados, ngspice falló (si no, se quedaría esperando para siempre).
  const failed = new Promise((resolve) => { onIdle = resolve; });
  const pending = sim.runSim();
  const res = await Promise.race([pending, failed]);
  onIdle = null;
  if (res === "failed") {
    const why = relevantInfo();
    const err = new Error(why.length ? why.join("\n") : "ngspice no produjo resultados.");
    err.dead = true;
    throw err;
  }
  const info = (sim.getInfo() || "") + "\n" + (sim.getError() || []).join("\n");
  if (/simulation interrupted due to error|run simulation\(s\) aborted/i.test(info)) {
    throw new Error(relevantInfo().join("\n") || "ngspice interrumpió la simulación por un error.");
  }
  return res;
}

function pack(res) {
  const vectors = [];
  const transfer = [];
  for (const d of res.data) {
    if (res.dataType === "complex") {
      const re = new Float64Array(d.values.length);
      const im = new Float64Array(d.values.length);
      d.values.forEach((v, i) => { re[i] = v.real; im[i] = v.img; });
      vectors.push({ name: d.name, type: d.type, re, im });
      transfer.push(re.buffer, im.buffer);
    } else {
      const re = Float64Array.from(d.values);
      vectors.push({ name: d.name, type: d.type, re });
      transfer.push(re.buffer);
    }
  }
  return { result: { complex: res.dataType === "complex", header: res.header,
                     points: res.numPoints, vectors,
                     warnings: relevantInfo() }, transfer };
}

self.onmessage = async (ev) => {
  const { id, op, args } = ev.data;
  try {
    if (op === "init") {
      const t0 = performance.now();
      await start();
      const line = (sim.getInitInfo() || "").split("\n").find((l) => /ngspice/i.test(l)) || "";
      postMessage({ id, ok: true, result: { seconds: (performance.now() - t0) / 1000,
                                            version: line.trim() } });
    } else if (op === "run") {
      const t0 = performance.now();
      const res = await run(args.text);
      const { result, transfer } = pack(res);
      result.seconds = (performance.now() - t0) / 1000;
      postMessage({ id, ok: true, result }, transfer);
    }
  } catch (e) {
    postMessage({ id, ok: false, error: String(e.message || e), dead: !!e.dead });
  }
};
