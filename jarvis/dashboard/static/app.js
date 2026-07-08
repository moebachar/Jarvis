import * as THREE from "three";

/* ================================================================= 3D core ==
   Rendered into its own panel (#bulb-panel); a ResizeObserver keeps it sized. */
const canvas = document.getElementById("scene");
const bulbPanel = document.getElementById("bulb-panel");
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(55, 1, 0.1, 100);
camera.position.set(0, 0, 8);

function resize() {
  const w = bulbPanel.clientWidth || 1;
  const h = bulbPanel.clientHeight || 1;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
new ResizeObserver(resize).observe(bulbPanel);
window.addEventListener("resize", resize);
resize();

function makeSprite() {
  const s = 64;
  const c = document.createElement("canvas");
  c.width = c.height = s;
  const g = c.getContext("2d");
  const grad = g.createRadialGradient(s / 2, s / 2, 0, s / 2, s / 2, s / 2);
  grad.addColorStop(0, "rgba(255,255,255,1)");
  grad.addColorStop(0.35, "rgba(255,255,255,0.55)");
  grad.addColorStop(1, "rgba(255,255,255,0)");
  g.fillStyle = grad;
  g.fillRect(0, 0, s, s);
  return new THREE.CanvasTexture(c);
}
const sprite = makeSprite();

const COUNT = 4200;
const R = 2.4;
const baseArr = new Float32Array(COUNT * 3);
const posArr = new Float32Array(COUNT * 3);
const phase = new Float32Array(COUNT);
for (let i = 0; i < COUNT; i++) {
  const k = i + 0.5;
  const ph = Math.acos(1 - (2 * k) / COUNT);
  const th = Math.PI * (1 + Math.sqrt(5)) * k;
  const x = Math.sin(ph) * Math.cos(th);
  const y = Math.sin(ph) * Math.sin(th);
  const z = Math.cos(ph);
  baseArr[i * 3] = x * R; baseArr[i * 3 + 1] = y * R; baseArr[i * 3 + 2] = z * R;
  posArr[i * 3] = x * R; posArr[i * 3 + 1] = y * R; posArr[i * 3 + 2] = z * R;
  phase[i] = Math.random() * Math.PI * 2;
}
const sphereGeo = new THREE.BufferGeometry();
sphereGeo.setAttribute("position", new THREE.BufferAttribute(posArr, 3));
const sphereMat = new THREE.PointsMaterial({
  size: 0.05, map: sprite, transparent: true,
  blending: THREE.AdditiveBlending, depthWrite: false, color: 0x38d4e6,
});
const sphere = new THREE.Points(sphereGeo, sphereMat);
scene.add(sphere);

const rings = [];
function makeRing(radius, count, color, tilt) {
  const p = new Float32Array(count * 3);
  for (let i = 0; i < count; i++) {
    const a = (i / count) * Math.PI * 2;
    p[i * 3] = Math.cos(a) * radius;
    p[i * 3 + 1] = (Math.random() - 0.5) * 0.06;
    p[i * 3 + 2] = Math.sin(a) * radius;
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.BufferAttribute(p, 3));
  const m = new THREE.PointsMaterial({
    size: 0.045, map: sprite, transparent: true,
    blending: THREE.AdditiveBlending, depthWrite: false, color,
  });
  const pts = new THREE.Points(g, m);
  pts.rotation.x = tilt;
  scene.add(pts);
  rings.push({ pts, speed: (0.08 + Math.random() * 0.22) * (Math.random() < 0.5 ? 1 : -1) });
}
makeRing(3.25, 260, 0x38d4e6, Math.PI * 0.5);
makeRing(3.7, 220, 0xe0a24a, Math.PI * 0.34);
makeRing(2.95, 180, 0x2a8b98, Math.PI * 0.64);

const coreMat = new THREE.SpriteMaterial({
  map: sprite, color: 0x38d4e6, transparent: true,
  blending: THREE.AdditiveBlending, depthWrite: false,
});
const core = new THREE.Sprite(coreMat);
core.scale.set(1.8, 1.8, 1.8);
scene.add(core);

const STATE_COLOR = {
  idle: 0x2a8b98, listening: 0x38d4e6, thinking: 0xe0a24a,
  speaking: 0xf2bd63, working: 0x47c78a, sleeping: 0x33454a,
};
const STATE_BASE = {
  idle: 0.05, listening: 0.28, thinking: 0.18, speaking: 0.42, working: 0.22, sleeping: 0.02,
};

let currentState = "idle";
let energy = 0.05;
let targetEnergy = 0.05;
const targetColor = new THREE.Color(STATE_COLOR.idle);
const curColor = new THREE.Color(STATE_COLOR.idle);

const clock = new THREE.Clock();
function animate() {
  requestAnimationFrame(animate);
  const t = clock.getElapsedTime();

  const base = STATE_BASE[currentState] ?? 0.05;
  targetEnergy = Math.max(targetEnergy * 0.9, base);
  energy += (targetEnergy - energy) * 0.18;

  curColor.lerp(targetColor, 0.06);
  sphereMat.color.copy(curColor);
  coreMat.color.copy(curColor);

  const arr = sphereGeo.attributes.position.array;
  const amp = 0.08 + energy * 1.1;
  for (let i = 0; i < COUNT; i++) {
    const i3 = i * 3;
    const d = 1 + (Math.sin(t * 2.1 + phase[i]) * 0.06 + 0.06) * amp;
    arr[i3] = baseArr[i3] * d;
    arr[i3 + 1] = baseArr[i3 + 1] * d;
    arr[i3 + 2] = baseArr[i3 + 2] * d;
  }
  sphereGeo.attributes.position.needsUpdate = true;
  sphere.rotation.y = t * 0.05;
  sphere.rotation.x = Math.sin(t * 0.1) * 0.15;
  for (const r of rings) r.pts.rotation.z += r.speed * 0.01;

  core.scale.setScalar(1.3 + energy * 2.2);
  coreMat.opacity = 0.5 + energy * 0.5;
  renderer.render(scene, camera);
}
animate();

/* =============================================================== helpers === */
function setText(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value != null && String(value).length ? value : "—";
}
function shortModel(m) {
  if (!m) return "—";
  return String(m).replace("claude-", "").replace(/-\d{8}$/, "");
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
/* Wrap bare http(s) URLs in an anchor that opens a NEW tab (never replaces the HUD).
   Operates on already-escaped text; `&amp;` in a URL decodes correctly inside an href. */
function linkify(escaped) {
  return escaped.replace(/(https?:\/\/[^\s<]+[^\s<.,)\]}"'!?:;])/g,
    (u) => `<a class="link" href="${u}" target="_blank" rel="noopener noreferrer">${u}</a>`);
}
/* Linkify bare URLs while leaving any existing <a>…</a> anchors untouched. */
function linkifyLoose(html) {
  return html.split(/(<a\b[^>]*>.*?<\/a>)/g)
    .map((part) => (part.startsWith("<a") ? part : linkify(part)))
    .join("");
}

const stateEl = document.getElementById("state");
const detailEl = document.getElementById("detail");
function setState(s) {
  if (!s || s === currentState) return;
  currentState = s;
  stateEl.textContent = `‹ ${s.toUpperCase()} ›`;
  stateEl.dataset.state = s;
  targetColor.set(STATE_COLOR[s] ?? 0x38d4e6);
}

/* ------------------------------------------------------- live CLI stream --- */
const feedEl = document.getElementById("feed");
function feedLine(kind, glyph, htmlBody) {
  const now = new Date();
  const ts = `${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}:${String(now.getSeconds()).padStart(2, "0")}`;
  const line = document.createElement("div");
  line.className = `fl ${kind}`;
  line.innerHTML = `<span class="ts">${ts}</span><span class="gl">${glyph}</span>${htmlBody}`;
  const atBottom = feedEl.scrollHeight - feedEl.scrollTop - feedEl.clientHeight < 40;
  feedEl.appendChild(line);
  while (feedEl.childElementCount > 300) feedEl.firstElementChild.remove();
  if (atBottom) feedEl.scrollTop = feedEl.scrollHeight;
}
function feedText(kind, glyph, text) {
  if (!text) return;
  feedLine(kind, glyph, linkify(escapeHtml(text)));
}
function feedAction(data) {
  const parts = (data.summary || data.tool || "").split(" · ");
  const name = (parts[0] || "tool").toUpperCase();
  const value = parts.slice(1).join(" · ");
  const nm = name.length < 8 ? name.padEnd(8) : name + " ";
  feedLine("action", "··", `<span class="tool">${escapeHtml(nm)}</span>${escapeHtml(value)}`);
}

/* ---------------------------------------------------------- session stats -- */
function applyStatus(d) {
  if (!d) return;
  if (d.state) setState(d.state);
  detailEl.textContent = d.detail || "standing by";
  setText("m-model", shortModel(d.model));
  setText("m-session", d.session_id ? String(d.session_id).slice(0, 8) : null);
  if (typeof d.tools_used === "number") setText("m-tools", d.tools_used);
}

const started = Date.now();
setInterval(() => {
  const s = Math.floor((Date.now() - started) / 1000);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
  setText("m-uptime", h ? `${h}:${String(m).padStart(2, "0")}:${String(ss).padStart(2, "0")}`
                         : `${m}:${String(ss).padStart(2, "0")}`);
}, 1000);

/* =========================================================== canvas preview */
if (window.mermaid) {
  try {
    window.mermaid.initialize({
      startOnLoad: false, securityLevel: "strict", theme: "dark",
      themeVariables: { fontFamily: "JetBrains Mono, monospace", primaryColor: "#0c1a1e",
        primaryBorderColor: "#38d4e6", lineColor: "#e0a24a", primaryTextColor: "#bcd2d6" },
    });
  } catch (_) { /* ignore */ }
}
const CHART_PALETTE = ["#38d4e6", "#e0a24a", "#47c78a", "#f2bd63", "#2a8b98", "#bcd2d6"];
const previewEl = document.getElementById("preview");
const previewTitle = document.getElementById("preview-title");
let previewChart = null;
let previewSeq = 0;

// The mini canvas viewer: the full list of cards, browsed one at a time (prev/next),
// replacing the old full /canvas board page. The shown card is also "focused" so a turn
// like "make this bigger" resolves to it.
let canvasItems = [];
let canvasIdx = -1;
let lastFocusId = undefined;

function sendFocus(item) {
  const id = item ? (item.id || null) : null;
  if (id === lastFocusId) return;
  lastFocusId = id;
  send({ type: "canvas_focus", id });
}

function renderCurrent() {
  const n = canvasItems.length;
  const countEl = document.getElementById("cv-count");
  const prevB = document.getElementById("cv-prev");
  const nextB = document.getElementById("cv-next");
  const delB = document.getElementById("cv-del");
  if (n === 0) {
    canvasIdx = -1;
    showEmptyPreview();
    countEl.textContent = "0 / 0";
    prevB.disabled = nextB.disabled = delB.disabled = true;
    sendFocus(null);
    return;
  }
  canvasIdx = Math.max(0, Math.min(canvasIdx, n - 1));
  const item = canvasItems[canvasIdx];
  renderPreview(item);
  countEl.textContent = `${canvasIdx + 1} / ${n}`;
  prevB.disabled = canvasIdx === 0;
  nextB.disabled = canvasIdx === n - 1;
  delB.disabled = false;
  sendFocus(item);
}

document.getElementById("cv-prev").addEventListener("click", () => {
  if (canvasIdx > 0) { canvasIdx--; renderCurrent(); }
});
document.getElementById("cv-next").addEventListener("click", () => {
  if (canvasIdx < canvasItems.length - 1) { canvasIdx++; renderCurrent(); }
});
document.getElementById("cv-del").addEventListener("click", () => {
  if (canvasIdx < 0) return;
  const item = canvasItems[canvasIdx];
  if (item && item.id) send({ type: "canvas_remove", id: item.id });
  canvasItems.splice(canvasIdx, 1);
  renderCurrent();
});

function mdToHtml(src) {
  const lines = String(src).split("\n"); let html = ""; let inList = false;
  for (let ln of lines) {
    let s = escapeHtml(ln)
      .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
      .replace(/\*(.+?)\*/g, "<i>$1</i>")
      .replace(/`(.+?)`/g, "<code>$1</code>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,   // [text](url) markdown links
        (m, t, u) => `<a class="link" href="${u}" target="_blank" rel="noopener noreferrer">${t}</a>`);
    s = linkifyLoose(s);  // + bare URLs, leaving the anchors above intact
    if (/^#\s/.test(ln)) { if (inList) { html += "</ul>"; inList = false; } html += `<h1>${s.slice(2)}</h1>`; }
    else if (/^##\s/.test(ln)) { if (inList) { html += "</ul>"; inList = false; } html += `<h2>${s.slice(3)}</h2>`; }
    else if (/^[-*]\s/.test(ln)) { if (!inList) { html += "<ul>"; inList = true; } html += `<li>${s.slice(2)}</li>`; }
    else if (ln.trim() === "") { if (inList) { html += "</ul>"; inList = false; } }
    else html += `<div>${s}</div>`;
  }
  if (inList) html += "</ul>";
  return html;
}

function clearPreview() {
  if (previewChart) { try { previewChart.destroy(); } catch (_) {} previewChart = null; }
  previewEl.innerHTML = "";
}

async function renderPreview(item) {
  if (!item) return;
  previewTitle.textContent = item.title || "";
  const seq = ++previewSeq;
  clearPreview();
  const kind = item.kind, content = item.content || "";
  try {
    if (kind === "mermaid" && window.mermaid) {
      const { svg } = await window.mermaid.render(`prev-${seq}`, content);
      if (seq !== previewSeq) return;
      previewEl.innerHTML = svg;
    } else if (kind === "chart" && window.Chart) {
      const spec = JSON.parse(content);
      const cv = document.createElement("canvas");
      previewEl.appendChild(cv);
      (spec.datasets || (spec.data && spec.data.datasets) || []).forEach((ds, i) => {
        const c = CHART_PALETTE[i % CHART_PALETTE.length];
        if (ds.borderColor == null) ds.borderColor = c;
        if (ds.backgroundColor == null) ds.backgroundColor = c + "aa";
      });
      previewChart = new window.Chart(cv, {
        type: spec.type || "bar",
        data: { labels: spec.labels || [], datasets: spec.datasets || [] },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { labels: { color: "#557077", font: { size: 9 } } } },
          scales: { x: { ticks: { color: "#557077", font: { size: 9 } }, grid: { color: "rgba(85,112,119,0.15)" } },
                    y: { ticks: { color: "#557077", font: { size: 9 } }, grid: { color: "rgba(85,112,119,0.15)" } } },
        },
      });
    } else if (kind === "stats") {
      const arr = JSON.parse(content);
      const grid = document.createElement("div");
      grid.className = "preview-stats";
      for (const s of arr) {
        const cell = document.createElement("div");
        cell.className = "preview-stat";
        cell.innerHTML = `<div class="pv"></div><div class="pk"></div>`;
        cell.querySelector(".pv").textContent = s.value ?? "";
        cell.querySelector(".pk").textContent = s.label ?? "";
        grid.appendChild(cell);
      }
      previewEl.appendChild(grid);
    } else if (kind === "image") {
      const img = document.createElement("img");
      img.src = content;
      previewEl.appendChild(img);
    } else {
      const div = document.createElement("div");
      div.className = "preview-md";
      div.innerHTML = mdToHtml(content);
      previewEl.appendChild(div);
    }
  } catch (err) {
    if (seq !== previewSeq) return;
    previewEl.innerHTML = `<div class="preview-empty">couldn't render this ${escapeHtml(kind || "item")}</div>`;
  }
}

function showEmptyPreview() {
  previewTitle.textContent = "";
  clearPreview();
  previewEl.innerHTML = `<div class="preview-empty">no render yet — ask jarvis to "show" something</div>`;
}

/* =============================================================== dispatch === */
function handle(msg) {
  const { type, data } = msg;
  switch (type) {
    case "snapshot":
    case "status": applyStatus(data); break;
    case "level": targetEnergy = Math.max(targetEnergy, Math.min(1, data.value || 0)); break;
    case "prompt": feedText("you", "››", data.text); break;
    case "action": feedAction(data); break;
    case "reply": feedText("reply", "‹‹", data.text); break;
    case "notify": feedText("note", "**", data.message); break;
    case "error": feedText("err", "!!", `[${data.where || "?"}] ${data.message || ""}`); break;
    case "voice": feedText("sys", "--", `${data.event}${data.threshold != null ? ` (thr ${Math.round(data.threshold)})` : ""}`); break;
    case "sleep": feedText("sys", "--", `sleeping — ${data.reason || ""}${data.minutes ? ` (~${data.minutes}m)` : ""}`); break;
    case "usage": {
      const inp = data.input_tokens ?? data.tokens_in;
      const out = data.output_tokens ?? data.tokens_out;
      if (inp != null) setText("m-tin", inp);
      if (out != null) setText("m-tout", out);
      break;
    }
    case "board": {
      canvasItems = ((data && data.items) || []).slice();
      canvasIdx = canvasItems.length - 1;   // start on the latest
      renderCurrent();
      break;
    }
    case "canvas_add": {
      canvasItems.push(data);
      canvasIdx = canvasItems.length - 1;   // jump to the new card
      renderCurrent();
      break;
    }
    case "canvas_remove": {
      const i = canvasItems.findIndex((it) => it.id === (data && data.id));
      if (i >= 0) { canvasItems.splice(i, 1); if (canvasIdx > i) canvasIdx--; renderCurrent(); }
      break;
    }
    case "canvas_clear": canvasItems = []; canvasIdx = -1; renderCurrent(); break;
    case "audio_mode": if (data && data.remote) enableRemoteVoice(); break;
    case "tts_start": ttsStart(data); break;
    case "tts_end": ttsEnd(); break;
    case "tts_flush": flushPlayback(); break;
    default: break;
  }
}

/* ---------------------------------------------------------------- websocket */
const connEl = document.getElementById("conn");
const connText = document.getElementById("conn-text");
function setConn(online) {
  connEl.className = `conn ${online ? "on" : "off"}`;
  connText.textContent = online ? "online" : "offline";
}
let ws = null;
function send(obj) {
  if (ws && ws.readyState === 1) { try { ws.send(JSON.stringify(obj)); } catch (_) { /* ignore */ } }
}
function connect() {
  ws = new WebSocket(`ws://${location.host}/ws`);
  ws.binaryType = "arraybuffer";
  ws.onopen = () => {
    setConn(true);
    startHeartbeat();
    if (audioEnabled) send({ type: "audio_hello" });   // reconnect: re-claim the audio slot
  };
  ws.onclose = () => {
    setConn(false); lastFocusId = undefined; stopHeartbeat();
    releasePTT();                                       // don't leave PTT stuck "down" on a drop
    setTimeout(connect, 1500);
  };
  ws.onerror = () => ws.close();
  ws.onmessage = (e) => {
    if (typeof e.data !== "string") { onTtsChunk(e.data); return; }  // binary frame = TTS PCM
    try { handle(JSON.parse(e.data)); } catch (_) { /* ignore */ }
  };
}

/* ============================================ remote / tunneled voice ======= *
 * When this process runs with transport="browser", THIS tab is Jarvis's mic and
 * speaker: we capture the mic (16 kHz Int16 PCM over the WebSocket), and play back
 * the TTS PCM the desktop streams to us. Push-to-talk = the on-screen button or the
 * space bar. Everything below is inert unless the server sends `audio_mode.remote`.  */
let remoteVoice = false;      // server said this is a remote-voice session
let audioEnabled = false;     // this tab can actually be the mic + speaker (secure context ok)
let pttDown = false;          // mic is currently streaming
let pttWanted = false;        // intent: the button/key is held (may still be setting the mic up)
let capCtx = null, workletNode = null, micSetup = null;
let playCtx = null, playGain = null, OUT_RATE = 24000, nextTime = 0, revertTimer = null;
let ttsFlushed = false;       // a barge-in flushed playback; drop straggler PCM until the next utterance
const liveSources = [];
let hbTimer = null;

function startHeartbeat() { stopHeartbeat(); hbTimer = setInterval(() => send({ type: "ping" }), 15000); }
function stopHeartbeat() { if (hbTimer) { clearInterval(hbTimer); hbTimer = null; } }

function setMic(on, text) {
  const el = document.getElementById("mic");
  if (!el) return;
  el.hidden = false;
  el.className = `conn ${on ? "on" : "off"}`;
  const t = document.getElementById("mic-text");
  if (t) t.textContent = text;
}
function isTyping(e) {
  const t = e.target;
  return t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable);
}

