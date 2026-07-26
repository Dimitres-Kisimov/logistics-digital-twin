"use strict";

// All rendering is hand-built here. No external libraries, no network beyond this app's own API.

const VEL_COLOR = { A: "#2a9d8f", B: "#e9a03b", C: "#6b7a8d", "-": "#33414f" };
let SLOTS = [];
let PLAN = null; // /api/reshuffle payload (sequence, savings, headline numbers)
let mode = "legacy"; // or "optimized"
const HILITE = new Set(); // slot ids highlighted from the re-shuffle table

async function getJSON(url, opts) {
  const res = await fetch(url, opts);
  if (!res.ok) {
    // Surface the server's error message when it sent one (e.g. validation errors).
    let msg = "HTTP " + res.status;
    try {
      const body = await res.json();
      if (body && body.error) msg = body.error;
    } catch (_) { /* non-JSON error body */ }
    throw new Error(msg);
  }
  return res.json();
}

function slotDetailText(r) {
  return `Slot ${r.dataset.code} (id ${r.dataset.id}) - ${r.dataset.sku || "empty"} - `
    + `velocity ${r.dataset.vel} - ${r.dataset.dist} m from dispatch`;
}

function renderMap() {
  const el = document.getElementById("map");
  if (!SLOTS.length) { el.textContent = "No slots."; return; }

  const aisles = Math.max(...SLOTS.map(s => s.aisle)) + 1;
  const perAisle = {};
  SLOTS.forEach(s => { perAisle[s.aisle] = (perAisle[s.aisle] || 0) + 1; });
  const maxPer = Math.max(...Object.values(perAisle));

  const cell = 30, gap = 6, padL = 70, padT = 34;
  const w = padL + maxPer * (cell + gap) + 20;
  const h = padT + aisles * (cell + gap) + 20;

  const key = mode === "legacy" ? "legacy_velocity" : "optimized_velocity";
  const skuKey = mode === "legacy" ? "legacy_sku" : "optimized_sku";

  // Position within each aisle by slot order (index), so column 0 = nearest dispatch.
  const idxByAisle = {};
  let svg = `<svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" role="img" aria-label="Warehouse slot map">`;
  svg += `<text x="10" y="20" class="dispatch-label">DISPATCH</text>`;
  svg += `<line x1="${padL - 12}" y1="${padT - 8}" x2="${padL - 12}" y2="${h - 12}" stroke="#2a9d8f" stroke-width="2"/>`;

  // sort slots for stable layout: by aisle then distance (nearest first)
  const ordered = [...SLOTS].sort((a, b) => a.aisle - b.aisle || a.distance - b.distance);
  ordered.forEach(s => {
    const col = (idxByAisle[s.aisle] = (idxByAisle[s.aisle] === undefined ? 0 : idxByAisle[s.aisle] + 1));
    const x = padL + col * (cell + gap);
    const y = padT + s.aisle * (cell + gap);
    const color = VEL_COLOR[s[key]] || VEL_COLOR["-"];
    const hl = HILITE.has(s.id) ? " hl" : "";
    const label = `Slot ${s.code}, ${s[skuKey] || "empty"}, velocity ${s[key]}, ${s.distance} m from dispatch`;
    svg += `<rect class="slot-rect${hl}" x="${x}" y="${y}" width="${cell}" height="${cell}" rx="5" fill="${color}" `
      + `tabindex="0" role="img" aria-label="${label}" data-id="${s.id}" data-code="${s.code}" `
      + `data-sku="${s[skuKey]}" data-vel="${s[key]}" data-dist="${s.distance}" data-aisle="${s.aisle}"/>`;
  });
  for (let a = 0; a < aisles; a++) {
    const y = padT + a * (cell + gap) + cell / 2 + 4;
    svg += `<text x="8" y="${y}" class="aisle-label">Aisle ${a + 1}</text>`;
  }
  svg += `</svg>`;
  el.innerHTML = svg;

  const tip = document.getElementById("map-tip");
  const show = (r) => { tip.textContent = slotDetailText(r); };
  el.querySelectorAll(".slot-rect").forEach(r => {
    r.addEventListener("mouseenter", () => show(r)); // hover
    r.addEventListener("focus", () => show(r));      // keyboard (Tab)
    r.addEventListener("click", () => show(r));      // tap / click
  });
}

