/* J.A.R.V.I.S. Canvas — an infinite, manipulable board.
   Classic script; uses the vendored globals `mermaid` and `Chart`.

   The board is a fixed #viewport with a transformed #world inside it. Cards live in
   world coordinates (left/top/width/height); panning translates the world, the wheel
   zooms it toward the cursor. Layout edits (move/resize/remove/clear/focus/viewport)
   are sent back to the server over the same WebSocket, which persists the board and
   tells the brain which card is "focused" so "make this bigger" resolves correctly. */

"use strict";

const VP = document.getElementById("viewport");
const world = document.getElementById("world");
const emptyEl = document.getElementById("empty");
const GRID = 26;

let view = { x: 0, y: 0, zoom: 1 };
const cards = new Map(); // id -> { el, body, data, chart }
let selectedId = null;
let ws = null;
let seq = 0;

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

mermaid.initialize({
  startOnLoad: false,
  theme: "dark",
  securityLevel: "strict",
  themeVariables: {
    fontFamily: "JetBrains Mono, monospace",
    primaryColor: "#1f242d", primaryBorderColor: "#a78bfa", primaryTextColor: "#e8ebf1",
    lineColor: "#5b6472", secondaryColor: "#181c23", tertiaryColor: "#181c23",
  },
});

const CHART_PALETTE = ["#2dd4bf", "#a78bfa", "#fbbf24", "#f472b6", "#60a5fa", "#a3e635"];
const TITLES = { mermaid: "Diagram", chart: "Chart", stats: "Stats", image: "Image", markdown: "Notes" };

/* ----------------------------------------------------------------- transport */
function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}
function sendUpdate(d) {
  send({ type: "canvas_update", id: d.id, x: Math.round(d.x), y: Math.round(d.y), w: Math.round(d.w), h: Math.round(d.h) });
}

let vpTimer = null;
function scheduleViewportSave() {
  clearTimeout(vpTimer);
  vpTimer = setTimeout(() => send({ type: "canvas_viewport", x: view.x, y: view.y, zoom: view.zoom }), 400);
}

