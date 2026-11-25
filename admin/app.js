// PHOSPHOR2000 admin dashboard. Vanilla JS, no external resources.
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const SCENES = ["boot", "dance", "spectrum", "grid", "rain", "countdown"];
let S = null;            // latest status
let ws = null;
let clusterOffset = null; // cluster seconds - performance.now()/1000 (admin browser)
let syncSamples = [];
let audioCtx = null, lastClickK = null;

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v; else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null) e.setAttribute(k, v);
  }
  for (const k of kids) e.append(k instanceof Node ? k : document.createTextNode(k ?? ""));
  return e;
}

function toast(msg, ok = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast" + (ok ? " ok" : "");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.add("hidden"), 4000);
}

async function api(path, body, method = "POST") {
  const opt = { method, headers: { "X-PHOS": "1" }, credentials: "same-origin" };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  let data = null;
  try { data = await r.json(); } catch (e) {}
  if (r.status === 401) { showLogin(); throw new Error("login required"); }
  if (!r.ok) { const m = (data && data.detail) || r.statusText; toast(String(m)); throw new Error(m); }
  return data;
}
const cmd = (name, args = {}) => api(`/api/cmd/${name}`, args).then((r) => {
  if (r && r.forwarded_to) toast(`forwarded to leader ${r.forwarded_to}`, true);
  return r;
});

// ------------------------------------------------------------------ login
function showLogin() { $("#login").classList.remove("hidden"); $("#app").classList.add("hidden"); $("#logout").classList.add("hidden"); }
function showApp() { $("#login").classList.add("hidden"); $("#app").classList.remove("hidden"); $("#logout").classList.remove("hidden"); }

$("#loginForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    await api("/api/login", { password: $("#pw").value });
    $("#pw").value = "";
    start();
  } catch (e) { $("#loginMsg").textContent = e.message; }
});
$("#logout").addEventListener("click", async () => { await api("/api/logout", {}); if (ws) ws.close(); showLogin(); });

// ------------------------------------------------------------------- tabs
$$("#tabs button").forEach((b) => b.addEventListener("click", () => {
  $$("#tabs button").forEach((x) => x.classList.toggle("active", x === b));
  $$("[data-panel]").forEach((p) => p.classList.toggle("hidden", p.dataset.panel !== b.dataset.tab));
  if (b.dataset.tab === "logs" || b.dataset.tab === "calib") loadLogs();
  location.hash = b.dataset.tab;
}));

