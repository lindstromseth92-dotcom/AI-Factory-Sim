/* AI Factory Sim dashboard. No build step, no dependencies. */
"use strict";

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

const S = { catalog: null, snap: null, history: [], sim: { dt_min: 2, paused: false }, hallKey: "", tab: "operate" };

// ---------------------------------------------------------------- formatting

const num = (v, d = 0) => v.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
const mw = (kw, d = 2) => num(kw / 1000, d);
const pct = (v, d = 0) => num(v * 100, d) + "%";
function big(v) {
  if (v >= 1e9) return [num(v / 1e9, 2), "B"];
  if (v >= 1e6) return [num(v / 1e6, v >= 1e8 ? 0 : 1), "M"];
  if (v >= 1e3) return [num(v / 1e3, v >= 1e5 ? 0 : 1), "K"];
  return [num(v, 0), ""];
}
const bigs = (v) => big(v).join(" ").trim();
function usd(v) {
  if (v === null || v === undefined) return "n/a";
  return "$" + (v >= 1 ? num(v, 2) : v >= 0.01 ? num(v, 3) : num(v, 4));
}
const clockOf = (t) => {
  const m = Math.floor(t % 1440);
  return String(Math.floor(m / 60)).padStart(2, "0") + ":" + String(m % 60).padStart(2, "0");
};
const isDark = () => getComputedStyle(document.documentElement).colorScheme.includes("dark");

// ---------------------------------------------------------------- transport

async function post(path, body) {
  const r = await fetch(path, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body || {}) });
  if (!r.ok) console.warn(path, await r.text());
  return r.ok ? r.json() : null;
}

function connect() {
  const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === "hello") {
      S.catalog = m.catalog;
      S.history = m.history;
      S.sim = m.sim;
      buildControls();
      $("log").replaceChildren();
      $("bus").replaceChildren();
      addLog(m.log);
      addBus(m.bus);
      update(m);
    } else if (m.type === "tick") {
      if (m.history) S.history = m.history;
      else S.history.push(point(m.snap));
      if (S.history.length > 600) S.history.shift();
      addLog(m.log);
      addBus(m.bus);
      update(m);
      if (m.rebuilt && S.tab === "compare") loadCompare();
    } else if (m.type === "sim") {
      S.sim = m.sim;
      renderSim();
    }
  };
  ws.onclose = () => setTimeout(connect, 1000);
}

const point = (s) => ({
  t: s.t_min, grid_kw: s.power.grid_kw, limit_kw: s.power.limit_kw, tokens: s.work.tokens_per_s,
  demand: s.work.demand_tokens_per_s, pue: s.power.pue, ambient: s.cooling.ambient_c,
});

function update(m) {
  S.snap = m.snap;
  if (m.sim) S.sim = m.sim;
  renderSim();
  renderBus(m.bus_status);
  syncControls();
  if (S.tab !== "operate") return;
  renderKpis();
  renderStatus();
  renderFlow();
  renderStack();
  renderHall();
  renderCharts();
}

// ---------------------------------------------------------------- controls

const SLIDERS = {
  site_mw: (v) => `${v} MW`,
  supply_c: (v) => `${v} °C`,
  bess_mwh: (v) => (+v === 0 ? "None" : `${v} MWh`),
  price_usd_mwh: (v) => `$${v}/MWh`,
  oversubscription: (v) => `${(+v).toFixed(2)}×`,
  inference_share: (v) => pct(+v),
  demand: (v) => `${pct(+v)} of capacity`,
  claim_credit: (v) => pct(+v),
};
const POLICY_NOTES = {
  maxp: "Size the site so every rack can run flat out at the same time. Simple and safe, but power sits unused whenever racks are below peak.",
  maxq: "Hold every GPU at its most efficient operating point and fit more racks. The cap is fixed; nothing adapts.",
  maxlps: "Install more racks than the connection could feed at peak, and let a control loop share the power out every few seconds.",
};
let pending = {};
let timer = null;
function sendConfig(patch) {
  Object.assign(pending, patch);
  clearTimeout(timer);
  timer = setTimeout(() => {
    const p = pending;
    pending = {};
    post("/api/config", p);
  }, 140);
}