function highlightSlots(ids) {
  HILITE.clear();
  ids.forEach(id => HILITE.add(id));
  renderMap();
}

async function loadSlots() {
  const data = await getJSON("/api/slots");
  SLOTS = data.slots;
  renderMap();
}

function exportCsv() {
  if (!PLAN || !PLAN.sequence.length) return;
  const header = "seq,sku,from_slot,from_code,to_slot,to_code,cycle,saving_m_day";
  const rows = PLAN.sequence.map(s => [
    s.seq, s.sku,
    s.from_slot === null ? "" : s.from_slot, s.from_code,
    s.to_slot === null ? "" : s.to_slot, s.to_code,
    s.cycle, s.saving_m_day,
  ].join(","));
  const csv = header + "\n" + rows.join("\n") + "\n";
  const blob = new Blob([csv], { type: "text/csv" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "reshuffle-plan-synthetic.csv";
  a.click();
  URL.revokeObjectURL(a.href);
}

function stepTitle(s) {
  if (s.to_slot === null) return `park ${s.sku} in the staging position (frees slot ${s.from_slot})`;
  if (s.from_slot === null) return `retrieve ${s.sku} from staging into slot ${s.to_slot}`;
  return `slot ${s.from_slot} to slot ${s.to_slot} - click to highlight on map`;
}

// What-if: executing only the first k steps of the sequence. Savings are credited when a SKU
// lands in its final slot, so the cumulative figure is exact for any prefix.
function applyWhatIf(k) {
  const seq = PLAN.sequence;
  const n = seq.length;
  document.getElementById("whatif-n").textContent = k === n ? `all ${n}` : `${k} of ${n}`;
  let cum = 0, finals = 0, parked = 0;
  for (let i = 0; i < k; i++) {
    cum += seq[i].saving_m_day;
    if (seq[i].to_slot === null) parked++;
    else { finals++; if (seq[i].from_slot === null) parked--; }
  }
  const frac = PLAN.travel_saved_m_day > 0 ? cum / PLAN.travel_saved_m_day : 0;
  const pctOfFull = (100 * frac).toFixed(0);
  const reduction = (PLAN.reduction_pct * frac).toFixed(1);
  const out = document.getElementById("whatif-readout");
  if (k === 0) {
    out.textContent = "No steps executed: layout stays legacy (0% of the saving).";
  } else if (k === n) {
    out.textContent = `All ${n} steps: full -${Number(PLAN.reduction_pct).toFixed(1)}% pick-travel reduction, ${finals} SKUs in their final slots.`;
  } else {
    out.textContent = `${pctOfFull}% of the full saving (-${reduction}% pick travel), ${finals} SKUs in their final slots.`
      + (parked > 0 ? " 1 carton is still parked in staging - finish the current cycle before stopping." : "");
  }
  document.querySelectorAll("#reshuffle-table tbody tr").forEach((tr, i) => {
    tr.classList.toggle("beyond", i >= k);
  });
}

async function loadReshuffle() {
  PLAN = await getJSON("/api/reshuffle");
  const data = PLAN;
  // Same precision as the KPI tile (1 dp) so the number reads identically everywhere.
  const pct = Number(data.reduction_pct).toFixed(1);
  const parks = data.n_steps - data.n_moves;
  document.getElementById("reshuffle-summary").innerHTML =
    `<b>${data.n_moves}</b> SKUs move in <b>${data.n_steps}</b> executable steps `
    + `(${parks} staging park${parks === 1 ? "" : "s"}, ${data.n_cycles} cycles) `
    + `&middot; demand-weighted pick travel -<b>${pct}%</b> `
    + `&middot; break-even in <b>${data.break_even_days}</b> days.`;
  const tb = document.querySelector("#reshuffle-table tbody");
  tb.innerHTML = data.sequence.map((s, i) => {
    const from = s.from_slot === null ? `<span class="stage">STAGE</span>` : s.from_code;
    const to = s.to_slot === null ? `<span class="stage">STAGE</span>` : s.to_code;
    const first = i === 0 || s.cycle !== data.sequence[i - 1].cycle;
    return `<tr data-i="${i}" tabindex="0" class="${first ? "cycle-start" : ""}" title="${stepTitle(s)}">`
      + `<td>${s.seq}</td><td>${s.sku}</td><td>${from}</td><td>${to}</td></tr>`;
  }).join("");
  tb.querySelectorAll("tr").forEach(tr => {
    const pick = () => {
      const s = data.sequence[+tr.dataset.i];
      tb.querySelectorAll("tr.sel").forEach(x => x.classList.remove("sel"));
      tr.classList.add("sel");
      const ids = [s.from_slot, s.to_slot].filter(x => x !== null);
      highlightSlots(ids);
      document.getElementById("map-tip").textContent =
        `Step ${s.seq} (cycle ${s.cycle}): ${s.sku} ${s.from_code} to ${s.to_code} - highlighted on map.`;
    };
    tr.addEventListener("click", pick);
    tr.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); } });
  });
  const slider = document.getElementById("seq-slider");
  slider.max = data.n_steps;
  slider.value = data.n_steps;
  slider.addEventListener("input", () => applyWhatIf(+slider.value));
  applyWhatIf(data.n_steps);
  document.getElementById("btn-export-csv").addEventListener("click", exportCsv);
}