// -------------------------------------------------------------- websocket
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws/admin`);
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.t === "status") { S = m; render(); }
    else if (m.t === "sync") onSync(m);
  };
  ws.onclose = (ev) => { if (ev.code === 4401) showLogin(); else setTimeout(connect, 1500); };
  ws.onopen = () => { syncSamples = []; for (let i = 0; i < 6; i++) setTimeout(sendSync, i * 150); };
}
function sendSync() { if (ws && ws.readyState === 1) ws.send(JSON.stringify({ t: "sync", c: performance.now() })); }
setInterval(sendSync, 2000);
function onSync(m) {
  const c4 = performance.now();
  const rtt = c4 - m.c;
  syncSamples.push({ off: m.s - (m.c + c4) / 2000, rtt });
  if (syncSamples.length > 20) syncSamples.shift();
  const best = [...syncSamples].sort((a, b) => a.rtt - b.rtt).slice(0, 4);
  clusterOffset = best.map((x) => x.off).sort((a, b) => a - b)[best.length >> 1];
  window.__adminRtt = best[0].rtt;
}
const clusterNow = (perfMs = performance.now()) => clusterOffset === null ? null : perfMs / 1000 + clusterOffset;

// ----------------------------------------------------------------- render
const fmt = (x, d = 1) => (x === null || x === undefined || Number.isNaN(x)) ? "–" : Number(x).toFixed(d);
function render() {
  if (!S) return;
  const wall = new Date(S.wall * 1000);
  $("#hdr").innerHTML = "";
  $("#hdr").append(el("span", {}, "node "), el("b", {}, S.node), el("span", {}, `  ${S.state}  leader `),
    el("b", {}, S.leader || "none"), el("span", {}, `  epoch ${S.epoch}  ${wall.toLocaleTimeString()}`),
    S.is_leader ? "" : el("span", { class: "sub" }, "  (commands are forwarded to the leader)"));
  renderNodes(); renderTiming(); renderScenes(); renderCal(); renderConfig();
  $("#transitions").textContent = S.transitions.map((t) => `${new Date(t.wall * 1000).toLocaleTimeString()}  ${t.old} → ${t.new}  (${t.reason})`).join("\n");
}

function cls(ok, warn) { return ok ? "ok" : warn ? "warn" : "bad"; }
function renderNodes() {
  const t = $("#nodes");
  t.innerHTML = "";
  t.append(el("tr", {}, ...["node", "state", "ip", "uptime", "scene", "BPM", "src", "skew ppm", "jitter", "±unc", "audio", "fps", "cpu %", "temp", "offset", ""].map((h) => el("th", {}, h))));
  const ids = Object.keys(S.nodes).sort();
  for (const id of ids) {
    const n = S.nodes[id];
    const sy = n.sync || {}, tl = n.timeline || {}, au = n.audio || {}, rd = n.renderer || {}, me = n.metrics || {};
    const online = n.self || n.online;
    const audio = au.has_device ? (au.live ? "LIVE" : (au.state || "no signal")) : (n.has_audio ? "device" : "–");
    const actions = el("td", {});
    if (S.is_leader && !n.self && online && n.state === "FOLLOWER")
      actions.append(el("button", { class: "ghost", onclick: () => confirm(`Hand leadership to ${id}? The network drops for ~20-40 s.`) && cmd("handover", { target: id }) }, "make leader"));
    actions.append(el("button", { class: "ghost", onclick: () => cmd("restart", { target: id, what: "renderer" }) }, "restart renderer"));
    actions.append(el("button", { class: "danger", onclick: () => confirm(`Reboot ${id}?`) && cmd("restart", { target: id, what: "reboot" }) }, "reboot"));
    const off = (S.shared.calibration.offsets_ms || {})[id];
    t.append(el("tr", {},
      el("td", { class: online ? "ok" : "bad" }, id + (n.self ? " *" : "")),
      el("td", { class: cls(["LEADER", "FOLLOWER"].includes(n.state), true) }, n.state || "?"),
      el("td", {}, n.ip || (n.self ? location.hostname : "–")),
      el("td", {}, n.uptime ? `${Math.floor(n.uptime / 60)}m` : "–"),
      el("td", {}, n.scene || "–"),
      el("td", {}, fmt(tl.bpm, 2)), el("td", {}, tl.source || "–"),
      el("td", {}, n.state === "LEADER" ? "ref" : fmt(sy.skew_ppm, 1)),
      el("td", { class: sy.jitter_ms > 5 ? "warn" : "" }, fmt(sy.jitter_ms, 2)),
      el("td", { class: sy.uncertainty_ms > 10 ? "warn" : "" }, fmt(sy.uncertainty_ms, 1)),
      el("td", { class: au.live ? "ok" : "" }, audio),
      el("td", { class: rd.fps && rd.fps < 20 ? "warn" : "" }, fmt(rd.fps, 1)),
      el("td", {}, fmt(me.cpu_pct, 0)), el("td", {}, me.temp_c ? `${fmt(me.temp_c, 0)}°` : "–"),
      el("td", {}, off === undefined ? "0" : fmt(off, 1)), actions));
  }
}

function renderTiming() {
  const tl = S.timeline;
  $("#bpmBig").textContent = fmt(tl.bpm, 2);
  $("#srcLine").textContent = `${tl.source}  confidence ${fmt(tl.confidence * 100, 0)}%  rev ${tl.revision}` +
    (tl.audio_src ? `  audio from ${tl.audio_src}` : "") + (tl.manual ? "  [manual override]" : "");
  if (document.activeElement !== $("#bpmIn")) $("#bpmIn").value = fmt(tl.bpm, 1);
  const own = S.nodes[S.node];
  const au = (own && own.audio) || {};
  const kv = $("#audioInfo"); kv.innerHTML = "";
  const rows = [["device on this node", au.has_device ? "yes" : "no"], ["state", au.state || "–"], ["live", au.live ? "yes" : "no"],
    ["detected BPM", fmt(au.bpm, 2)], ["confidence", fmt(au.confidence, 2)], ["rms", fmt(au.rms, 3)], ["age s", fmt(au.age, 1)]];
  for (const [k, v] of rows) kv.append(el("span", {}, k), el("span", {}, v));
}
// beat dots animate from local prediction
function animate() {
  if (S) {
    const tl = S.timeline;
    const c = clusterNow();
    if (c !== null) {
      let b = tl.anchor_beat + (c - tl.anchor_time) * tl.bpm / 60;
      const k = ((Math.floor(b) % 4) + 4) % 4;
      $$("#beatDots i").forEach((d, i) => d.classList.toggle("on", i === k && b - Math.floor(b) < 0.3));
      clickTick(c);
    }
  }
  requestAnimationFrame(animate);
}

let scenesBuilt = false;
function renderScenes() {
  const sc = S.shared.scenes;
  const ids = Object.keys(S.nodes).sort();
  if (!scenesBuilt) {
    scenesBuilt = true;
    for (const s of SCENES) $("#sceneBtns").append(el("button", { onclick: () => {
      const t = $("#sceneTarget").value; const bars = parseInt($("#sceneBars").value, 10);
      cmd("scene", { scene: s, quantize: $("#quant").value, targets: t ? [t] : null, bars: bars > 0 ? bars : null });
    } }, s));
    loadProgramme();
  }
  const sel = $("#sceneTarget");
  const cur = sel.value;
  sel.innerHTML = ""; sel.append(el("option", { value: "" }, "all CRTs"));
  for (const id of ids) sel.append(el("option", { value: id }, id));
  sel.value = cur;
  $("#overrides").textContent = S.schedule.overrides.length
    ? "overrides: " + S.schedule.overrides.map((o) => `${o.scene}@${o.start_beat}${o.end_beat ? "-" + o.end_beat : "+"}${o.targets ? " " + o.targets.join(",") : ""}`).join("  ")
    : "no overrides";
  if (document.activeElement !== $("#intensity")) $("#intensity").value = sc.intensity;
}

function loadProgramme() {
  const sc = S.shared.scenes;
  $("#mode").value = sc.mode; $("#defQuant").value = sc.quantize;
  const t = $("#playlist"); t.innerHTML = "";
  t.append(el("tr", {}, el("th", {}, "scene"), el("th", {}, "bars"), el("th", {}, "")));
  for (const it of sc.playlist) addPlRow(it);
  $("#choreo").value = JSON.stringify(sc.choreography, null, 1);
  $("#cdTarget").value = sc.countdown_target; $("#cdLabel").value = sc.countdown_label;
  const ind = $("#indiv"); ind.innerHTML = "";
  for (const id of Object.keys(S.nodes).sort()) {
    const s = el("select", { "data-node": id }, el("option", { value: "" }, "rotate playlist"), ...SCENES.map((x) => el("option", { value: x }, x)));
    s.value = (sc.individual[id] && sc.individual[id].scene) || "";
    ind.append(el("label", {}, id, s));
  }
}
function addPlRow(it = { scene: "boot", bars: 8 }) {
  const s = el("select", {}, ...SCENES.map((x) => el("option", { value: x }, x)));
  s.value = it.scene;
  const b = el("input", { type: "number", min: 1, max: 1024, value: it.bars });
  const tr = el("tr", {}, el("td", {}, s), el("td", {}, b), el("td", {}, el("button", { class: "ghost", onclick: () => tr.remove() }, "✕")));
  $("#playlist").append(tr);
}
$("#plAdd").addEventListener("click", () => addPlRow());
$("#plSave").addEventListener("click", () => {
  const playlist = $$("#playlist tr").slice(1).map((tr) => ({ scene: $("select", tr).value, bars: parseInt($("input", tr).value, 10) }));
  const individual = {};
  $$("#indiv select").forEach((s) => { if (s.value) individual[s.dataset.node] = { scene: s.value }; });
  cmd("set_shared", { patch: { scenes: { mode: $("#mode").value, quantize: $("#defQuant").value, playlist, individual } } })
    .then(() => toast("programme applied at next bar", true));
});
$("#choreoSave").addEventListener("click", () => {
  let c; try { c = JSON.parse($("#choreo").value); } catch (e) { toast("invalid JSON"); return; }
  cmd("set_shared", { patch: { scenes: { choreography: c } } }).then(() => toast("choreography applied", true));
});
$("#clearScene").addEventListener("click", () => cmd("clear_scene", { quantize: $("#quant").value }));
$("#intensity").addEventListener("change", (e) => cmd("set_shared", { patch: { scenes: { intensity: parseFloat(e.target.value) } } }));
$("#cdSave").addEventListener("click", () => cmd("set_shared", { patch: { scenes: { countdown_target: $("#cdTarget").value, countdown_label: $("#cdLabel").value } } }));

// ----------------------------------------------------------------- timing
$("#bpmSet").addEventListener("click", () => cmd("set_bpm", { bpm: parseFloat($("#bpmIn").value) }));
$("#tap").addEventListener("pointerdown", () => cmd("tap", { t: performance.now() / 1000 }));
$$("[data-nudge]").forEach((b) => b.addEventListener("click", () => cmd("nudge", { ms: parseFloat(b.dataset.nudge) })));
$("#downbeat").addEventListener("click", () => cmd("downbeat"));
$("#manualOff").addEventListener("click", () => cmd("manual", { enabled: false }));

// ------------------------------------------------------------ calibration
function renderCal() {
  const ids = Object.keys(S.nodes).sort();
  const sel = $("#calTarget"); const cur = sel.value || (S.cal.target || ids[0]);
  sel.innerHTML = ""; for (const id of ids) sel.append(el("option", { value: id }, id)); sel.value = cur;
  const c = S.cal, r = c.result || {};
  const offs = S.shared.calibration.offsets_ms || {};
  const kv = $("#calStatus"); kv.innerHTML = "";
  const rows = [
    ["session", c.active ? `${c.mode} ${c.target || ""} @ ${c.bpm} BPM` : "inactive"],
    ["taps (valid)", `${c.tap_count}`], ["used / rejected", r.n_used === undefined ? "–" : `${r.n_used} / ${r.n_rejected}`],
    ["phase estimate", r.mean_ms == null ? "–" : `${r.mean_ms >= 0 ? "+" : ""}${fmt(r.mean_ms, 1)} ms (taps after flash = +)`],
    ["variability (circ. SD)", r.circ_std_ms == null ? "–" : `${fmt(r.circ_std_ms, 1)} ms`],
    ["uncertainty (SE)", r.std_err_ms == null ? "–" : `±${fmt(r.std_err_ms, 1)} ms`],
    ["confidence", r.confidence == null ? "–" : `${fmt(r.confidence * 100, 0)}%${r.ready ? "" : " (need more taps)"}`],
    ["suggested offset", r.suggested_offset_ms == null ? "–" : `${fmt(r.suggested_offset_ms, 1)} ms`],
    ["trial offset (live)", c.trial_offset_ms == null ? "–" : `${fmt(c.trial_offset_ms, 1)} ms`],
    ["saved offset", c.target ? `${fmt(offs[c.target] ?? 0, 1)} ms` : "–"],
    ["browser↔cluster RTT", window.__adminRtt ? `${fmt(window.__adminRtt, 1)} ms` : "–"],
  ];
  for (const [k, v] of rows) kv.append(el("span", {}, k), el("span", {}, v));
  if (r.notes && r.notes.length) kv.append(el("span", {}, "notes"), el("span", {}, r.notes.join("; ")));
  const t = $("#offsets"); t.innerHTML = "";
  t.append(el("tr", {}, el("th", {}, "CRT"), el("th", {}, "offset ms")));
  for (const id of ids) t.append(el("tr", {}, el("td", {}, id), el("td", {}, fmt(offs[id] ?? 0, 1))));
  const active = c.active && c.mode === "single";
  $$("[data-adj], #calSave, #calApply, #calTap").forEach((b) => (b.disabled = !active));
}
$("#calStart").addEventListener("click", () => cmd("cal_start", { mode: "single", target: $("#calTarget").value, bpm: parseFloat($("#calBpm").value) }));
$("#calAll").addEventListener("click", () => cmd("cal_start", { mode: "all", bpm: parseFloat($("#calBpm").value) }));
$("#calClick").addEventListener("click", () => cmd("cal_start", { mode: "click", bpm: parseFloat($("#calBpm").value) }));
$$("[data-adj]").forEach((b) => b.addEventListener("click", () => cmd("cal_adjust", { delta_ms: parseFloat(b.dataset.adj) })));
$("#calApply").addEventListener("click", () => cmd("cal_adjust", { apply_suggestion: true }));
$("#calSave").addEventListener("click", () => cmd("cal_save").then(() => { toast("offset saved & replicated", true); loadLogs(); }));
$("#calCancel").addEventListener("click", () => cmd("cal_cancel"));
$("#calReset").addEventListener("click", () => confirm("Reset offset to 0?") && cmd("cal_reset", { target: $("#calTarget").value }));
function calTap(ev) {
  const t = ev && ev.timeStamp ? ev.timeStamp : performance.now();   // event time, not handler time
  const c = clusterNow(t);
  if (c === null) { toast("clock not synced yet"); return; }
  cmd("cal_tap", { cluster: c, value: 1 });
}
$("#calTap").addEventListener("pointerdown", calTap);
document.addEventListener("keydown", (ev) => {
  const calVisible = !$("[data-panel=calib]").classList.contains("hidden");
  if (!calVisible || ev.key !== "Enter" || ev.repeat || ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) return;
  ev.preventDefault();
  calTap(ev);
});
// optional click in the admin browser (output latency of phones/Bluetooth is large and unknown!)
function clickTick(c) {
  if (!$("#clickHere").checked || !S.cal.active) return;
  try {
    if (!audioCtx) audioCtx = new AudioContext({ latencyHint: "interactive" });
    const period = 60 / S.cal.bpm;
    const k = Math.ceil((c - S.cal.origin) / period);
    if (k === lastClickK) return;
    const lead = S.cal.origin + k * period - c - (audioCtx.outputLatency || audioCtx.baseLatency || 0);
    if (lead < 0.03 || lead > 0.5) return;
    const at = audioCtx.currentTime + lead;
    const o = audioCtx.createOscillator(), g = audioCtx.createGain();
    o.frequency.value = 1000; g.gain.setValueAtTime(0.4, at); g.gain.exponentialRampToValueAtTime(0.001, at + 0.04);
    o.connect(g).connect(audioCtx.destination); o.start(at); o.stop(at + 0.05);
    lastClickK = k;
  } catch (e) {}
}

// ----------------------------------------------------------------- config
function renderConfig() {
  const sh = S.shared;
  const set = (id, v) => { if (document.activeElement !== $(id)) $(id).value = v; };
  set("#globOff", sh.timeline.global_visual_offset_ms); set("#dispLat", sh.timeline.display_latency_ms);
  set("#fpsCap", sh.display.fps_cap); set("#maxHz", sh.safety.max_flash_hz); set("#maxLum", sh.safety.max_flash_luminance);
  if (document.activeElement !== $("#reduced")) $("#reduced").checked = sh.safety.reduced_flashing;
}
$("#timingSave").addEventListener("click", () => cmd("set_shared", { patch: {
  timeline: { global_visual_offset_ms: parseFloat($("#globOff").value), display_latency_ms: parseFloat($("#dispLat").value) },
  display: { fps_cap: parseInt($("#fpsCap").value, 10) } } }).then(() => toast("saved", true)));
$("#safetySave").addEventListener("click", () => cmd("set_shared", { patch: { safety: {
  reduced_flashing: $("#reduced").checked, max_flash_hz: parseFloat($("#maxHz").value), max_flash_luminance: parseFloat($("#maxLum").value) } } })
  .then(() => toast("safety settings applied", true)));
$("#importFile").addEventListener("change", async (ev) => {
  const f = ev.target.files[0]; if (!f) return;
  const text = await f.text();
  try {
    const r = await fetch("/api/config/import", { method: "POST", headers: { "X-PHOS": "1", "Content-Type": "application/json" }, body: text });
    const d = await r.json();
    $("#cfgMsg").textContent = r.ok ? `imported (rev ${d.shared_rev ?? "?"})` : `import failed: ${d.detail}`;
  } catch (e) { $("#cfgMsg").textContent = "import failed"; }
  ev.target.value = "";
});
$("#setTime").addEventListener("click", () => confirm(`Set cluster clock to ${new Date().toString()}?`) && cmd("set_wall_time", { unix: Date.now() / 1000 }));

// ------------------------------------------------------------------- logs
async function loadLogs() {
  try {
    const d = await api("/api/logs?n=300", undefined, "GET");
    $("#events").textContent = d.events.map((e) => `${new Date(e.wall * 1000).toLocaleTimeString()} ${e.node} ${e.level.toUpperCase()} ${e.msg}`).join("\n");
    const t = $("#calHist"); t.innerHTML = "";
    t.append(el("tr", {}, el("th", {}, "time"), el("th", {}, "CRT"), el("th", {}, "offset"), el("th", {}, "taps"), el("th", {}, "SD"), el("th", {}, "SE")));
    for (const c of d.calibrations) t.append(el("tr", {}, el("td", {}, new Date(c.wall * 1000).toLocaleString()), el("td", {}, c.node),
      el("td", {}, fmt(c.offset_ms, 1)), el("td", {}, c.detail.n_used ?? "–"), el("td", {}, fmt(c.detail.circ_std_ms, 1)), el("td", {}, fmt(c.detail.std_err_ms, 1))));
  } catch (e) {}
}

// ------------------------------------------------------------------- boot
async function start() {
  const s = await fetch("/api/session").then((r) => r.json());
  if (!s.authenticated) {
    showLogin();
    if (!s.password_configured) $("#loginMsg").textContent = "No admin password set. On the Pi run: sudo phosphor-passwd";
    return;
  }
  showApp();
  connect();
  const tab = location.hash.slice(1);
  if (tab) { const b = $(`#tabs [data-tab=${tab}]`); if (b) b.click(); }
}
start();
requestAnimationFrame(animate);