function buildControls() {
  const plat = $("platform");
  plat.replaceChildren();
  for (const p of Object.values(S.catalog.platforms)) {
    const b = el("button", "", p.short);
    b.dataset.v = p.key;
    b.onclick = () => sendConfig({ platform: p.key });
    plat.append(b);
  }
  const clim = $("climate");
  clim.replaceChildren();
  for (const [k, c] of Object.entries(S.catalog.climates)) {
    const o = el("option", "", c.name);
    o.value = k;
    clim.append(o);
  }
  clim.onchange = () => sendConfig({ climate: clim.value });
  for (const id of Object.keys(SLIDERS)) {
    const input = $(id);
    input.oninput = () => {
      $("o-" + id).textContent = SLIDERS[id](input.value);
      sendConfig({ [id]: +input.value });
    };
  }
  for (const b of $("policy").children) b.onclick = () => sendConfig({ policy: b.dataset.v });
  for (const b of $("speed").children) b.onclick = () => post("/api/sim", { dt_min: +b.dataset.dt });
  $("pause").onclick = () => post("/api/sim", { paused: !S.sim.paused });
  for (const b of document.querySelectorAll("[data-act]")) {
    b.onclick = () => {
      if (b.dataset.act === "target") post("/api/actions/load-target", { fraction: +b.dataset.frac, duration_min: 90, ramp_min: 10 });
      else if (b.dataset.act === "fault") post("/api/actions/fault", { kind: b.dataset.kind });
      else post("/api/reset");
    };
  }
  $("theme").onclick = () => {
    document.documentElement.dataset.theme = isDark() ? "light" : "dark";
    S.hallKey = "";
    if (S.snap) update({ snap: S.snap });
    if (S.tab === "compare") loadCompare();
  };
  for (const t of document.querySelectorAll(".tabs button")) {
    t.onclick = () => {
      S.tab = t.dataset.tab;
      for (const o of document.querySelectorAll(".tabs button")) o.setAttribute("aria-selected", o === t);
      for (const name of ["operate", "compare", "learn"]) $("tab-" + name).hidden = name !== S.tab;
      if (S.tab === "compare") loadCompare();
      if (S.tab === "operate" && S.snap) { S.hallKey = ""; update({ snap: S.snap }); }
    };
  }
  renderLearn();
}

function syncControls() {
  const c = S.snap.config;
  const p = S.catalog.platforms[c.platform];
  for (const b of $("platform").children) b.classList.toggle("on", b.dataset.v === c.platform);
  for (const b of $("policy").children) b.classList.toggle("on", b.dataset.v === c.policy);
  $("platform-note").textContent =
    `${p.generation} · ${p.gpus_per_rack} GPUs and ${p.rack_kw_maxp} kW per rack · ` +
    (p.cooling === "air" ? "air cooled" : `${pct(p.liquid_fraction)} liquid cooled`);
  $("policy-note").textContent = POLICY_NOTES[c.policy];
  $("oversub-wrap").hidden = c.policy !== "maxlps";
  if (document.activeElement !== $("climate")) $("climate").value = c.climate;
  const supply = $("supply_c");
  supply.max = p.max_supply_c;
  supply.disabled = p.cooling === "air";
  for (const id of Object.keys(SLIDERS)) {
    const input = $(id);
    if (document.activeElement !== input && !(id in pending)) input.value = c[id];
    $("o-" + id).textContent = SLIDERS[id](input.value);
  }
  if (p.cooling === "air") $("o-supply_c").textContent = "18 °C chilled (air)";
}

function renderSim() {
  $("clock").textContent = S.snap ? S.snap.clock : "";
  $("pause").textContent = S.sim.paused ? "Resume" : "Pause";
  for (const b of $("speed").children) b.classList.toggle("on", +b.dataset.dt === S.sim.dt_min);
}

function renderBus(st) {
  if (!st) return;
  const live = st.mode === "connected";
  const label = live ? "Bus connected" : st.mode === "dry-run" ? "Bus: dry run (no broker)" : "Bus: connecting";
  $("busstat").replaceChildren(el("i", live ? "live" : ""), document.createTextNode(`${label} · ${num(st.published_total)} msgs`));
}

// ---------------------------------------------------------------- KPIs and status

function renderKpis() {
  const s = S.snap, w = s.work, p = s.power, e = s.econ;
  const [tv, tu] = big(w.tokens_per_s);
  const [mv, mu] = big(e.tokens_per_s_per_mw);
  const cool = s.cooling.mode === "free" ? "Free cooling, chiller off"
    : `Chiller carrying ${pct(s.cooling.chiller_share)} of the heat`;
  const tiles = [
    ["Tokens served", tv, `${tu} /s`, `of ${bigs(w.demand_tokens_per_s)} /s requested`],
    ["Tokens per MW", mv, `${mu} /s per MW`, `${bigs(e.tokens_per_kwh)} tokens per kWh`],
    ["Grid draw", mw(p.grid_kw), "MW", `limit ${mw(p.limit_kw, 1)}, unused ${mw(p.headroom_kw)}`],
    ["PUE", num(p.pue, 3), "", cool],
    ["GPUs", num(s.site.gpus), "", `${s.site.racks} racks of ${s.platform.gpus_per_rack}`],
    ["Cost per million tokens", usd(e.allin_usd_per_mtok), "", `energy alone ${usd(e.energy_usd_per_mtok)}`],
  ];
  const root = $("kpis");
  if (root.children.length !== tiles.length) {
    root.replaceChildren(...tiles.map(() => {
      const t = el("div", "kpi");
      t.append(el("div", "k"), el("div", "v"), el("div", "d"));
      return t;
    }));
  }
  tiles.forEach(([k, v, u, d], i) => {
    const t = root.children[i];
    t.children[0].textContent = k;
    t.children[1].replaceChildren(document.createTextNode(v), el("small", "", u));
    t.children[2].textContent = d;
    t.children[2].title = d;
  });
}