function enableRemoteVoice() {
  if (remoteVoice) return;
  remoteVoice = true;
  const ptt = document.getElementById("ptt");
  const hint = document.getElementById("ptt-hint");
  const mic = document.getElementById("mic");
  if (ptt) ptt.hidden = false;
  if (hint) hint.hidden = false;
  if (mic) mic.hidden = false;

  if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    // getUserMedia only works in a secure context: https, or http://localhost (incl. an
    // SSH-forwarded port). A bare LAN IP leaves navigator.mediaDevices undefined.
    setMic(false, "mic blocked");
    if (ptt) { ptt.disabled = true; ptt.textContent = "MIC BLOCKED"; }
    if (hint) hint.textContent = "open http://localhost:<port> over the SSH tunnel — a LAN IP blocks the mic";
    return;
  }
  setMic(false, "tap to talk");
  // Claim the audio slot now, so the desktop can speak to this tab (a greeting or a presence
  // note) even before the first PTT press. Re-sent on every reconnect from ws.onopen.
  audioEnabled = true;
  send({ type: "audio_hello" });
  if (ptt) {
    ptt.addEventListener("pointerdown", (e) => { e.preventDefault(); pressPTT(); });
    ptt.addEventListener("pointercancel", () => releasePTT());
  }
  window.addEventListener("pointerup", () => releasePTT());
  window.addEventListener("blur", () => releasePTT());           // don't get stuck "down"
  window.addEventListener("keydown", (e) => {
    if (e.code === "Space" && !e.repeat && !isTyping(e)) { e.preventDefault(); pressPTT(); }
  });
  window.addEventListener("keyup", (e) => {
    if (e.code === "Space") { e.preventDefault(); releasePTT(); }
  });
}

