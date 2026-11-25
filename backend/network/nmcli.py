"""NetworkManager (nmcli) implementation of the radio adapter.

Design rules:
* Only fixed argv lists are executed (no shell, no user-provided strings
  except validated connection/interface names from the typed config).
* The Wi-Fi passphrase never appears on a command line: connection profiles
  are NetworkManager keyfiles (0600) written by scripts/configure-network.sh
  (``write_keyfiles`` below is the same logic, used by the installer).
* All calls run on one worker thread; the asyncio core only enqueues requests
  and drains events, so the renderer/clock/beacons never block on nmcli.
* A single radio cannot be AP and station at the same time: every switch is
  "down the other profile, then up the requested one" and the network *will*
  drop for several seconds. The election logic is built around that.
"""
from __future__ import annotations

import logging
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Sequence

from backend.config import NetworkConfig
from backend.network.base import EventKind, NetEvent, NetworkAdapter, RadioMode, RadioStatus, ScanEntry

log = logging.getLogger("phos.nmcli")

Runner = Callable[[Sequence[str], float], tuple[int, str, str]]


def default_runner(argv: Sequence[str], timeout: float) -> tuple[int, str, str]:
    try:
        p = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout,
                           env={"LC_ALL": "C", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError:
        return 127, "", "nmcli not found"


def split_terse(line: str) -> list[str]:
    """Split an `nmcli -t` line on unescaped colons."""
    out, cur, esc = [], [], False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def parse_scan(text: str) -> list[ScanEntry]:
    res = []
    for line in text.splitlines():
        if not line.strip():
            continue
        f = split_terse(line)
        if len(f) < 3:
            continue
        ssid, bssid, signal = f[0], f[1].upper(), f[2]
        try:
            res.append(ScanEntry(ssid, bssid, int(signal or 0)))
        except ValueError:
            continue
    return res


class NmcliAdapter(NetworkAdapter):
    def __init__(self, cfg: NetworkConfig, runner: Runner = default_runner, monitor_interval: float = 2.0,
                 sysfs: Path = Path("/sys/class/net")) -> None:
        self.cfg = cfg
        self.run = runner
        self.monitor_interval = monitor_interval
        self.sysfs = sysfs
        self._ops: queue.Queue = queue.Queue(maxsize=8)
        self._events: queue.Queue = queue.Queue(maxsize=64)
        self._status = RadioStatus(RadioMode.IDLE, own_mac=self._own_mac())
        self._expected: RadioMode = RadioMode.IDLE
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._probe_initial()
        self._thread = threading.Thread(target=self._worker, name="nmcli", daemon=True)
        self._thread.start()

    def _probe_initial(self) -> None:
        """After a core restart the AP or station connection may still be active: report it so
        the election can resume instead of dropping the network."""
        try:
            conn = self._active_connection()
        except Exception:
            return
        if conn == self.cfg.ap_connection:
            self._expected = RadioMode.AP
            self._set(RadioMode.AP, self._own_mac(), self.cfg.gateway)
        elif conn == self.cfg.sta_connection:
            bssid, ip = self._station_info()
            self._expected = RadioMode.STATION
            self._set(RadioMode.STATION, bssid, ip)

    # ---- public API (non-blocking) ----------------------------------------
    def request_scan(self) -> None:
        self._enqueue(("scan",))

    def start_hotspot(self) -> None:
        self._enqueue(("ap",))

    def connect(self, bssid: str | None = None) -> None:
        self._enqueue(("sta", bssid))

    def stop(self) -> None:
        self._enqueue(("stop",))

    def status(self) -> RadioStatus:
        with self._lock:
            return RadioStatus(self._status.mode, self._status.bssid, self._status.ip, self._status.own_mac)

    def poll_events(self) -> list[NetEvent]:
        out = []
        while True:
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                return out

    def supports_scan_in_ap_mode(self) -> bool:
        # brcmfmac on Pi 3 does not reliably scan while serving an AP.
        return False

    def close(self) -> None:
        self._stop.set()
        self._enqueue(("noop",))

    # ---- internals --------------------------------------------------------
    def _own_mac(self) -> str | None:
        try:
            return (self.sysfs / self.cfg.interface / "address").read_text().strip().upper()
        except OSError:
            return None

    def _enqueue(self, op: tuple) -> None:
        # Mode changes supersede queued mode changes (only the latest intent matters).
        if op[0] in ("ap", "sta", "stop"):
            kept = []
            while True:
                try:
                    o = self._ops.get_nowait()
                except queue.Empty:
                    break
                if o[0] not in ("ap", "sta", "stop"):
                    kept.append(o)
            for o in kept:
                self._ops.put_nowait(o)
        try:
            self._ops.put_nowait(op)
        except queue.Full:
            log.warning("nmcli op queue full, dropping %s", op[0])

    def _emit(self, ev: NetEvent) -> None:
        try:
            self._events.put_nowait(ev)
        except queue.Full:
            log.warning("nmcli event queue full, dropping %s", ev.kind)

    def _set(self, mode: RadioMode, bssid: str | None = None, ip: str | None = None) -> None:
        with self._lock:
            self._status = RadioStatus(mode, bssid, ip, self._status.own_mac or self._own_mac())

    def _nm(self, *args: str, timeout: float = 15.0) -> tuple[int, str, str]:
        argv = ["nmcli", *args]
        rc, out, err = self.run(argv, timeout)
        if rc != 0:
            log.info("nmcli %s -> rc=%s %s", " ".join(args[:4]), rc, err.strip()[:200])
        return rc, out, err

    def _worker(self) -> None:
        last_check = 0.0
        while not self._stop.is_set():
            try:
                op = self._ops.get(timeout=0.25)
            except queue.Empty:
                op = None
            if op is None:
                if time.monotonic() - last_check >= self.monitor_interval:
                    last_check = time.monotonic()
                    self._check_link()
                continue
            try:
                getattr(self, "_op_" + op[0])(*op[1:])
            except Exception as exc:  # never kill the worker
                log.exception("nmcli op %s failed", op[0])
                self._emit(NetEvent(EventKind.HOTSPOT_FAILED if op[0] == "ap" else EventKind.CONNECT_FAILED,
                                    ok=False, error=str(exc)))

    def _op_noop(self) -> None:
        pass

    def _op_scan(self) -> None:
        rc, out, _ = self._nm("-t", "-f", "SSID,BSSID,SIGNAL", "device", "wifi", "list",
                              "ifname", self.cfg.interface, "--rescan", "yes", timeout=20)
        entries = parse_scan(out) if rc == 0 else []
        self._emit(NetEvent(EventKind.SCAN_RESULT, [e for e in entries if e.ssid == self.cfg.ssid], ok=rc == 0))

    def _op_ap(self) -> None:
        self._set(RadioMode.BUSY)
        self._nm("connection", "down", self.cfg.sta_connection, timeout=10)
        rc, _, err = self._nm("--wait", str(int(self.cfg.hotspot_start_timeout)), "connection", "up",
                              self.cfg.ap_connection, "ifname", self.cfg.interface,
                              timeout=self.cfg.hotspot_start_timeout + 5)
        if rc == 0:
            bssid = self._own_mac()
            self._expected = RadioMode.AP
            self._set(RadioMode.AP, bssid, self.cfg.gateway)
            self._emit(NetEvent(EventKind.HOTSPOT_UP, {"bssid": bssid}))
        else:
            self._expected = RadioMode.IDLE
            self._set(RadioMode.IDLE)
            self._emit(NetEvent(EventKind.HOTSPOT_FAILED, ok=False, error=err.strip()[:200]))

    def _op_sta(self, bssid: str | None) -> None:
        self._set(RadioMode.BUSY)
        self._nm("connection", "down", self.cfg.ap_connection, timeout=10)
        args = ["--wait", str(int(self.cfg.connect_timeout)), "connection", "up", self.cfg.sta_connection,
                "ifname", self.cfg.interface]
        if bssid:
            args += ["ap", bssid]
        rc, _, err = self._nm(*args, timeout=self.cfg.connect_timeout + 5)
        if rc == 0:
            st_bssid, ip = self._station_info()
            self._expected = RadioMode.STATION
            self._set(RadioMode.STATION, st_bssid or bssid, ip)
            self._emit(NetEvent(EventKind.CONNECTED, {"bssid": st_bssid or bssid, "ip": ip}))
        else:
            self._expected = RadioMode.IDLE
            self._set(RadioMode.IDLE)
            self._emit(NetEvent(EventKind.CONNECT_FAILED, ok=False, error=err.strip()[:200]))

    def _op_stop(self) -> None:
        self._nm("connection", "down", self.cfg.ap_connection, timeout=10)
        self._nm("connection", "down", self.cfg.sta_connection, timeout=10)
        self._expected = RadioMode.IDLE
        self._set(RadioMode.IDLE)
        self._emit(NetEvent(EventKind.STOPPED))

    def _active_connection(self) -> str | None:
        rc, out, _ = self._nm("-t", "-f", "GENERAL.CONNECTION", "device", "show", self.cfg.interface, timeout=5)
        if rc != 0:
            return None
        for line in out.splitlines():
            f = split_terse(line)
            if len(f) >= 2 and f[0] == "GENERAL.CONNECTION":
                return f[1] or None
        return None

    def _station_info(self) -> tuple[str | None, str | None]:
        bssid = ip = None
        rc, out, _ = self._nm("-t", "-f", "ACTIVE,BSSID", "device", "wifi", "list", "ifname",
                              self.cfg.interface, "--rescan", "no", timeout=5)
        if rc == 0:
            for line in out.splitlines():
                f = split_terse(line)
                if len(f) >= 2 and f[0] == "yes":
                    bssid = f[1].upper()
        rc, out, _ = self._nm("-t", "-f", "IP4.ADDRESS", "device", "show", self.cfg.interface, timeout=5)
        if rc == 0:
            for line in out.splitlines():
                f = split_terse(line)
                if len(f) >= 2 and f[0].startswith("IP4.ADDRESS"):
                    ip = f[1].split("/")[0]
                    break
        return bssid, ip

    def _check_link(self) -> None:
        if self._expected not in (RadioMode.AP, RadioMode.STATION):
            return
        conn = self._active_connection()
        want = self.cfg.ap_connection if self._expected == RadioMode.AP else self.cfg.sta_connection
        if conn != want:
            log.warning("link lost: expected %s active, got %s", want, conn)
            self._expected = RadioMode.IDLE
            self._set(RadioMode.IDLE)
            self._emit(NetEvent(EventKind.LINK_LOST, {"was": want}))


# ---------------------------------------------------------------------------
# Keyfile generation (used by scripts/configure-network.sh via the CLI below)
# ---------------------------------------------------------------------------

def ap_keyfile(cfg: NetworkConfig, psk: str) -> str:
    return f"""[connection]
id={cfg.ap_connection}
type=wifi
interface-name={cfg.interface}
autoconnect=false

[wifi]
mode=ap
ssid={cfg.ssid}
band={cfg.band}
channel={cfg.channel}

[wifi-security]
key-mgmt=wpa-psk
proto=rsn
pairwise=ccmp
group=ccmp
psk={psk}

[ipv4]
method=shared
address1={cfg.gateway}/{cfg.prefix}

[ipv6]
method=disabled
"""


def sta_keyfile(cfg: NetworkConfig, psk: str) -> str:
    return f"""[connection]
id={cfg.sta_connection}
type=wifi
interface-name={cfg.interface}
autoconnect=false

[wifi]
mode=infrastructure
ssid={cfg.ssid}

[wifi-security]
key-mgmt=wpa-psk
proto=rsn
pairwise=ccmp
group=ccmp
psk={psk}

[ipv4]
method=auto
# never let the cluster network become the default route of a Pi that also has Ethernet
never-default=true

[ipv6]
method=disabled
"""


def write_keyfiles(cfg: NetworkConfig, psk: str, directory: Path) -> list[Path]:
    if not 8 <= len(psk) <= 63:
        raise ValueError("WPA2 passphrase must be 8..63 characters")
    if any(c in psk for c in "\n\r"):
        raise ValueError("passphrase must not contain newlines")
    directory.mkdir(parents=True, exist_ok=True)
    out = []
    for name, text in ((cfg.ap_connection, ap_keyfile(cfg, psk)), (cfg.sta_connection, sta_keyfile(cfg, psk))):
        p = directory / f"{name}.nmconnection"
        tmp = p.with_suffix(".tmp")
        tmp.touch(mode=0o600)
        tmp.chmod(0o600)
        tmp.write_text(text)
        tmp.replace(p)
        out.append(p)
    return out


def _cli() -> None:  # pragma: no cover - exercised by scripts
    import argparse
    import sys

    from backend.config import load_config

    ap = argparse.ArgumentParser(description="write PHOSPHOR NetworkManager keyfiles")
    ap.add_argument("--dir", default="/etc/NetworkManager/system-connections")
    a = ap.parse_args()
    cfg = load_config()
    if not cfg.secrets.wifi_psk:
        sys.exit("secrets.yaml has no wifi_psk")
    for p in write_keyfiles(cfg.network, cfg.secrets.wifi_psk, Path(a.dir)):
        print(p)


if __name__ == "__main__":  # pragma: no cover
    _cli()