function renderStatus() {
  const s = S.snap, w = s.work, p = s.power, f = s.flex;
  const node = $("status");
  const k = S.catalog.reference.inference_k;
  const cap = w.cap_inference;
  const lost = k * (1 - cap) ** 2;
  let cls = "", html;
  const leak = s.faults.find((x) => x.kind === "leak");
  const pump = s.faults.find((x) => x.kind === "pump_failure");
  if (f.active && !f.compliant) {
    cls = "crit";
    html = `<b>Grid target breached.</b> The site draws ${mw(p.grid_kw)} MW against a ${mw(f.target_kw)} MW target and power capping has run out of room. Racks have to be suspended next.`;
  } else if (f.active) {
    cls = "warn";
    const phase = { ramp_down: "ramping down to", hold: "holding below", ramp_up: "ramping back up from" }[f.phase];
    html = `<b>Grid event.</b> The site is ${phase} ${mw(f.target_kw, 1)} MW. Racks are capped at ${pct(cap)} of MaxP` +
      (w.suspended ? ` and <b>${w.suspended} are suspended</b>` : "") +
      `. Serving ${pct(w.demand_tokens_per_s ? w.tokens_per_s / w.demand_tokens_per_s : 1)} of requested tokens.`;
  } else if (leak || pump) {
    cls = "warn";
    html = leak
      ? `<b>Leak response.</b> ${w.isolated} rack isolated from coolant and power until repaired. The rest of the hall is unaffected.`
      : `<b>Cooling fault.</b> Row ${pump.row + 1} has lost a CDU pump. Its racks are power-capped to match the coolant they still get.`;
  } else if (w.control > 0.01) {
    html = `<b>Power-bound.</b> There is more demand than the grid connection can feed, so the control loop holds every rack at ${pct(cap)} of MaxP. That costs about ${pct(lost, 1)} of per-GPU throughput and keeps the site at ${mw(p.grid_kw)} MW.`;
  } else if (w.unserved_tokens_per_s > 0.01 * w.demand_tokens_per_s) {
    html = `<b>Capacity-bound.</b> Every inference GPU is busy and ${pct(w.unserved_tokens_per_s / w.demand_tokens_per_s)} of requests go unserved, while ${mw(p.headroom_kw)} MW of the grid connection sits unused. More racks would fit.`;
  } else {
    html = `<b>Demand-bound.</b> Inference racks average ${pct(w.util_inference)} utilization. ${mw(p.headroom_kw)} MW of the grid connection is paid for and unused right now.`;
  }
  node.className = "status " + cls;
  node.innerHTML = html;
}

// ---------------------------------------------------------------- flow and power stack

function renderFlow() {
  const s = S.snap, p = s.power, w = s.work, c = s.cooling, e = s.econ;
  const liquid = s.platform.cooling === "liquid";
  const mode = { free: "Free (dry coolers)", partial: "Dry coolers + chiller", chiller: "Chiller" }[c.mode];
  const nodes = [
    ["In · Electricity", mw(p.grid_kw), "MW", [
      ["Limit in force", `${mw(p.limit_kw, 1)} MW`],
      ["Price", `$${num(s.config.price_usd_mwh)}/MWh`],
      ["Energy bill", `$${num(e.energy_usd_h)}/h`],
      ["Battery", p.bess_soc === null ? "none" : p.bess_discharge_kw > 0 ? `−${mw(p.bess_discharge_kw)} MW` : `${pct(p.bess_soc)} full`],
    ]],
    ["In · Requests", big(w.demand_tokens_per_s)[0], big(w.demand_tokens_per_s)[1] + " tokens/s", [
      ["Inference racks", num(s.site.inference_racks)],
      ["Utilization", pct(w.util_inference)],
      ["Training racks", num(s.site.training_racks)],
      ["Outdoor air", `${num(c.ambient_c, 1)} °C`],
    ]],
    ["Inside · Compute hall", mw(p.it_kw), "MW IT load", [
      ["GPU racks", `${mw(p.gpu_kw)} MW`],
      ["Network", `${mw(p.network_kw)} MW`],
      ["Power cap", `${pct(w.cap_inference)} of MaxP`],
      ["Racks offline", num(w.suspended + w.isolated)],
    ]],
    ["Out · Tokens", big(w.tokens_per_s)[0], big(w.tokens_per_s)[1] + " tokens/s", [
      ["Unserved", `${bigs(w.unserved_tokens_per_s)} /s`],
      ["Per MW", `${bigs(e.tokens_per_s_per_mw)} /s`],
      ["Training", `${bigs(w.train_h100_gpu_hours_per_h)} H100-h/h`],
      ["All-in cost", `${usd(e.allin_usd_per_mtok)}/M`],
    ]],
    ["Out · Heat", mw(c.heat_kw), "MW", [
      ["Rejected by", mode],
      liquid ? ["Water in / out", `${num(c.supply_c)} / ${num(c.return_c, 1)} °C`] : ["Exhaust air", `${num(c.return_c, 1)} °C`],
      ["Flow per rack", liquid ? `${num(c.flow_lpm_per_rack)} L/min` : "air"],
      ["Cooling power", `${mw(p.cooling_kw)} MW`],
    ]],
  ];
  const root = $("flow");
  root.replaceChildren(...nodes.map(([name, v, u, rows]) => {
    const n = el("div", "node");
    n.append(el("div", "n", name));
    const b = el("div", "big", v);
    b.append(el("small", "", u));
    const dl = el("dl");
    for (const [k, val] of rows) dl.append(el("dt", "", k), el("dd", "", val));
    n.append(b, dl);
    return n;
  }));
}