/* ------------------------------------------------------------------- the view */
function applyView() {
  world.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.zoom})`;
  VP.style.backgroundSize = `${GRID * view.zoom}px ${GRID * view.zoom}px`;
  VP.style.backgroundPosition = `${view.x}px ${view.y}px`;
  document.getElementById("zoom-level").textContent = Math.round(view.zoom * 100) + "%";
}

function zoomAt(z, sx, sy) {
  const rect = VP.getBoundingClientRect();
  if (sx == null) sx = rect.width / 2;
  if (sy == null) sy = rect.height / 2;
  z = clamp(z, 0.2, 2.5);
  const wx = (sx - view.x) / view.zoom, wy = (sy - view.y) / view.zoom;
  view.zoom = z;
  view.x = sx - wx * z;
  view.y = sy - wy * z;
  applyView();
  scheduleViewportSave();
}

function home() { view = { x: 0, y: 0, zoom: 1 }; applyView(); scheduleViewportSave(); }

function fit() {
  if (!cards.size) { home(); return; }
  let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
  for (const { data: d } of cards.values()) {
    minx = Math.min(minx, d.x); miny = Math.min(miny, d.y);
    maxx = Math.max(maxx, d.x + d.w); maxy = Math.max(maxy, d.y + d.h);
  }
  const rect = VP.getBoundingClientRect();
  const pad = 90;
  const z = clamp(Math.min((rect.width - pad * 2) / (maxx - minx), (rect.height - pad * 2) / (maxy - miny), 1.4), 0.2, 2.5);
  view.zoom = z;
  view.x = rect.width / 2 - ((minx + maxx) / 2) * z;
  view.y = rect.height / 2 - ((miny + maxy) / 2) * z;
  applyView();
  scheduleViewportSave();
}

/* --------------------------------------------------------------- pan + zoom */
VP.addEventListener("wheel", (e) => {
  e.preventDefault();
  const rect = VP.getBoundingClientRect();
  zoomAt(view.zoom * Math.exp(-e.deltaY * 0.0015), e.clientX - rect.left, e.clientY - rect.top);
}, { passive: false });

let pan = null;
VP.addEventListener("pointerdown", (e) => {
  if (e.target.closest(".card")) return;          // cards handle their own pointer
  if (e.button !== 0 && e.button !== 1) return;
  pan = { sx: e.clientX, sy: e.clientY, ox: view.x, oy: view.y, moved: false };
  VP.classList.add("panning");
  VP.setPointerCapture(e.pointerId);
});
VP.addEventListener("pointermove", (e) => {
  if (!pan) return;
  const dx = e.clientX - pan.sx, dy = e.clientY - pan.sy;
  if (Math.abs(dx) + Math.abs(dy) > 3) pan.moved = true;
  view.x = pan.ox + dx; view.y = pan.oy + dy;
  applyView();
});
VP.addEventListener("pointerup", (e) => {
  if (!pan) return;
  const moved = pan.moved;
  pan = null;
  VP.classList.remove("panning");
  if (!moved) setFocus(null);                     // plain click on empty space deselects
  else scheduleViewportSave();
});

/* ------------------------------------------------------------------ focus */
const focusPill = document.getElementById("focus-pill");
const focusLabel = document.getElementById("focus-label");

function setFocus(id) {
  if (id === selectedId) return;
  if (selectedId && cards.has(selectedId)) cards.get(selectedId).el.classList.remove("selected");
  selectedId = id && cards.has(id) ? id : null;
  if (selectedId) {
    const d = cards.get(selectedId).data;
    cards.get(selectedId).el.classList.add("selected");
    focusLabel.textContent = `${d.kind} · ${d.title || TITLES[d.kind] || "untitled"}`;
    focusPill.hidden = false;
  } else {
    focusPill.hidden = true;
  }
  send({ type: "canvas_focus", id: selectedId });
}

/* --------------------------------------------------------------- card lifecycle */
function bringToFront(entry) { world.appendChild(entry.el); }

function updateEmpty() { emptyEl.classList.toggle("hidden", cards.size > 0); }

function makeCard(d) {
  const el = document.createElement("div");
  el.className = `card kind-${d.kind}`;
  el.dataset.id = d.id;
  el.style.left = d.x + "px"; el.style.top = d.y + "px";
  el.style.width = d.w + "px"; el.style.height = d.h + "px";
  el.innerHTML =
    '<div class="card-bar"><span class="kind-chip"></span><span class="card-title"></span>' +
    '<button class="card-x" title="Remove">×</button></div>' +
    '<div class="card-body"></div><div class="resize" title="Resize"></div>';
  el.querySelector(".kind-chip").textContent = d.kind;
  el.querySelector(".card-title").textContent = d.title || TITLES[d.kind] || "untitled";
  const body = el.querySelector(".card-body");
  world.appendChild(el);
  const entry = { el, body, data: d, chart: null };
  cards.set(d.id, entry);
  wireCard(entry);
  renderBody(entry);
  updateEmpty();
  return entry;
}

function removeCard(id, fromUser) {
  const entry = cards.get(id);
  if (!entry) return;
  if (entry.chart) { try { entry.chart.destroy(); } catch (_) {} }
  entry.el.remove();
  cards.delete(id);
  if (selectedId === id) { selectedId = null; focusPill.hidden = true; send({ type: "canvas_focus", id: null }); }
  if (fromUser) send({ type: "canvas_remove", id });
  updateEmpty();
}

function wireCard(entry) {
  const { el, data } = entry;
  el.addEventListener("pointerdown", (e) => {     // interacting with a card selects it (and never pans)
    e.stopPropagation();
    bringToFront(entry);
    setFocus(data.id);
  });
  const x = el.querySelector(".card-x");
  x.addEventListener("pointerdown", (e) => e.stopPropagation());
  x.addEventListener("click", (e) => { e.stopPropagation(); removeCard(data.id, true); });
  dragHandle(entry, el.querySelector(".card-bar"), "move");
  dragHandle(entry, el.querySelector(".resize"), "resize");
}

function dragHandle(entry, handle, mode) {
  handle.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.stopPropagation(); e.preventDefault();
    const d = entry.data;
    const start = { mx: e.clientX, my: e.clientY, x: d.x, y: d.y, w: d.w, h: d.h };
    entry.el.classList.add(mode === "move" ? "dragging" : "resizing");
    bringToFront(entry); setFocus(d.id);
    const onMove = (ev) => {
      const dx = (ev.clientX - start.mx) / view.zoom, dy = (ev.clientY - start.my) / view.zoom;
      if (mode === "move") {
        d.x = start.x + dx; d.y = start.y + dy;
        entry.el.style.left = d.x + "px"; entry.el.style.top = d.y + "px";
      } else {
        d.w = Math.max(200, start.w + dx); d.h = Math.max(130, start.h + dy);
        entry.el.style.width = d.w + "px"; entry.el.style.height = d.h + "px";
      }
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      entry.el.classList.remove("dragging", "resizing");
      sendUpdate(entry.data);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  });
}

/* ----------------------------------------------------------- smart placement */
function placeNew(d) {
  const rect = VP.getBoundingClientRect();
  const cx = (rect.width / 2 - view.x) / view.zoom;
  const cy = (rect.height / 2 - view.y) / view.zoom;
  let x = cx - d.w / 2, y = cy - d.h / 2;
  const others = [...cards.values()].map((e) => e.data).filter((o) => o.id !== d.id);
  const pad = 26;
  const hits = (rx, ry) => others.some((o) =>
    rx < o.x + o.w + pad && rx + d.w + pad > o.x && ry < o.y + o.h + pad && ry + d.h + pad > o.y);
  if (hits(x, y)) {
    const step = 64;
    outer:
    for (let r = 1; r <= 48; r++) {
      for (let a = 0; a < 8; a++) {
        const ang = (a * Math.PI) / 4;
        const nx = x + Math.cos(ang) * r * step, ny = y + Math.sin(ang) * r * step;
        if (!hits(nx, ny)) { x = nx; y = ny; break outer; }
      }
    }
  }
  d.x = Math.round(x); d.y = Math.round(y);
}

/* ------------------------------------------------------------------ body render */
function escapeHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function mdToHtml(src) {
  const lines = String(src).replace(/\r\n/g, "\n").split("\n");
  const inline = (t) =>
    escapeHtml(t)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>")
      .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  let html = "", inCode = false, inList = false;
  for (const raw of lines) {
    if (raw.trim().startsWith("```")) {
      if (inCode) { html += "</code></pre>"; inCode = false; }
      else { if (inList) { html += "</ul>"; inList = false; } html += "<pre><code>"; inCode = true; }
      continue;
    }
    if (inCode) { html += escapeHtml(raw) + "\n"; continue; }
    const h = raw.match(/^(#{1,4})\s+(.*)$/);
    const li = raw.match(/^\s*[-*]\s+(.*)$/);
    if (h) { if (inList) { html += "</ul>"; inList = false; } html += `<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`; continue; }
    if (li) { if (!inList) { html += "<ul>"; inList = true; } html += `<li>${inline(li[1])}</li>`; continue; }
    if (!raw.trim()) { if (inList) { html += "</ul>"; inList = false; } continue; }
    if (inList) { html += "</ul>"; inList = false; }
    html += `<p>${inline(raw)}</p>`;
  }
  if (inCode) html += "</code></pre>";
  if (inList) html += "</ul>";
  return html;
}

