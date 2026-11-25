"""Radio abstraction used by the election state machine.

All operations are *requests*; completion is reported asynchronously through
``poll_events()``. This mirrors reality (nmcli calls take seconds) and lets
the same state machine run against NetworkManager or the discrete-event
simulator.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum


class RadioMode(str, Enum):
    IDLE = "idle"
    AP = "ap"
    STATION = "station"
    BUSY = "busy"
    UNAVAILABLE = "unavailable"


class EventKind(str, Enum):
    SCAN_RESULT = "scan_result"      # data: list[ScanEntry]
    HOTSPOT_UP = "hotspot_up"        # data: {"bssid": str}
    HOTSPOT_FAILED = "hotspot_failed"
    CONNECTED = "connected"          # data: {"bssid": str, "ip": str}
    CONNECT_FAILED = "connect_failed"
    LINK_LOST = "link_lost"
    STOPPED = "stopped"


@dataclass
class ScanEntry:
    ssid: str
    bssid: str
    signal: int  # 0..100


@dataclass
class NetEvent:
    kind: EventKind
    data: dict | list | None = None
    ok: bool = True
    error: str = ""


@dataclass
class RadioStatus:
    mode: RadioMode
    bssid: str | None = None      # AP mode: own BSSID; station: AP BSSID
    ip: str | None = None
    own_mac: str | None = None
    extra: dict = field(default_factory=dict)


class NetworkAdapter(ABC):
    @abstractmethod
    def request_scan(self) -> None: ...

    @abstractmethod
    def start_hotspot(self) -> None: ...

    @abstractmethod
    def connect(self, bssid: str | None = None) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def status(self) -> RadioStatus: ...

    @abstractmethod
    def poll_events(self) -> list[NetEvent]: ...

    def supports_scan_in_ap_mode(self) -> bool:
        return False

    def close(self) -> None:
        pass


class NullAdapter(NetworkAdapter):
    """Used when network.manage is false: never touches the radio.

    The node then behaves as a standalone renderer that still follows a leader
    if the OS (configured by hand) happens to put it on the cluster network.
    """

    def __init__(self) -> None:
        self._events: list[NetEvent] = []

    def request_scan(self) -> None:
        self._events.append(NetEvent(EventKind.SCAN_RESULT, []))

    def start_hotspot(self) -> None:
        self._events.append(NetEvent(EventKind.HOTSPOT_FAILED, ok=False, error="network management disabled"))

    def connect(self, bssid: str | None = None) -> None:
        self._events.append(NetEvent(EventKind.CONNECT_FAILED, ok=False, error="network management disabled"))

    def stop(self) -> None:
        pass

    def status(self) -> RadioStatus:
        return RadioStatus(RadioMode.UNAVAILABLE)

    def poll_events(self) -> list[NetEvent]:
        ev, self._events = self._events, []
        return ev