function ensureMic() {
  if (micSetup) return micSetup;
  micSetup = (async () => {
    // Get the mic FIRST (permission + stream), then an AudioContext at the NATIVE device rate —
    // the worklet resamples to 16 kHz. Forcing the context to 16 kHz can stall getUserMedia on
    // real hardware (it forces an unusual rate on the output device too).
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    capCtx = new AudioContext();
    await capCtx.audioWorklet.addModule("/static/mic-worklet.js");
    const src = capCtx.createMediaStreamSource(stream);
    workletNode = new AudioWorkletNode(capCtx, "capture");
    src.connect(workletNode);
    // A worklet whose output goes nowhere isn't "pulled" by the graph in most browsers, so
    // process() never runs. Route it to the destination through a MUTED gain node: the graph
    // keeps it live, but nothing is audible (no mic monitoring / echo).
    const mute = capCtx.createGain();
    mute.gain.value = 0;
    workletNode.connect(mute);
    mute.connect(capCtx.destination);
    workletNode.port.onmessage = (ev) => onMicBlock(ev.data);
    setMic(true, "mic live");
  })().catch((err) => {
    console.error("[jarvis] mic setup failed", err);
    setMic(false, "mic denied");
    micSetup = null;                    // allow a retry on the next press
    throw err;
  });
  return micSetup;
}