function colorize(ds, i, type) {
  const cat = type === "pie" || type === "doughnut";
  const c = CHART_PALETTE[i % CHART_PALETTE.length];
  return Object.assign({
    backgroundColor: cat ? CHART_PALETTE : type === "line" ? c + "33" : c + "cc",
    borderColor: cat ? "#0e1014" : c,
    borderWidth: cat ? 2 : 1.5,
    pointBackgroundColor: c,
    tension: 0.3,
    fill: type === "line",
  }, ds);
}

function chartOptions(type) {
  const grid = { color: "rgba(255,255,255,0.06)" };
  const ticks = { color: "#98a0ad", font: { family: "JetBrains Mono" } };
  const cat = type === "pie" || type === "doughnut";
  return {
    responsive: true,
    maintainAspectRatio: false,
    plugins: { legend: { labels: { color: "#aeb6c2", font: { family: "Inter, system-ui" } } } },
    scales: cat ? {} : { x: { grid, ticks }, y: { grid, ticks } },
  };
}

async function renderBody(entry) {
  const { body, data } = entry;
  const kind = (data.kind || "markdown").toLowerCase();
  const content = data.content || "";
  body.innerHTML = "";
  try {
    if (kind === "mermaid") {
      const { svg } = await mermaid.render("mmd-" + ++seq, content);
      body.innerHTML = svg;
    } else if (kind === "chart") {
      const cfg = JSON.parse(content || "{}");
      const wrap = document.createElement("div");
      wrap.className = "chart-wrap";
      const cv = document.createElement("canvas");
      wrap.appendChild(cv);
      body.appendChild(wrap);
      entry.chart = new Chart(cv.getContext("2d"), {
        type: cfg.type || "bar",
        data: { labels: cfg.labels || [], datasets: (cfg.datasets || []).map((ds, i) => colorize(ds, i, cfg.type)) },
        options: chartOptions(cfg.type),
      });
    } else if (kind === "stats") {
      const items = JSON.parse(content || "[]");
      const g = document.createElement("div");
      g.className = "stat-grid";
      for (const it of items) {
        const s = document.createElement("div");
        s.className = "stat";
        s.innerHTML = '<div class="stat-val"></div><div class="stat-label"></div><div class="stat-hint"></div>';
        s.querySelector(".stat-val").textContent = it.value ?? "";
        s.querySelector(".stat-label").textContent = it.label ?? "";
        s.querySelector(".stat-hint").textContent = it.hint ?? "";
        g.appendChild(s);
      }
      body.appendChild(g);
    } else if (kind === "image") {
      const img = document.createElement("img");
      img.className = "canvas-img"; img.alt = ""; img.src = content;
      body.appendChild(img);
    } else {
      body.innerHTML = mdToHtml(content);
    }
  } catch (err) {
    body.innerHTML = '<div class="card-err"></div>';
    body.querySelector(".card-err").textContent = `Couldn't render ${kind}: ${err && err.message ? err.message : err}`;
  }
}

