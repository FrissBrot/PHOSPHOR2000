// Photosensitivity guard. Every full-screen luminance flash goes through here.
//  * never more than max_flash_hz flashes per second (<= 3, WCAG 2.3.1)
//  * luminance capped at max_flash_luminance
//  * reduced_flashing: no full-screen flashes at all; effects use small areas
//    and gentle amplitude instead.
export class FlashGuard {
  constructor() {
    this.cfg = { reduced_flashing: false, max_flash_hz: 3, max_flash_luminance: 0.85 };
    this.history = [];
  }
  configure(cfg) { if (cfg) this.cfg = { ...this.cfg, ...cfg }; }
  get reduced() { return !!this.cfg.reduced_flashing; }

  // Request a full-screen flash at musical event `eventId` (e.g. beat index) at time t (s).
  // Returns the permitted intensity (0..1). Same event id -> same decision.
  flash(eventId, t, wanted) {
    if (this.reduced) return 0;
    const minGap = 1 / Math.min(3, this.cfg.max_flash_hz);
    const last = this.history[this.history.length - 1];
    if (!last || last.id !== eventId) {
      if (last && t - last.t < minGap - 1e-3) return 0;
      this.history.push({ id: eventId, t });
      if (this.history.length > 8) this.history.shift();
    }
    return Math.min(wanted, this.cfg.max_flash_luminance);
  }

  // Amplitude for non-fullscreen pulsing (always allowed but softened when reduced).
  pulse(wanted) { return this.reduced ? Math.min(wanted, 0.25) : wanted; }
}
