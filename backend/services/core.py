"""phosphor-core: the per-Pi daemon (network/election, sync, replication, API).

One asyncio loop hosts:
  * the 20 Hz node tick (election state machine, clock sync, timeline upkeep)
  * the authenticated UDP transport to other nodes (port network.udp_port)
  * a Unix datagram socket for local helpers (audio + keyboard services)
  * the FastAPI/uvicorn server (renderer WebSocket, admin dashboard)
nmcli runs on its own worker thread (backend/network/nmcli.py), the audio
engine and keyboard reader are separate processes, so none of them can stall
the tick or the renderer.
"""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import logging
import os
import shutil
import signal
import socket
import struct
import time
from pathlib import Path

from backend.cluster.messages import Codec, CodecError, derive_key
from backend.cluster.node import ClusterNode, CommandError
from backend.config import ETC_DIR, STATE_DIR, AppConfig, load_config
from backend.network.base import NetworkAdapter, NullAdapter
from backend.storage.cache import EventLog, StateCache, boot_id
from backend.services.sdnotify import notify
from backend.services.system import CpuSampler, overlay_root_active, read_metrics, request_persist, run_action, set_wifi_country
from backend.version import software_version

log = logging.getLogger("phos.core")

SAFE_MODE_FLAGS = (Path("/boot/firmware/phosphor-safe-mode"), Path("/boot/phosphor-safe-mode"),
                   ETC_DIR / "safe-mode")
RUN_DIR = Path(os.environ.get("PHOSPHOR_RUN", "/run/phosphor2000"))
LOCAL_SOCK = "core.sock"


def safe_mode() -> bool:
    return any(p.exists() for p in SAFE_MODE_FLAGS)


def make_adapter(cfg: AppConfig) -> NetworkAdapter:
    if not cfg.network.manage or safe_mode():
        log.warning("network management disabled (config or safe-mode flag): radio untouched")
        return NullAdapter()
    if not shutil.which("nmcli"):
        log.error("nmcli not found: running without network management")
        return NullAdapter()
    from backend.network.nmcli import NmcliAdapter
    return NmcliAdapter(cfg.network)


def detect_audio_device() -> bool:
    """A USB capture device is present (ALSA card with a capture PCM and USB in its id)."""
    try:
        for card in Path("/proc/asound").glob("card[0-9]*"):
            usb = (card / "usbid").exists()
            capture = any(card.glob("pcm*c"))
            if usb and capture:
                return True
    except OSError:
        pass
    return False


SO_TIMESTAMPNS = getattr(socket, "SO_TIMESTAMPNS", 35)   # Linux; == SCM_TIMESTAMPNS (struct timespec)
_TIMESPEC = struct.Struct("@ll")


def kernel_rx_monotonic(ancdata, now_real: float, now_mono: float) -> float | None:
    """Convert a SO_TIMESTAMPNS (CLOCK_REALTIME, stamped by the kernel when the packet arrived)
    into CLOCK_MONOTONIC using the realtime-monotonic difference measured right now."""
    for level, typ, data in ancdata:
        if level == socket.SOL_SOCKET and typ == SO_TIMESTAMPNS and len(data) >= _TIMESPEC.size:
            sec, nsec = _TIMESPEC.unpack_from(data)
            real = sec + nsec / 1e9
            mono = real - (now_real - now_mono)
            if 0 <= now_mono - mono < 5.0:      # sanity: not in the future, not ancient
                return mono
    return None