/* ----------------------------------------------------------------- inbound */
function onBoard(p) {
  for (const id of [...cards.keys()]) removeCard(id, false);
  if (p && p.viewport) { view = { x: p.viewport.x || 0, y: p.viewport.y || 0, zoom: p.viewport.zoom || 1 }; }
  applyView();
  const items = (p && p.items) || [];
  for (const d of items) if (d.placed) makeCard(d);          // saved layout first
  for (const d of items) if (!d.placed) {                    // then smart-place the rest
    placeNew(d); makeCard(d); sendUpdate(d);
  }
  updateEmpty();
}

function onAdd(d) {
  if (cards.has(d.id)) return;
  if (!d.placed) placeNew(d);
  const entry = makeCard(d);
  bringToFront(entry);
  if (!d.placed) sendUpdate(d); // report our chosen placement so the server saves it
}

function onUpdate(d) {
  const entry = cards.get(d.id);
  if (!entry) return;
  Object.assign(entry.data, { x: d.x, y: d.y, w: d.w, h: d.h });
  entry.el.style.left = d.x + "px"; entry.el.style.top = d.y + "px";
  entry.el.style.width = d.w + "px"; entry.el.style.height = d.h + "px";
}

function handle(msg) {
  if (!msg || !msg.type) return;
  switch (msg.type) {
    case "board": onBoard(msg.data); break;
    case "canvas_add": onAdd(msg.data); break;
    case "canvas_update": onUpdate(msg.data); break;
    case "canvas_remove": removeCard(msg.data && msg.data.id, false); break;
    case "canvas_clear": for (const id of [...cards.keys()]) removeCard(id, false); break;
    default: break; // snapshot/status/level/etc. belong to the HUD page
  }
}

/* ---------------------------------------------------------------- websocket */
const connEl = document.getElementById("conn");
const connText = document.getElementById("conn-text");
function setConn(on) {
  connEl.className = `chip conn ${on ? "on" : "off"}`;
  connText.textContent = on ? "online" : "offline";
}
function connect() {
  ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen = () => setConn(true);
  ws.onclose = () => { setConn(false); setTimeout(connect, 1500); };
  ws.onerror = () => ws.close();
  ws.onmessage = (e) => { try { handle(JSON.parse(e.data)); } catch (_) {} };
}

/* ------------------------------------------------------------------- controls */
document.getElementById("zoom-in").addEventListener("click", () => zoomAt(view.zoom * 1.2));
document.getElementById("zoom-out").addEventListener("click", () => zoomAt(view.zoom / 1.2));
document.getElementById("zoom-level").addEventListener("click", () => zoomAt(1));
document.getElementById("fit").addEventListener("click", fit);
document.getElementById("home").addEventListener("click", home);
document.getElementById("clear").addEventListener("click", () => {
  send({ type: "canvas_clear" });
  for (const id of [...cards.keys()]) removeCard(id, false);
});
document.getElementById("focus-clear").addEventListener("click", () => setFocus(null));

applyView();
connect();
