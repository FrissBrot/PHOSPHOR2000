// Browser <-> local core clock sync.
//
// The browser cannot read CLOCK_MONOTONIC; it has performance.now(). We
// exchange {c: perf_ms} -> {c, s: cluster_seconds} over the local WebSocket and
// estimate offset = s - (c1 + c4)/2 with a lowest-RTT filter. Small changes are
// slewed (max 5 ms/s); large changes are applied at the next bar boundary by
// the renderer (requestStep), never mid-bar.
export class BrowserClock {
  constructor() {
    this.samples = [];
    this.applied = null;   // seconds: cluster - perf/1000
    this.target = null;
    this.lastUpdate = performance.now();
    this.pendingStep = false;
    this.rttMs = null;
    this.maxSlewPerSec = 0.005;
    this.stepThreshold = 0.030;
  }

  // Fallback before the first sample (e.g. backend down after browser restart):
  // cached relation between cluster time and Date.now() (same boot, stable).
  seedFromWall(clusterMinusWall) {
    if (this.applied !== null || clusterMinusWall == null) return;
    const wallSec = (performance.timeOrigin + performance.now()) / 1000;
    this.applied = this.target = wallSec + clusterMinusWall - performance.now() / 1000;
  }

  onSample(c1, s, c4) {
    const rtt = c4 - c1;
    if (rtt < 0 || rtt > 1000) return;
    this.samples.push({ off: s - (c1 + c4) / 2000, rtt });
    if (this.samples.length > 16) this.samples.shift();
    const best = [...this.samples].sort((a, b) => a.rtt - b.rtt).slice(0, Math.max(1, this.samples.length >> 2));
    const offs = best.map(x => x.off).sort((a, b) => a - b);
    this.target = offs[offs.length >> 1];
    this.rttMs = best[0].rtt;
    if (this.applied === null) this.applied = this.target;
    else if (Math.abs(this.target - this.applied) > this.stepThreshold) this.pendingStep = true;
  }

  // Called once per frame.
  update(perfNow) {
    const dt = Math.max(0, (perfNow - this.lastUpdate) / 1000);
    this.lastUpdate = perfNow;
    if (this.target === null || this.applied === null || this.pendingStep) return;
    const err = this.target - this.applied;
    const lim = this.maxSlewPerSec * dt;
    this.applied += Math.max(-lim, Math.min(lim, err));
  }

  applyStep() {
    if (this.pendingStep) { this.applied = this.target; this.pendingStep = false; return true; }
    return false;
  }

  clusterAt(perfMs) { return this.applied === null ? null : perfMs / 1000 + this.applied; }
  // what the cluster time would be after a pending step (to find the boundary)
  targetAt(perfMs) { return this.target === null ? null : perfMs / 1000 + this.target; }
  ready() { return this.applied !== null; }
}