class UdpTransport:
    """Cluster UDP socket with kernel receive timestamps.

    Clock-sync accuracy depends on *when* a packet arrived, not when Python got round to it:
    the kernel stamps every datagram (SO_TIMESTAMPNS) in the receive path, so asyncio/GIL
    scheduling delay drops out of t2 (leader) and t4 (follower). Falls back to time.monotonic().
    """

    def __init__(self, core: "Core") -> None:
        self.core = core
        self.sock: socket.socket | None = None
        self.kernel_stamps = 0
        self.fallback_stamps = 0

    def open(self, port: int, loop: asyncio.AbstractEventLoop) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0xB8)  # DSCP EF: WMM voice queue on Wi-Fi
        try:
            s.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPNS, 1)
        except OSError:
            log.warning("SO_TIMESTAMPNS unavailable: user-space receive timestamps")
        s.bind(("0.0.0.0", port))
        s.setblocking(False)
        self.sock = s
        loop.add_reader(s.fileno(), self._readable)

    def sendto(self, data: bytes, addr) -> None:
        assert self.sock is not None
        self.sock.sendto(data, addr)

    def _readable(self) -> None:
        assert self.sock is not None
        for _ in range(64):
            try:
                data, anc, _flags, addr = self.sock.recvmsg(65535, 256)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                self.core.tx_errors += 1
                return
            now_mono = time.monotonic()
            stamp = kernel_rx_monotonic(anc, time.time(), now_mono)
            if stamp is None:
                self.fallback_stamps += 1
                stamp = now_mono
            else:
                self.kernel_stamps += 1
            self.datagram_received(data, addr, stamp)

    def datagram_received(self, data: bytes, addr, recv_local: float) -> None:
        try:
            msg = self.core.codec.decode(data)
        except CodecError:
            self.core.rx_rejected += 1
            return
        to = msg.get("_to")
        if to is not None and to != self.core.node.id:
            return
        self.core.rx += 1
        try:
            self.core.node.on_message(msg, recv_local, addr[0])
        except Exception:
            log.exception("error handling %s from %s", msg.get("t"), msg.get("_from"))


class LocalProto(asyncio.DatagramProtocol):
    """JSON datagrams from local helper processes (audio, keyboard)."""

    def __init__(self, core: "Core") -> None:
        self.core = core

    def datagram_received(self, data: bytes, addr) -> None:  # type: ignore[override]
        try:
            msg = json.loads(data)
        except ValueError:
            return
        self.core.on_local(msg)


KEYS = {2: "pi-01", 3: "pi-02", 4: "pi-03", 5: "pi-04", 6: "pi-05"}
KEY_ENTER, KEY_KPENTER, KEY_ESC = 28, 96, 1
KEY_UP, KEY_DOWN, KEY_LEFT, KEY_RIGHT, KEY_PGUP, KEY_PGDN = 103, 108, 105, 106, 104, 109
KEY_S, KEY_R, KEY_A, KEY_C, KEY_F = 31, 19, 30, 46, 33


