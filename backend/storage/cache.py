"""Local persistence: atomic JSON state cache + small SQLite event log.

SD cards hate frequent writes: the cache is written at most every 10 s and
only when something changed; the event log is capped.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger("phos.storage")


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "unknown-boot"


def atomic_write_json(path: Path, data: dict, mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)
    try:
        dfd = os.open(path.parent, os.O_DIRECTORY)
        os.fsync(dfd)
        os.close(dfd)
    except OSError:
        pass


def read_json(path: Path) -> dict | None:
    for p in (path, path.with_name(path.name + ".bak")):
        try:
            return json.loads(p.read_text())
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            log.warning("cache %s unreadable: %s", p, exc)
    return None


class StateCache:
    def __init__(self, directory: Path) -> None:
        self.path = directory / "state.json"
        self.last_write = 0.0

    def load(self) -> dict | None:
        return read_json(self.path)

    def save(self, blob: dict) -> None:
        if self.path.exists():
            try:
                os.replace(self.path, self.path.with_name(self.path.name + ".bak"))
            except OSError:
                pass
        atomic_write_json(self.path, blob)
        self.last_write = time.monotonic()


class EventLog:
    """Event log in RAM (journald keeps a copy), calibration history on disk.

    The SD card only sees a write when a calibration is saved — continuous log writes are what
    kills SD cards and what corrupts them on power loss.
    """
    MAX_ROWS = 5000

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
        self.db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, wall REAL, node TEXT,"
                        " level TEXT, msg TEXT)")
        self.cal_db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.cal_db.execute("PRAGMA journal_mode=DELETE")
        self.cal_db.execute("PRAGMA synchronous=FULL")
        self.cal_db.execute("CREATE TABLE IF NOT EXISTS calibrations (id INTEGER PRIMARY KEY, wall REAL, node TEXT,"
                            " offset_ms REAL, detail TEXT)")
        self.lock = threading.Lock()
        self.n = 0

    def add(self, wall: float, node: str, level: str, msg: str) -> None:
        with self.lock:
            self.db.execute("INSERT INTO events (wall, node, level, msg) VALUES (?,?,?,?)", (wall, node, level, msg[:1000]))
            self.n += 1
            if self.n % 200 == 0:
                self.db.execute("DELETE FROM events WHERE id <= (SELECT MAX(id) FROM events) - ?", (self.MAX_ROWS,))

    def add_calibration(self, wall: float, node: str, offset_ms: float, detail: dict) -> None:
        with self.lock:
            self.cal_db.execute("INSERT INTO calibrations (wall, node, offset_ms, detail) VALUES (?,?,?,?)",
                            (wall, node, offset_ms, json.dumps(detail)[:4000]))

    def recent(self, n: int = 200) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT wall, node, level, msg FROM events ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [{"wall": r[0], "node": r[1], "level": r[2], "msg": r[3]} for r in reversed(rows)]

    def calibrations(self, n: int = 50) -> list[dict]:
        with self.lock:
            rows = self.cal_db.execute("SELECT wall, node, offset_ms, detail FROM calibrations ORDER BY id DESC LIMIT ?",
                                   (n,)).fetchall()
        return [{"wall": r[0], "node": r[1], "offset_ms": r[2], "detail": json.loads(r[3])} for r in rows]