function renderStack() {
  const s = S.snap, p = s.power;
  const parts = [
    ["GPU racks", "s1", p.gpu_kw],
    ["Network and storage", "s2", p.network_kw],
    ["Chiller", "s3", p.chiller_kw],
    ["Fans and pumps", "s4", p.fans_kw + p.pumps_kw],
    ["Losses and other", "s5", p.loss_kw + p.misc_kw + p.bess_charge_kw],
  ];
  const load = parts.reduce((a, x) => a + x[2], 0);
  const stack = $("stack");
  if (stack.children.length !== parts.length) {
    stack.replaceChildren(...parts.map(([name, cls]) => {
      const d = el("div");
      d.style.background = `var(--${cls})`;
      d.dataset.name = name;
      hover(d, () => tipRows(d.dataset.name, [[null, "Power", d.dataset.mw + " MW"], [null, "Share of site load", d.dataset.share]]));
      return d;
    }));
  }
  parts.forEach(([, , kw], i) => {
    const d = stack.children[i];
    d.style.flex = `0 0 calc(${(100 * kw) / p.site_kw}% - ${kw > 0 ? 2 : 0}px)`;
    d.style.display = kw > 0.0005 * p.site_kw ? "" : "none";
    d.dataset.mw = mw(kw);
    d.dataset.share = pct(load ? kw / load : 0, 1);
  });
  const leg = $("stack-legend");
  leg.replaceChildren(...parts.map(([name, cls, kw]) => {
    const sp = el("span");
    sp.append(el("i", "sw " + cls), document.createTextNode(name + " "), el("b", "", `${mw(kw)} MW`));
    return sp;
  }));
  const un = el("span");
  un.append(el("i", "sw tr"), document.createTextNode("Unused "), el("b", "", `${mw(Math.max(0, p.site_kw - load))} MW`));
  leg.append(un);
  $("power-sub").textContent = `${mw(p.site_kw, 0)} MW connection · ${pct(load / p.site_kw)} in use` +
    (p.bess_discharge_kw > 0 ? ` · battery covering ${mw(p.bess_discharge_kw)} MW` : "");
  const mark = $("limit-mark");
  mark.hidden = p.limit_kw >= p.site_kw * 0.999;
  if (!mark.hidden) {
    const x = p.limit_kw / p.site_kw;
    mark.style.left = `calc(${100 * x}% - 1px)`;
    mark.dataset.label = `Grid limit ${mw(p.limit_kw, 1)} MW`;
    mark.classList.toggle("flip", x > 0.7);
  }
}

// ---------------------------------------------------------------- compute hall

const RAMP = ["#cde2fb", "#86b6ef", "#3987e5", "#1c5cab", "#0d366b"].map((h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16)));
function rampColor(x, dark) {
  x = Math.min(1, Math.max(0, x));
  if (dark) x = 1 - x;
  const f = x * (RAMP.length - 1), i = Math.min(RAMP.length - 2, Math.floor(f)), t = f - i;
  const c = RAMP[i].map((a, k) => Math.round(a + (RAMP[i + 1][k] - a) * t));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
}
const STATES = ["Running", "Throttled: low coolant flow", "Suspended", "Isolated: leak"];

