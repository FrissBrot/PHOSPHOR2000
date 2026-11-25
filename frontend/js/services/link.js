// WebSocket link to the local core with reconnect and a localStorage cache,
// so a restarted browser renders immediately from the last known state even
// if the core is (re)starting. The renderer never waits for the network.
const CACHE_KEY = "phos.render_state.v1";

export class Link {
  constructor(clock, onState, onFeatures) {
    this.clock = clock;
    this.onState = onState;
    this.onFeatures = onFeatures;
    this.ws = null;
    this.connected = false;
    this.lastMsg = 0;
    this.retry = 500;
    this.syncTimer = null;
    this.burst = 0;
  }

  loadCache() {
    try {
      const raw = localStorage.getItem(CACHE_KEY);
      if (!raw) return null;
      const st = JSON.parse(raw);
      this.clock.seedFromWall(st._cluster_minus_wall);
      return st;
    } catch (e) { return null; }
  }

  saveCache(st) {
    try {
      const wall = (performance.timeOrigin + performance.now()) / 1000;
      const c = this.clock.clusterAt(performance.now());
      if (c !== null) st._cluster_minus_wall = c - wall;
      localStorage.setItem(CACHE_KEY, JSON.stringify(st));
    } catch (e) { /* storage full / disabled: rendering continues */ }
  }

  connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    let ws;
    try { ws = new WebSocket(`${proto}://${location.host}/ws/render`); }
    catch (e) { this.scheduleReconnect(); return; }
    this.ws = ws;
    ws.onopen = () => {
      this.connected = true;
      this.retry = 500;
      this.burst = 8;
      this.sync();
    };
    ws.onmessage = (ev) => {
      this.lastMsg = performance.now();
      let m;
      try { m = JSON.parse(ev.data); } catch (e) { return; }
      if (m.t === "sync") this.clock.onSample(m.c, m.s, performance.now());
      else if (m.t === "render_state") { this.onState(m); this.maybeCache(m); }
      else if (m.t === "features") this.onFeatures(m);
    };
    ws.onclose = () => { this.connected = false; this.scheduleReconnect(); };
    ws.onerror = () => { try { ws.close(); } catch (e) {} };
  }

  maybeCache(m) {
    const now = performance.now();
    if (!this._lastCache || now - this._lastCache > 5000) { this._lastCache = now; this.saveCache(m); }
  }

  scheduleReconnect() {
    clearTimeout(this.syncTimer);
    setTimeout(() => this.connect(), this.retry);
    this.retry = Math.min(this.retry * 2, 5000);
  }

  sync() {
    if (!this.connected) return;
    try { this.ws.send(JSON.stringify({ t: "sync", c: performance.now() })); } catch (e) {}
    const delay = this.burst > 0 ? 120 : 1000;
    if (this.burst > 0) this.burst--;
    this.syncTimer = setTimeout(() => this.sync(), delay);
  }

  report(obj) {
    if (!this.connected) return;
    try { this.ws.send(JSON.stringify({ t: "report", ...obj })); } catch (e) {}
  }
}