function bindToggle() {
  const bl = document.getElementById("btn-legacy");
  const bo = document.getElementById("btn-optimized");
  bl.addEventListener("click", () => { mode = "legacy"; bl.classList.add("active"); bo.classList.remove("active"); renderMap(); });
  bo.addEventListener("click", () => { mode = "optimized"; bo.classList.add("active"); bl.classList.remove("active"); renderMap(); });
}

function bindScan() {
  const form = document.getElementById("scan-form");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    const body = {
      sku: fd.get("sku"),
      length: fd.get("length"),
      width: fd.get("width"),
      height: fd.get("height"),
      weight: fd.get("weight"),
    };
    const out = document.getElementById("scan-result");
    try {
      const r = await getJSON("/api/scan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const fitCls = r.fits_container ? "ok" : "bad";
      let html = "";
      html += `<div class="row"><span>Fits container</span><span class="${fitCls}">${r.fits_container ? "yes" : "no"}</span></div>`;
      html += `<div class="row"><span>Carton volume</span><span>${r.carton_volume_cm3} cm3</span></div>`;
      html += `<div class="row"><span>Fill if shipped alone</span><span>${r.fill_if_alone_pct}%</span></div>`;
      html += `<div class="row"><span>Recommended container</span><span>${r.recommended_container ?? (r.fits_dimensions ? "none" : "oversize")}</span></div>`;
      if (r.placement) {
        html += `<div class="row"><span>Placement (x,y,z)</span><span>${r.placement.x}, ${r.placement.y}, ${r.placement.z}</span></div>`;
      }
      if (r.over_weight) html += `<div class="row"><span>Weight</span><span class="bad">exceeds max</span></div>`;
      if (r.reslot_instruction) {
        const mv = r.reslot_instruction;
        html += `<div class="row"><span>Re-slot</span><span class="badge move" title="slot ${mv.from_slot} to slot ${mv.to_slot}">`
          + `move ${mv.from_code} &rarr; ${mv.to_code}</span></div>`;
      } else if (!r.sku) {
        html += `<div class="row"><span>Re-slot</span><span>n/a (no SKU entered)</span></div>`;
      } else if (!r.sku_known) {
        html += `<div class="row"><span>Re-slot</span><span>unknown SKU (not in the 60-SKU synthetic catalog)</span></div>`;
      } else {
        html += `<div class="row"><span>Re-slot</span><span>none (already in its optimal slot)</span></div>`;
      }
      html += `<p class="note">${r.note}</p>`;
      out.innerHTML = html;
    } catch (err) {
      out.textContent = "Scan failed: " + err.message;
    }
  });
}

window.addEventListener("DOMContentLoaded", () => {
  bindToggle();
  bindScan();
  loadSlots().catch(e => { document.getElementById("map").textContent = "Failed to load slots: " + e.message; });
  loadReshuffle().catch(() => {});
});