function renderHall() {
  const s = S.snap, r = s.racks, n = s.site.racks, hall = $("hall");
  const rows = s.site.rows, dark = isDark();
  const cell = rows > 60 ? 9 : rows > 24 ? 13 : 20;
  const key = `${s.platform.key}:${n}:${cell}:${s.site.inference_racks}`;
  if (key !== S.hallKey) {
    S.hallKey = key;
    S.cells = [];
    hall.style.setProperty("--cell", cell + "px");
    hall.classList.toggle("dense", rows > 60);
    hall.replaceChildren();
    for (let row = 0; row < rows; row++) {
      const block = el("div", "hrow"), cells = el("div", "cells");
      const lo = row * 10, hi = Math.min(n, lo + 10);
      const inf = Math.max(0, Math.min(hi, s.site.inference_racks) - lo);
      const role = inf === hi - lo ? "inference" : inf === 0 ? "training" : "inference + training";
      block.append(el("div", "hl", `Row ${row + 1} · ${role}`), cells);
      for (let i = lo; i < hi; i++) {
        const d = el("div", "rack");
        hover(d, () => {
          const q = S.snap.racks, pl = S.snap.platform;
          const id = `R${String(row + 1).padStart(2, "0")}-${String((i % 10) + 1).padStart(2, "0")}`;
          return tipRows(`Rack ${id} · ${q.role[i] ? "training" : "inference"}`, [
            [null, "State", STATES[q.state[i]]],
            [null, "Power", `${num(q.power_frac[i] * pl.rack_kw_maxp, 1)} kW`],
            [null, "Power cap", pct(q.cap[i]) + " of MaxP"],
            [null, "Utilization", pct(q.util[i])],
            [null, pl.cooling === "liquid" ? "Coolant return" : "Exhaust air", `${num(q.return_c[i], 1)} °C`],
          ]);
        });
        cells.append(d);
        S.cells.push(d);
      }
      hall.append(block);
    }
    $("ramp").style.background = `linear-gradient(90deg, ${[0, 0.25, 0.5, 0.75, 1].map((x) => rampColor(x, dark)).join(",")})`;
  }
  for (let i = 0; i < n; i++) {
    const d = S.cells[i];
    d.style.background = rampColor((r.power_frac[i] - 0.08) / 0.92, dark);
    d.className = "rack" + (r.state[i] ? " st" + r.state[i] : "");
  }
  $("hall-sub").textContent =
    `${n} racks in ${rows} rows of 10, one coolant distribution unit (CDU) per row · ${s.site.inference_racks} inference, ${s.site.training_racks} training`;
}

// ---------------------------------------------------------------- tooltips

const tip = $("tip");
function tipRows(title, rows) {
  const frag = document.createDocumentFragment();
  frag.append(el("div", "t", title));
  for (const [color, k, v] of rows) {
    const r = el("div", "r"), name = el("span");
    if (color) { const sw = el("i"); sw.style.background = color; name.append(sw); }
    name.append(document.createTextNode(k));
    r.append(name, el("b", "", v));
    frag.append(r);
  }
  return frag;
}
function showTip(content, x, y) {
  tip.replaceChildren(content);
  tip.hidden = false;
  const w = tip.offsetWidth, h = tip.offsetHeight;
  tip.style.left = Math.min(window.innerWidth - w - 8, x + 14) + "px";
  tip.style.top = Math.max(8, Math.min(window.innerHeight - h - 8, y - h - 10)) + "px";
}
function hover(node, make) {
  node.addEventListener("mousemove", (e) => showTip(make(), e.clientX, e.clientY));
  node.addEventListener("mouseleave", () => (tip.hidden = true));
}

// ---------------------------------------------------------------- line charts

const SVGNS = "http://www.w3.org/2000/svg";
const svg = (tag, attrs) => {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v);
  return n;
};
function niceTicks(lo, hi, count = 4) {
  const span = hi - lo || 1, raw = span / count, mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((st) => span / st <= count + 0.5) || raw;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-6; v += step) out.push(+v.toFixed(10));
  return out;
}