class Core:
    def __init__(self, cfg: AppConfig, adapter: NetworkAdapter | None = None, state_dir: Path = STATE_DIR,
                 has_audio: bool | None = None) -> None:
        self.cfg = cfg
        self.state_dir = state_dir
        if has_audio is None:
            has_audio = cfg.audio.enabled in ("on", True) or (cfg.audio.enabled == "auto" and detect_audio_device())
        self.codec = Codec(derive_key(cfg.secrets.cluster_key), cfg.node.id)
        if not cfg.secrets.cluster_key:
            log.error("no cluster_key in secrets.yaml: using an INSECURE development key")
        net = ipaddress.ip_network(f"{cfg.network.gateway}/{cfg.network.prefix}", strict=False)
        self.broadcast = str(net.broadcast_address)
        self.udp: UdpTransport | None = None
        self.node = ClusterNode(cfg, adapter or make_adapter(cfg), self.send, has_audio=has_audio)
        self.node.on_restart_request = self._restart
        self.node.now_fn = time.monotonic
        self.node.version = software_version()
        self.started = time.monotonic()
        self.kiosk_flag = os.environ.get("PHOSPHOR_NO_KIOSK") != "1"
        self.kiosk_override: bool | None = None
        self.renderer_seen_at = self.started        # last moment the renderer looked healthy
        self.renderer_restarts = 0
        self.next_renderer_restart = 0.0
        self.overlay = overlay_root_active()
        self.persisted_key: tuple | None = None
        from backend.update import Updater
        self.updater = Updater(self, RUN_DIR)
        self.node.on_update_message = self.updater.on_message
        self.persist_due: float | None = None
        if cfg.network.manage and not safe_mode():
            self.node.on_wifi_country = set_wifi_country
        self.cache = StateCache(state_dir)
        self.events = EventLog(state_dir / "calibrations.sqlite3")
        self.boot = boot_id()
        self.rx = self.rx_rejected = self.tx = self.tx_errors = 0
        self.metrics: dict = {}
        self.cpu = CpuSampler()
        self.kbd_active_until = 0.0
        self._log_idx = 0
        self._stop = asyncio.Event()
        blob = self.cache.load()
        if blob:
            self.node.restore_cache(blob, self.boot, time.monotonic())

    # ------------------------------------------------------------ transport
    def send(self, msg: dict, to: str | None) -> None:
        if self.udp is None or self.udp.sock is None:
            return
        if to is not None:
            msg = {**msg, "_to": to}
        try:
            data = self.codec.encode(msg)
        except Exception:
            log.exception("encode failed for %s", msg.get("t"))
            return
        ip = self.node.peer_ips.get(to) if to else None
        try:
            self.udp.sendto(data, (ip or self.broadcast, self.cfg.network.udp_port))
            self.tx += 1
        except OSError:
            self.tx_errors += 1

    def on_local(self, msg: dict) -> None:
        now = time.monotonic()
        t = msg.get("t")
        try:
            if t == "audio_beat":
                self.node.ingest_local_audio(msg, now)
            elif t == "audio_features":
                self.node.ingest_local_features(msg, now)
            elif t == "audio_status":
                self.node.local_audio = {**msg, "_rx": now}
                if self.cfg.audio.enabled not in ("off", False):
                    self.node.set_has_audio(msg.get("state") not in ("NO_DEVICE", "DISABLED"), now)
            elif t == "key":
                self.on_key(int(msg["code"]), int(msg.get("value", 1)), float(msg.get("local", now)), now)
        except (KeyError, ValueError, TypeError, CommandError) as exc:
            log.warning("local message %s rejected: %s", t, exc)

    def on_key(self, code: int, value: int, ev_local: float, now: float) -> None:
        """USB keyboard on this Pi: coordinator calibration mode (see docs/synchronization.md)."""
        self.kbd_active_until = now + 120.0
        n = self.node
        if code in (KEY_ENTER, KEY_KPENTER):
            n.command("key_tap", {"local": ev_local, "value": value}, now)
            return
        if value != 1:
            return  # only fresh presses trigger commands; repeats/releases ignored
        if code in KEYS:
            n.command("cal_start", {"mode": "single", "target": KEYS[code]}, now)
        elif code == KEY_A:
            n.command("cal_start", {"mode": "all"}, now)
        elif code == KEY_C:
            n.command("cal_start", {"mode": "click"}, now)
        elif code in (KEY_UP, KEY_DOWN, KEY_LEFT, KEY_RIGHT, KEY_PGUP, KEY_PGDN):
            d = {KEY_UP: 5, KEY_DOWN: -5, KEY_RIGHT: 1, KEY_LEFT: -1, KEY_PGUP: 20, KEY_PGDN: -20}[code]
            n.command("cal_adjust", {"delta_ms": d}, now)
        elif code == KEY_F:
            n.command("cal_adjust", {"apply_suggestion": True}, now)
        elif code == KEY_S:
            n.command("cal_save", {}, now)
        elif code == KEY_ESC:
            n.command("cal_cancel", {}, now)
        elif code == KEY_R:
            n.command("cal_reset", {}, now)

    def _restart(self, what: str) -> None:
        log.warning("restart requested: %s", what)
        run_action(what)

    # ------------------------------------------------------------------ loop
    async def start_network(self) -> None:
        loop = asyncio.get_running_loop()
        self.udp = UdpTransport(self)
        self.udp.open(self.cfg.network.udp_port, loop)
        try:
            RUN_DIR.mkdir(parents=True, exist_ok=True)
            path = RUN_DIR / LOCAL_SOCK
            if path.exists():
                path.unlink()
            await loop.create_datagram_endpoint(lambda: LocalProto(self), local_addr=str(path),
                                                family=socket.AF_UNIX)
            os.chmod(path, 0o660)
        except OSError as exc:
            log.error("local helper socket unavailable: %s", exc)

    async def run_ticks(self) -> None:
        last_metrics = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            try:
                self.node.tick(now)
            except Exception:
                log.exception("tick failed")  # keep going: renderer must never be blocked
            self._flush_logs()
            if now - last_metrics > 2.0:
                last_metrics = now
                self.metrics = {**read_metrics(), "cpu_pct": self.cpu.sample(), "rx": self.rx,
                                "rx_rejected": self.rx_rejected, "tx": self.tx, "tx_errors": self.tx_errors,
                                "kernel_rx_stamps": self.udp.kernel_stamps if self.udp else 0,
                                "fallback_rx_stamps": self.udp.fallback_stamps if self.udp else 0}
            if self.node.dirty and time.monotonic() - self.cache.last_write > 10.0:
                self.persist()
            self.renderer_watchdog(now)
            try:
                self.updater.tick(now)
            except Exception:
                log.exception("updater tick failed")
            if self.persist_due is not None and now >= self.persist_due:
                self.persist_due = None
                request_persist()
            notify("WATCHDOG=1")   # systemd restarts us if this loop ever hangs (WatchdogSec)
            await asyncio.sleep(0.05)

    RENDERER_SILENT_S = 45.0       # no report from the browser for this long -> hung/crashed page
    RENDERER_FIRST_S = 240.0       # grace after core start (Chromium cold start on a Pi 3)
    RENDERER_FROZEN_S = 30.0       # page alive but drawing < 1 fps

    KIOSK_WANTS = Path("/etc/systemd/system/phosphor.target.wants/phosphor-renderer.service")

    @property
    def kiosk(self) -> bool:
        """A screen is attached *and enabled* on this Pi (configure-kiosk.sh). The unit file exists on
        every Pi, so its presence is not enough: a headless master must never get a kiosk started."""
        if self.kiosk_override is not None:
            return self.kiosk_override
        return self.kiosk_flag and self.KIOSK_WANTS.exists()

    @kiosk.setter
    def kiosk(self, value: bool) -> None:
        self.kiosk_override = value

    def renderer_watchdog(self, now: float) -> None:
        """Restart a hung kiosk. systemd only restarts *crashed* processes; a frozen page,
        "Aw, Snap!" or a stalled compositor keeps the process alive. The browser reports every 2 s."""
        if not self.kiosk or now < self.next_renderer_restart:
            return
        r = self.node.renderer
        rx = r.get("_rx")
        healthy = rx is not None and now - rx < 5 and (r.get("fps") or 0) >= 1
        if healthy:
            self.renderer_seen_at = now
            return
        limit = self.RENDERER_FIRST_S if rx is None else (
            self.RENDERER_FROZEN_S if now - rx < 5 else self.RENDERER_SILENT_S)
        if now - self.renderer_seen_at > limit:
            self.renderer_restarts += 1
            self.node._log("error", f"renderer unresponsive for {now - self.renderer_seen_at:.0f}s "
                                    f"(restart #{self.renderer_restarts})")
            run_action("renderer")
            self.renderer_seen_at = now
            self.node.renderer = {}
            # back off if it keeps failing: 2, 4, 8 ... max 15 min between restarts
            self.next_renderer_restart = now + min(900.0, 120.0 * 2 ** min(self.renderer_restarts - 1, 3))

    def persist(self) -> None:
        try:
            self.cache.save(self.node.cache_blob(self.boot, time.monotonic()))
            self.node.dirty = False
            if self.overlay:
                # read-only root: copy config/offsets to the real SD card when they changed
                key = (self.node.shared_rev, json.dumps(self.node.shared.calibration.offsets_ms, sort_keys=True))
                if key != self.persisted_key:
                    self.persisted_key = key
                    if self.persist_due is None:
                        self.persist_due = time.monotonic() + 5.0   # debounce bursts of edits
        except OSError as exc:
            log.error("cache write failed: %s", exc)

    def _flush_logs(self) -> None:
        lines = self.node.log_lines
        if self._log_idx > len(lines):
            self._log_idx = 0
        for ln in lines[self._log_idx:]:
            self.events.add(ln["wall"], ln["node"], ln["level"], ln["msg"])
        self._log_idx = len(lines)
        if len(lines) > 400:
            del lines[:200]
            self._log_idx = len(lines)

    def stop(self) -> None:
        self._stop.set()


async def amain(cfg: AppConfig) -> None:
    import uvicorn

    from backend.api.app import create_app

    core = Core(cfg)
    await core.start_network()
    app = create_app(core)
    server = uvicorn.Server(uvicorn.Config(app, host=cfg.server.host, port=cfg.server.port, log_level="warning",
                                           access_log=False, ws_ping_interval=10, ws_ping_timeout=20,
                                           lifespan="off"))
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: (core.stop(), setattr(server, "should_exit", True)))
    ticks = asyncio.create_task(core.run_ticks())
    notify("READY=1")
    try:
        await server.serve()
    finally:
        core.stop()
        await ticks
        core.persist()
        core.node.election.adapter.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="PHOSPHOR2000 core daemon")
    ap.add_argument("--node", help="override node id (development)")
    ap.add_argument("--port", type=int)
    ap.add_argument("--no-network", action="store_true", help="never touch the radio")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    over: dict = {}
    if a.node:
        over.setdefault("node", {})["id"] = a.node
    if a.port:
        over.setdefault("server", {})["port"] = a.port
    if a.no_network:
        over.setdefault("network", {})["manage"] = False
    cfg = load_config(overrides=over)
    asyncio.run(amain(cfg))


if __name__ == "__main__":
    main()