function onMicBlock(ab) {
  const i16 = new Int16Array(ab);
  if (!i16.length || !pttDown) return;
  let sum = 0;
  for (let i = 0; i < i16.length; i++) { const s = i16[i] / 32768; sum += s * s; }
  targetEnergy = Math.max(targetEnergy, Math.min(1, Math.sqrt(sum / i16.length) * 3));
  if (ws && ws.readyState === 1) { try { ws.send(ab); } catch (_) { /* ignore */ } }
}

async function pressPTT() {
  if (pttDown) return;
  pttWanted = true;                       // record intent BEFORE the async mic-setup gap
  try { await ensureMic(); } catch (_) { pttWanted = false; return; }
  if (!pttWanted) return;                 // released during first-time mic setup — abort (no press sent)
  ensurePlayCtx();
  if (capCtx && capCtx.state === "suspended") capCtx.resume();
  if (playCtx && playCtx.state === "suspended") playCtx.resume();
  if (currentState === "speaking") flushPlayback();     // barge: kill local playback instantly
  pttDown = true;
  const ptt = document.getElementById("ptt");
  if (ptt) ptt.classList.add("live");
  setState("listening");
  send({ type: "ptt_press" });
}
function releasePTT() {
  pttWanted = false;                      // a release during mic setup must win (clear intent first)
  if (!pttDown) return;
  pttDown = false;
  const ptt = document.getElementById("ptt");
  if (ptt) ptt.classList.remove("live");
  send({ type: "ptt_release" });
}

