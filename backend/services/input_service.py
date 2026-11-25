"""cyberspace-input: USB keyboard -> core, with kernel monotonic timestamps.

Uses the Linux evdev interface directly (/dev/input/event*):
* ``EVIOCSCLOCKID(CLOCK_MONOTONIC)`` makes the kernel stamp events with the
  same clock as Python's ``time.monotonic()``, captured in the interrupt path
  — independent of scheduling delay in this process or in Chromium.
* only EV_KEY events are forwarded; value 1 = press, 2 = autorepeat,
  0 = release (the core ignores repeats/releases for taps).
* devices are not grabbed (EVIOCGRAB) so the console/kiosk keep working.
Hot-plug: rescans every 5 s. Requires membership in group ``input``.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import select
import socket
import struct
import time
from pathlib import Path

log = logging.getLogger("cs2k.input")

EV_KEY = 0x01
KEY_ENTER = 28
CLOCK_MONOTONIC = 1
EVIOCSCLOCKID = 0x400445A0          # _IOW('E', 0xa0, int)
EVENT_FMT = "llHHi"                 # struct input_event (native long = timeval fields)
EVENT_SIZE = struct.calcsize(EVENT_FMT)
SOCK = Path(os.environ.get("CYBERSPACE_RUN", "/run/cyberspace2000")) / "core.sock"


def is_keyboard(event_path: Path, sys_root: Path = Path("/sys/class/input")) -> bool:
    """Device advertises KEY_ENTER in its key capability bitmap."""
    cap = sys_root / event_path.name / "device" / "capabilities" / "key"
    try:
        words = cap.read_text().split()
    except OSError:
        return False
    bits = 0
    for w in words:  # most significant word first, each a native long in hex
        bits = (bits << (8 * struct.calcsize("l"))) | int(w, 16)
    return bool(bits >> KEY_ENTER & 1)


def parse_events(buf: bytes) -> list[tuple[float, int, int, int]]:
    out = []
    for off in range(0, len(buf) - EVENT_SIZE + 1, EVENT_SIZE):
        sec, usec, typ, code, value = struct.unpack_from(EVENT_FMT, buf, off)
        out.append((sec + usec / 1e6, typ, code, value))
    return out


class InputService:
    def __init__(self, dev_root: Path = Path("/dev/input"), sock_path: Path = SOCK) -> None:
        self.dev_root = dev_root
        self.sock_path = str(sock_path)
        self.fds: dict[int, Path] = {}
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.monotonic_ok: dict[int, bool] = {}

    def scan(self) -> None:
        known = set(self.fds.values())
        for p in sorted(self.dev_root.glob("event*")):
            if p in known or not is_keyboard(p):
                continue
            try:
                fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK)
            except OSError as exc:
                log.warning("cannot open %s: %s", p, exc)
                continue
            try:
                fcntl.ioctl(fd, EVIOCSCLOCKID, struct.pack("i", CLOCK_MONOTONIC))
                self.monotonic_ok[fd] = True
            except OSError:
                self.monotonic_ok[fd] = False
                log.warning("%s: EVIOCSCLOCKID unsupported; using arrival time", p)
            self.fds[fd] = p
            log.info("keyboard: %s", p)

    def forward(self, t: float, code: int, value: int) -> None:
        msg = {"t": "key", "code": code, "value": value, "local": t}
        try:
            self.sock.sendto(json.dumps(msg).encode(), self.sock_path)
        except OSError:
            pass

    def run(self) -> None:
        last_scan = 0.0
        while True:
            if time.monotonic() - last_scan > 5.0:
                last_scan = time.monotonic()
                self.scan()
            if not self.fds:
                time.sleep(1.0)
                continue
            r, _, _ = select.select(list(self.fds), [], [], 1.0)
            for fd in r:
                try:
                    buf = os.read(fd, EVENT_SIZE * 64)
                except OSError:  # unplugged
                    log.info("keyboard removed: %s", self.fds.get(fd))
                    os.close(fd)
                    self.fds.pop(fd, None)
                    continue
                arrival = time.monotonic()
                for t, typ, code, value in parse_events(buf):
                    if typ != EV_KEY:
                        continue
                    self.forward(t if self.monotonic_ok.get(fd) else arrival, code, value)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    InputService().run()


if __name__ == "__main__":
    main()
