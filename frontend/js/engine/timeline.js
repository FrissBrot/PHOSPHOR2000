// Twin of backend/sync/timeline.py: identical formula, cluster seconds.
export class Timeline {
  constructor(d = {}) {
    this.bpm = d.bpm ?? 120;
    this.anchor_time = d.anchor_time ?? 0;
    this.anchor_beat = d.anchor_beat ?? 0;
    this.beats_per_bar = d.beats_per_bar ?? 4;
    this.confidence = d.confidence ?? 0;
    this.source = d.source ?? "PREDICTED";
    this.revision = d.revision ?? 0;
    this.epoch = d.epoch ?? 0;
    this.seed = d.seed ?? 0x2000;
    this.slew_delta = d.slew_delta ?? 0;
    this.slew_start = d.slew_start ?? 0;
    this.slew_end = d.slew_end ?? 0;
    this.live_at = d.live_at ?? null;
  }
  slew(t) {
    if (this.slew_delta === 0) return 0;
    let ramp;
    if (this.slew_end <= this.slew_start) ramp = t >= this.slew_end ? 1 : 0;
    else ramp = Math.min(1, Math.max(0, (t - this.slew_start) / (this.slew_end - this.slew_start)));
    return -this.slew_delta * (1 - ramp);
  }
  rawBeat(t) { return this.anchor_beat + (t - this.anchor_time) * this.bpm / 60; }
  beatAt(t) { return this.rawBeat(t) + this.slew(t); }
  period() { return 60 / this.bpm; }
  timeOfBeat(beat) {
    const spb = 60 / this.bpm;
    let t = this.anchor_time + (beat - this.anchor_beat) * spb;
    for (let i = 0; i < 8; i++) {
      const err = this.beatAt(t) - beat;
      if (Math.abs(err) < 1e-9) break;
      t -= err * spb;
    }
    return t;
  }
}