function lineChart(root, { xs, series, fmt, zero = false, pad = 0.08, minSpan = 0 }) {
  const W = root.clientWidth, H = root.clientHeight;
  if (W < 160 || H < 80 || xs.length < 2) return;
  const m = { l: 40, r: 50, t: 8, b: 20 };
  let lo = Infinity, hi = -Infinity;
  for (const s of series) for (const v of s.values) { if (v < lo) lo = v; if (v > hi) hi = v; }
  if (zero) lo = 0;
  if (hi - lo < minSpan) { const mid = (hi + lo) / 2; lo = mid - minSpan / 2; hi = mid + minSpan / 2; }
  const span = hi - lo || Math.abs(hi) * 0.1 || 1;
  lo = zero ? 0 : lo - span * pad;
  hi += span * pad;
  const x = (t) => m.l + ((t - xs[0]) / (xs[xs.length - 1] - xs[0] || 1)) * (W - m.l - m.r);
  const y = (v) => H - m.b - ((v - lo) / (hi - lo)) * (H - m.t - m.b);
  const g = svg("svg", { viewBox: `0 0 ${W} ${H}` });

  for (const v of niceTicks(lo, hi)) {
    g.append(svg("line", { x1: m.l, x2: W - m.r, y1: y(v), y2: y(v), stroke: "var(--grid)", "stroke-width": 1 }));
    const t = svg("text", { x: m.l - 6, y: y(v) + 4, "text-anchor": "end" });
    t.textContent = fmt(v);
    g.append(t);
  }
  const t0 = xs[0], t1 = xs[xs.length - 1], stepMin = [30, 60, 120, 180, 360, 720, 1440].find((st) => (t1 - t0) / st <= 6) || 1440;
  for (let t = Math.ceil(t0 / stepMin) * stepMin; t <= t1; t += stepMin) {
    const lab = svg("text", { x: x(t), y: H - 5, "text-anchor": "middle" });
    lab.textContent = clockOf(t);
    g.append(lab);
  }
  g.append(svg("line", { x1: m.l, x2: W - m.r, y1: H - m.b, y2: H - m.b, stroke: "var(--axis)", "stroke-width": 1 }));

  const ends = [];
  for (const s of series) {
    let d = "";
    for (let i = 0; i < xs.length; i++) d += (i ? "L" : "M") + x(xs[i]).toFixed(1) + " " + y(s.values[i]).toFixed(1);
    g.append(svg("path", { d, fill: "none", stroke: s.color, "stroke-width": s.ref ? 1.5 : 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
    const last = s.values[s.values.length - 1];
    if (!s.ref) {
      g.append(svg("circle", { cx: x(t1), cy: y(last), r: 4, fill: s.color, stroke: "var(--surface)", "stroke-width": 2 }));
      ends.push({ y: y(last), text: fmt(last) });
    }
  }
  ends.sort((a, b) => a.y - b.y);
  for (let i = 1; i < ends.length; i++) if (ends[i].y - ends[i - 1].y < 13) ends[i].y = ends[i - 1].y + 13;
  for (const e of ends) {
    const t = svg("text", { x: W - m.r + 9, y: e.y + 4, class: "endlabel" });
    t.textContent = e.text;
    g.append(t);
  }

  const cross = svg("line", { y1: m.t, y2: H - m.b, stroke: "var(--axis)", "stroke-width": 1, visibility: "hidden" });
  g.append(cross);
  const hit = svg("rect", { x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, fill: "transparent" });
  const at = (clientX, clientY) => {
    const px = clientX - root.getBoundingClientRect().left;
    let i = 0, best = Infinity;
    for (let k = 0; k < xs.length; k++) { const dd = Math.abs(x(xs[k]) - px); if (dd < best) { best = dd; i = k; } }
    cross.setAttribute("x1", x(xs[i]));
    cross.setAttribute("x2", x(xs[i]));
    cross.setAttribute("visibility", "visible");
    showTip(tipRows(clockOf(xs[i]), series.map((s) => [s.color, s.name, fmt(s.values[i])])), clientX, clientY);
  };
  hit.addEventListener("mousemove", (e) => { root._hover = [e.clientX, e.clientY]; at(e.clientX, e.clientY); });
  hit.addEventListener("mouseleave", () => { root._hover = null; tip.hidden = true; });
  g.append(hit);
  root.replaceChildren(g);
  if (root._hover) at(...root._hover);
}

function renderCharts() {
  const h = S.history;
  if (h.length < 2) return;
  const xs = h.map((p) => p.t);
  lineChart($("c-power"), {
    xs, zero: true, fmt: (v) => num(v, 1),
    series: [
      { name: "Limit", color: "var(--muted)", ref: true, values: h.map((p) => p.limit_kw / 1000) },
      { name: "Grid draw", color: "var(--s1)", values: h.map((p) => p.grid_kw / 1000) },
    ],
  });
  const top = Math.max(...h.map((p) => p.demand));
  const div = top >= 1e9 ? 1e9 : 1e6;
  $("c-tokens").parentElement.querySelector(".muted").textContent = (div === 1e9 ? "Billion" : "Million") + " tokens per second";
  lineChart($("c-tokens"), {
    xs, zero: true, fmt: (v) => num(v, v < 10 ? 2 : 1),
    series: [
      { name: "Requested", color: "var(--s2)", values: h.map((p) => p.demand / div) },
      { name: "Served", color: "var(--s1)", values: h.map((p) => p.tokens / div) },
    ],
  });
  lineChart($("c-pue"), { xs, fmt: (v) => num(v, 2), minSpan: 0.08, pad: 0.1, series: [{ name: "PUE", color: "var(--s1)", values: h.map((p) => p.pue) }] });
  lineChart($("c-ambient"), { xs, fmt: (v) => num(v, 0), minSpan: 6, pad: 0.1, series: [{ name: "Outdoor air", color: "var(--s1)", values: h.map((p) => p.ambient) }] });
}

// ---------------------------------------------------------------- log and bus feeds

function addLog(entries) {
  const root = $("log");
  for (const e of entries || []) {
    const li = el("li");
    li.append(el("time", "", e.clock || ""), el("span", "", e.text));
    root.prepend(li);
  }
  while (root.children.length > 60) root.lastChild.remove();
  if (!root.children.length) root.append(el("li", "empty", "Nothing yet. Use the buttons on the left to cause an event."));
  else if (root.children.length > 1) root.querySelector(".empty")?.remove();
}

function addBus(messages) {
  const root = $("bus");
  for (const m of messages || []) {
    const li = el("li"), row = el("div", "row"), body = el("div");
    body.append(el("div", "sum", m.summary), el("div", "topic", m.topic));
    row.append(el("time", "", m.clock), el("span", "fam", m.family === "flex" ? "FLEX" : "BMS"), body);
    li.append(row);
    li.onclick = (ev) => {
      if (ev.target.closest("pre")) return;
      const open = li.querySelector("pre");
      if (open) return li.querySelectorAll("pre").forEach((p) => p.remove());
      const add = (caption, obj) => {
        const pre = el("pre");
        pre.append(el("span", "cap", caption), document.createTextNode(JSON.stringify(obj, null, 2)));
        li.append(pre);
      };
      if (m.decoded) add("Payload after verifying the signature", m.decoded);
      add(m.decoded ? "On the wire: CloudEvents envelope, data is a signed JWS" : "On the wire", m.payload);
    };
    root.prepend(li);
  }
  while (root.children.length > 80) root.lastChild.remove();
}

// ---------------------------------------------------------------- compare tab

function barChart(title, unit, rows, fmt, selected, baseLabel, lowerBetter = false) {
  const box = el("div", "bars");
  box.append(el("h4", "", title), el("div", "u", unit));
  const max = Math.max(...rows.map((r) => r.value || 0)) || 1;
  const base = rows[0].value;
  for (const r of rows) {
    const row = el("div", "bar-row" + (r.key === selected ? " sel" : ""));
    const track = el("div", "bar-track"), fill = el("div", "bar-fill"), val = el("span", "bar-val", fmt(r.value));
    fill.style.width = `calc((100% - 104px) * ${(r.value || 0) / max})`;
    if (base && r !== rows[0]) {
      const x = lowerBetter ? base / r.value : r.value / base;
      val.append(el("em", "", `${num(x, x >= 10 ? 0 : 1)}×${lowerBetter ? " lower" : ""}`));
    }
    track.append(fill, val);
    row.append(el("span", "lab", r.label), track);
    hover(row, () => tipRows(r.label, [[null, title, fmt(r.value)], [null, `vs ${baseLabel}`, base ? `${num(r.value / base, 2)}×` : "n/a"]]));
    box.append(row);
  }
  return box;
}

async function loadCompare() {
  const r = await (await fetch("/api/compare")).json();
  const c = r.config, cat = S.catalog, sel = S.snap.config.platform;
  const polName = { maxp: "MaxP", maxq: "MaxQ", maxlps: "MaxLPS" };
  $("cmp-lede").innerHTML =
    `One <b>${c.site_mw} MW</b> grid connection in a <b>${cat.climates[c.climate].name}</b> climate, sized with <b>${polName[c.policy]}</b>, ` +
    `every rack on sold-out inference. Only the hardware changes. Vendor claim credit is at <b>${pct(c.claim_credit)}</b>; ` +
    `move that slider on the Operate tab to see how much the answer depends on it.`;
  const rows = (f) => r.platforms.map((p) => ({ key: p.platform, label: cat.platforms[p.platform].short, value: f(p) }));
  const card = (node) => { const s = el("section", "card"); s.append(node); return s; };
  $("cmp-charts").replaceChildren(
    card(barChart("GPUs that fit", "installed on this connection", rows((p) => p.gpus), (v) => num(v), sel, "H100")),
    card(barChart("Tokens per second", "whole site", rows((p) => p.tokens_per_s), bigs, sel, "H100")),
    card(barChart("Tokens per MW", "tokens/s per MW of grid power", rows((p) => p.tokens_per_s_per_mw), bigs, sel, "H100")),
    card(barChart("Cost per million tokens", "energy plus amortized capex", rows((p) => p.allin_usd_per_mtok), usd, sel, "H100", true)),
  );
  const prow = (f) => r.policies.map((p) => ({ key: p.policy, label: polName[p.policy], value: f(p) }));
  $("pol-sub").textContent = `${cat.platforms[c.platform].name} on the same ${c.site_mw} MW`;
  $("pol-charts").replaceChildren(
    barChart("GPUs that fit", "installed on this connection", prow((p) => p.gpus), (v) => num(v), c.policy, "MaxP"),
    barChart("Tokens per second", "whole site, sold out", prow((p) => p.tokens_per_s), bigs, c.policy, "MaxP"),
  );

  const cool = { free: "Free", partial: "Free + chiller", chiller: "Chiller" };
  table($("cmp-table"),
    ["System", "Racks", "GPUs", "Design-day PUE", "Average PUE", "Heat rejection", "Tokens/s", "Tokens/s per MW", "Energy $/M tokens", "All-in $/M tokens"],
    r.platforms.map((p) => ({ sel: p.platform === sel, cells: [p.name, num(p.racks), num(p.gpus), num(p.design_pue, 3), num(p.pue, 3),
      cool[p.cooling_mode], bigs(p.tokens_per_s), bigs(p.tokens_per_s_per_mw), usd(p.energy_usd_per_mtok), usd(p.allin_usd_per_mtok)] })));
  const tag = (p, k) => p.basis[k] || "";
  table($("spec-table"),
    ["System", "GPUs per rack", "Rack power, MaxP", "Rack power, MaxQ", "Liquid share of heat", "Warmest supply water",
      "Inference per GPU vs H100", "Training per GPU vs H100", "Rack cost"],
    Object.values(cat.platforms).map((p) => ({ sel: p.key === sel, cells: [
      [p.name, p.generation],
      [num(p.gpus_per_rack), tag(p, "gpus_per_rack")],
      [`${num(p.rack_kw_maxp)} kW`, tag(p, "rack_kw_maxp")],
      [`${num(p.rack_kw_maxq)} kW`, tag(p, "maxq_fraction")],
      [pct(p.liquid_fraction), tag(p, "liquid_fraction")],
      [`${p.max_supply_c} °C`, tag(p, "max_supply_c")],
      [`${num(p.perf.inference.compute, 1)}× to ${num(p.perf.inference.claim)}×`, "compute to claim"],
      [`${num(p.perf.training.compute, 2)}× to ${num(p.perf.training.claim)}×`, "compute to claim"],
      [`$${num(p.rack_capex_usd_m, 1)}M`, tag(p, "rack_capex_usd_m")],
    ] })));
}

function table(node, head, rows) {
  const thead = el("thead"), tr = el("tr"), tbody = el("tbody");
  for (const h of head) tr.append(el("th", "", h));
  thead.append(tr);
  for (const r of rows) {
    const row = el("tr", r.sel ? "sel" : "");
    for (const c of r.cells) {
      const td = el("td");
      if (Array.isArray(c)) { td.append(document.createTextNode(c[0]), el("span", "tag", c[1])); } else td.textContent = c;
      row.append(td);
    }
    tbody.append(row);
  }
  node.replaceChildren(thead, tbody);
}

// ---------------------------------------------------------------- learn tab

const LEARN = [
  ["DSX Reference Design", "Validated blueprints for compute, network, storage, power and cooling, one per hardware generation.",
    "<b>Here:</b> the hardware picker and the sizing math. Pick a system and the model works out how many racks a grid connection carries on the hottest design day.",
    "<b>Try:</b> switch from H100 to Vera Rubin on the same 10 MW. Fewer racks, far more tokens, and the chiller mostly switches off."],
  ["DSX MaxLPS", "Chip, cooling and software techniques that get the most tokens out of a fixed power envelope. Includes 45 °C liquid cooling and Dynamic Power Software.",
    "<b>Here:</b> the power policy switch and the control loop that caps rack power. MaxLPS installs more racks than the connection can feed at peak, then shares the power out.",
    "<b>Try:</b> flip MaxP to MaxLPS and watch GPUs rise about 40% while per-GPU speed drops a few percent. Then lower the supply water to 25 °C in the hot climate and watch PUE climb."],
  ["DSX Exchange", "The open-source bus that lets facility systems (power, cooling) and IT systems share signals. NATS underneath, MQTT on the wire, AsyncAPI schemas on top.",
    "<b>Here:</b> the Event bus panel. Every message uses the published topic pattern, for example <code>BMS/v1/PUB/Value/Rack/RackPower/…</code>, and the published value shape.",
    "<b>Try:</b> trigger a coolant leak. The BMS reports the leak, IT requests isolation, the BMS confirms. Three messages, two publishers."],
  ["DSX Flex", "How an AI factory takes part in grid programs: a grid services provider sets a load target, the site reports its power state, and breaches are alerted and enforced.",
    "<b>Here:</b> the two grid buttons. The four Flex event types are published as signed CloudEvents with the documented payload fields.",
    "<b>Try:</b> ask for 75% and the site rides through on power capping alone. Ask for 40% and capping runs out, a breach alert fires, and racks get suspended, training first."],
  ["DSX OS", "The open-source software that runs the factory: scheduling (KAI Scheduler, Run:ai), health (NVSentinel), bare-metal lifecycle (NICo), the GPU Operator and more.",
    "<b>Here:</b> not modelled beyond one hook. <code>/metrics</code> exposes GPU telemetry under the same DCGM names NVIDIA's exporter uses, so cluster dashboards read it unchanged.",
    "<b>Next:</b> a real scheduler placing jobs on these racks is the obvious extension."],
  ["DSX Sim", "Simulate the factory before building it: the Omniverse DSX Blueprint for physically accurate digital twins, and DSX Air for the network.",
    "<b>Here:</b> this whole app is a toy version of that idea. It uses a few dozen equations where the real thing uses CFD, electrical and thermal solvers.",
    "<b>Read:</b> docs/MODEL.md in the repo lists every equation and where each constant comes from."],
];
function renderLearn() {
  $("learn").replaceChildren(...LEARN.map(([name, what, here, tryit]) => {
    const c = el("section", "card");
    const p1 = el("p"), p2 = el("p");
    p1.innerHTML = here;
    p2.innerHTML = tryit;
    c.append(el("h3", "", name), el("div", "what", what), p1, p2);
    return c;
  }));
}

window.addEventListener("resize", () => { if (S.snap && S.tab === "operate") { renderHall(); renderCharts(); } });
connect();
