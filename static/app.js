"use strict";

// All rendering is hand-built here. No external libraries, no network beyond this app's own API.

const VEL_COLOR = { A: "#2a9d8f", B: "#e9a03b", C: "#6b7a8d", "-": "#33414f" };
let SLOTS = [];
let mode = "legacy"; // or "optimized"

async function getJSON(url, opts) {
  const res = await fetch(url, opts);
  if (!res.ok) throw new Error("HTTP " + res.status);
  return res.json();
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
    svg += `<rect class="slot-rect" x="${x}" y="${y}" width="${cell}" height="${cell}" rx="5" fill="${color}" `
      + `data-id="${s.id}" data-sku="${s[skuKey]}" data-vel="${s[key]}" data-dist="${s.distance}" data-aisle="${s.aisle}"/>`;
  });
  for (let a = 0; a < aisles; a++) {
    const y = padT + a * (cell + gap) + cell / 2 + 4;
    svg += `<text x="8" y="${y}" class="aisle-label">Aisle ${a + 1}</text>`;
  }
  svg += `</svg>`;
  el.innerHTML = svg;

  const tip = document.getElementById("map-tip");
  el.querySelectorAll(".slot-rect").forEach(r => {
    r.addEventListener("mouseenter", () => {
      tip.textContent = `Slot ${r.dataset.id} (Aisle ${+r.dataset.aisle + 1}) - `
        + `${r.dataset.sku || "empty"} - velocity ${r.dataset.vel} - ${r.dataset.dist} m from dispatch`;
    });
  });
}

async function loadSlots() {
  const data = await getJSON("/api/slots");
  SLOTS = data.slots;
  renderMap();
}

async function loadReshuffle() {
  const data = await getJSON("/api/reshuffle");
  document.getElementById("reshuffle-summary").innerHTML =
    `<b>${data.n_moves}</b> moves reach the optimized layout &middot; travel -<b>${data.reduction_pct}%</b> `
    + `&middot; break-even in <b>${data.break_even_days}</b> days.`;
  const tb = document.querySelector("#reshuffle-table tbody");
  tb.innerHTML = data.moves.map(m => `<tr><td>${m.sku}</td><td>${m.from_slot}</td><td>${m.to_slot}</td></tr>`).join("");
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
      html += `<div class="row"><span>Recommended container</span><span>${r.recommended_container ?? "oversize"}</span></div>`;
      if (r.placement) {
        html += `<div class="row"><span>Placement (x,y,z)</span><span>${r.placement.x}, ${r.placement.y}, ${r.placement.z}</span></div>`;
      }
      if (r.over_weight) html += `<div class="row"><span>Weight</span><span class="bad">exceeds max</span></div>`;
      if (r.reslot_instruction) {
        html += `<div class="row"><span>Re-slot</span><span class="badge move">move slot ${r.reslot_instruction.from_slot} &rarr; ${r.reslot_instruction.to_slot}</span></div>`;
      } else {
        html += `<div class="row"><span>Re-slot</span><span>none (already optimal)</span></div>`;
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
