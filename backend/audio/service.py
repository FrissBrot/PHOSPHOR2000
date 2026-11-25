"""cyberspace-audio: USB line-in capture + beat tracking, feeding the local core.

Real-time rules:
* the PortAudio callback only copies the block and stamps it, then
  ``put_nowait`` into a bounded queue (drops + counts on overflow); it never
  blocks, allocates large objects, logs or touches the network.
* analysis runs on a normal thread; results go to the core over a Unix
  datagram socket (non-blocking, lossy by design).

Timestamps: ``inputBufferAdcTime`` (PortAudio stream clock) is converted to
CLOCK_MONOTONIC inside the callback by measuring both clocks at the same
instant; if the driver reports nonsense we fall back to "now minus one
block minus configured input latency".
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import socket
import threading
import time
import wave
from pathlib import Path

import numpy as np

from backend.audio.analysis import AudioParams, BeatTracker
from backend.config import AudioConfig, load_config

log = logging.getLogger("cs2k.audio")
SOCK = Path(os.environ.get("CYBERSPACE_RUN", "/run/cyberspace2000")) / "core.sock"


class CoreLink:
    def __init__(self, path: Path = SOCK) -> None:
        self.path = str(path)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.errors = 0

    def send(self, msg: dict) -> None:
        try:
            self.sock.sendto(json.dumps(msg, separators=(",", ":")).encode(), self.path)
        except OSError:
            self.errors += 1  # core not running / restarting: drop


class Capture:
    """Callback side. Kept tiny on purpose."""

    def __init__(self, hop: int, samplerate: int, input_latency_s: float | None, maxsize: int = 64) -> None:
        self.q: queue.Queue = queue.Queue(maxsize=maxsize)
        self.hop = hop
        self.sr = samplerate
        self.fallback_latency = input_latency_s or 0.010
        self.force_latency = input_latency_s
        self.dropped = 0
        self.overflows = 0
        self.last_latency = None

    def callback(self, indata, frames, time_info, status) -> None:  # PortAudio thread
        now = time.monotonic()
        if status:
            self.overflows += 1
        adc_local = None
        if self.force_latency is None:
            try:
                lag = time_info.currentTime - time_info.inputBufferAdcTime
                if 0.0 <= lag < 0.5:
                    adc_local = now - lag
                    self.last_latency = lag
            except AttributeError:
                pass
        if adc_local is None:
            adc_local = now - frames / self.sr - self.fallback_latency
        mono = indata[:, 0].copy() if indata.ndim == 2 and indata.shape[1] == 1 else indata.mean(axis=1, dtype=np.float32)
        try:
            self.q.put_nowait((mono, adc_local))
        except queue.Full:
            self.dropped += 1


class Analyzer(threading.Thread):
    def __init__(self, cap: Capture, tracker: BeatTracker, link: CoreLink, device_name: str = "?") -> None:
        super().__init__(name="analysis", daemon=True)
        self.cap, self.tr, self.link = cap, tracker, link
        self.device_name = device_name
        self.stop_ev = threading.Event()
        self.last_feat = 0.0
        self.last_status = 0.0
        self.last_beat_sent = 0.0

    def run(self) -> None:
        pending = np.zeros(0, dtype=np.float32)
        t_pending = None
        hop = self.tr.p.hop
        sr = self.tr.p.samplerate
        while not self.stop_ev.is_set():
            try:
                block, t0 = self.cap.q.get(timeout=0.5)
            except queue.Empty:
                self._status()
                continue
            # re-chunk to exactly `hop` samples (drivers may deliver other sizes)
            if t_pending is None or len(pending) == 0:
                t_pending = t0
            pending = np.concatenate((pending, block.astype(np.float32, copy=False)))
            while len(pending) >= hop:
                chunk, pending = pending[:hop], pending[hop:]
                self.handle(chunk, t_pending)
                t_pending += hop / sr
            self._status()

    def handle(self, chunk: np.ndarray, t0: float) -> None:
        beats, feat = self.tr.process(chunk, t0)
        st = self.tr.state
        for b in beats:
            self.link.send({"t": "audio_beat", "live": st == "LIVE", "bpm": round(b.bpm, 4),
                            "beat_local": b.next_beat, "confidence": round(b.confidence, 3),
                            "phase_unc_ms": round(b.phase_unc_ms, 2), "state": st})
        now = time.monotonic()
        if feat is not None and now - self.last_feat >= 0.05:
            self.last_feat = now
            self.link.send({"t": "audio_features", "local": feat.t, "rms": round(feat.rms, 4), "bass": round(feat.bass, 3),
                            "mid": round(feat.mid, 3), "treble": round(feat.treble, 3), "spectrum": feat.spectrum,
                            "onset": feat.onset, "live": st == "LIVE"})

    def _status(self) -> None:
        now = time.monotonic()
        if now - self.last_status < 1.0:
            return
        self.last_status = now
        tr = self.tr
        self.link.send({"t": "audio_status", "state": tr.state, "live": tr.state == "LIVE",
                        "bpm": round(60 / tr.period, 3) if tr.period else None,
                        "confidence": round(tr.confidence(), 3), "device": self.device_name,
                        "dropped": self.cap.dropped, "overflows": self.cap.overflows,
                        "input_latency_ms": None if self.cap.last_latency is None else round(self.cap.last_latency * 1000, 2),
                        "queue": self.cap.q.qsize()})


def params_from(cfg: AudioConfig) -> AudioParams:
    return AudioParams(samplerate=cfg.samplerate, hop=cfg.blocksize, min_bpm=cfg.min_bpm, max_bpm=cfg.max_bpm,
                       silence_rms=cfg.silence_rms, silence_hold_s=cfg.silence_hold_s)


def pick_device(sd, want: str | None) -> tuple[int | None, str]:
    devs = sd.query_devices()
    cands = [(i, d) for i, d in enumerate(devs) if d.get("max_input_channels", 0) > 0]
    for key in ([want] if want else []) + ["USB", "usb"]:
        for i, d in cands:
            if key and key in d["name"]:
                return i, d["name"]
    return None, ""


def run_device(cfg: AudioConfig, link: CoreLink) -> None:
    import sounddevice as sd  # PortAudio; imported lazily so tests run without it

    while True:
        idx, name = pick_device(sd, cfg.device)
        if idx is None:
            link.send({"t": "audio_status", "state": "NO_DEVICE", "live": False})
            time.sleep(5)
            continue
        tracker = BeatTracker(params_from(cfg))
        cap = Capture(cfg.blocksize, cfg.samplerate,
                      None if cfg.input_latency_ms is None else cfg.input_latency_ms / 1000)
        an = Analyzer(cap, tracker, link, name)
        an.start()
        log.info("capturing from %s (%d Hz, block %d)", name, cfg.samplerate, cfg.blocksize)
        try:
            with sd.InputStream(device=idx, channels=cfg.channels, samplerate=cfg.samplerate,
                                blocksize=cfg.blocksize, dtype="float32", latency="low", callback=cap.callback):
                while an.is_alive():
                    time.sleep(1.0)
                    if cap.overflows and cap.overflows % 100 == 0:
                        log.warning("input overflows: %d", cap.overflows)
        except Exception as exc:  # device unplugged, ALSA error...
            log.error("audio stream failed: %s; retrying", exc)
            link.send({"t": "audio_status", "state": "ERROR", "live": False, "error": str(exc)[:200]})
            time.sleep(3)
        finally:
            an.stop_ev.set()


def run_file(path: Path, cfg: AudioConfig, link: CoreLink, realtime: bool = True) -> BeatTracker:
    """Feed a 16-bit PCM WAV as if captured live (development / hardware-free tests)."""
    with wave.open(str(path)) as w:
        sr, ch = w.getframerate(), w.getnchannels()
        raw = w.readframes(w.getnframes())
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    p = params_from(cfg)
    p.samplerate = sr
    tracker = BeatTracker(p)
    cap = Capture(p.hop, sr, 0.0)
    an = Analyzer(cap, tracker, link, f"file:{path.name}")
    t0 = time.monotonic()
    for i in range(len(x) // p.hop):
        t_blk = t0 + i * p.hop / sr
        if realtime:
            delay = t_blk + p.hop / sr - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        an.handle(x[i * p.hop:(i + 1) * p.hop], t_blk)
        an._status()
    return tracker


def main() -> None:
    ap = argparse.ArgumentParser(description="CYBERSPACE 2000 audio engine")
    ap.add_argument("--file", type=Path, help="analyse a WAV file in real time instead of the USB input")
    ap.add_argument("--list", action="store_true", help="list capture devices and exit")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = load_config().audio
    if a.list:
        import sounddevice as sd
        print(sd.query_devices())
        return
    link = CoreLink()
    if a.file:
        run_file(a.file, cfg, link)
        return
    if cfg.enabled in ("off", False):
        log.info("audio disabled in config")
        while True:
            link.send({"t": "audio_status", "state": "DISABLED", "live": False})
            time.sleep(10)
    run_device(cfg, link)


if __name__ == "__main__":
    main()