/* ---- gapless TTS playback (schedule AudioBuffers back-to-back) ---- */
function ensurePlayCtx() {
  if (!playCtx) {
    playCtx = new AudioContext();
    playGain = playCtx.createGain();
    playGain.connect(playCtx.destination);
  }
  return playCtx;
}
function ttsStart(data) {
  OUT_RATE = (data && data.rate) || 24000;
  if (revertTimer) { clearTimeout(revertTimer); revertTimer = null; }
  ensurePlayCtx();
  if (playCtx.state === "suspended") playCtx.resume();
  // Re-arm playback after any prior barge-in (flushPlayback leaves the gain at ~0 and the
  // drop-flag set so stragglers were ignored; restore both for this fresh utterance).
  ttsFlushed = false;
  try { const t = playCtx.currentTime; playGain.gain.cancelScheduledValues(t); playGain.gain.setValueAtTime(1, t); } catch (_) { /* ignore */ }
  setState("speaking");
}
function onTtsChunk(ab) {
  if (ttsFlushed) return;                  // barge-in: drop in-flight PCM until the next tts_start
  const ctx = ensurePlayCtx();
  const i16 = new Int16Array(ab);
  if (!i16.length) return;
  const f32 = new Float32Array(i16.length);
  let sum = 0;
  for (let i = 0; i < i16.length; i++) { const s = i16[i] / 32768; f32[i] = s; sum += s * s; }
  targetEnergy = Math.max(targetEnergy, Math.min(1, Math.sqrt(sum / i16.length) * 3));
  const buf = ctx.createBuffer(1, f32.length, OUT_RATE);
  buf.copyToChannel(f32, 0);
  const node = ctx.createBufferSource();
  node.buffer = buf;
  node.connect(playGain);
  const now = ctx.currentTime;
  if (nextTime < now + 0.03) nextTime = now + 0.03;     // small lead; recover after any underrun
  node.start(nextTime);
  nextTime += buf.duration;
  liveSources.push(node);
  node.onended = () => { const i = liveSources.indexOf(node); if (i >= 0) liveSources.splice(i, 1); };
}
function ttsEnd() {
  // The desktop flips its own state to idle as soon as the last slice is SENT, but the tail is
  // still playing here — hold "speaking" until playback actually drains.
  const remainMs = playCtx ? Math.max(0, (nextTime - playCtx.currentTime) * 1000) : 0;
  if (revertTimer) clearTimeout(revertTimer);
  revertTimer = setTimeout(() => { if (currentState === "speaking") setState("idle"); }, remainMs + 150);
}
function flushPlayback() {
  if (playCtx && playGain) {
    ttsFlushed = true;                    // drop any straggler PCM that arrives before the next utterance
    const t = playCtx.currentTime;
    try {
      playGain.gain.cancelScheduledValues(t);
      playGain.gain.setValueAtTime(playGain.gain.value, t);
      playGain.gain.linearRampToValueAtTime(0.0001, t + 0.015);   // ≤20 ms fade — no click
    } catch (_) { /* ignore */ }
    for (const s of liveSources) { try { s.stop(t + 0.03); } catch (_) { /* ignore */ } }
    liveSources.length = 0;
    nextTime = 0;
    // NOTE: gain is deliberately left at ~0; the next ttsStart re-arms it. Restoring it here
    // would let a late straggler (in-flight before ttsFlushed took hold) blip at full volume.
  }
}

connect();
