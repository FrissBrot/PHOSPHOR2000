"""Typed configuration models and loader.

Layering (later wins):
  1. config/default.yaml               (repo defaults)
  2. /etc/phosphor2000/node.yaml      (node identity / local overrides)
  3. /etc/phosphor2000/secrets.yaml   (0600: Wi-Fi PSK, cluster key, admin hash)

The ``SharedConfig`` part (scenes, safety, calibration, timeline offsets) is
replicated across the cluster with a revision number; the leader is the
authority. Node identity, network and secrets are never replicated.
"""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "config" / "default.yaml"
ETC_DIR = Path(os.environ.get("PHOSPHOR_ETC", "/etc/phosphor2000"))
STATE_DIR = Path(os.environ.get("PHOSPHOR_STATE", "/var/lib/phosphor2000"))

NODE_ID_RE = re.compile(r"^pi-0[1-9]$")
SCENES = ("boot", "dance", "spectrum", "grid", "rain", "countdown", "mattliball", "guggen", "stollen",
          "mottos", "bars", "hell", "calibration")
PLAYABLE_SCENES = SCENES[:-1]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NodeConfig(Strict):
    id: str = "pi-01"
    priority: int = Field(100, ge=1, le=1000)
    priority_table: dict[str, int] = {}

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not NODE_ID_RE.match(v):
            raise ValueError("node id must look like pi-01 .. pi-09")
        return v


class NetworkConfig(Strict):
    manage: bool = True
    interface: str = Field("wlan0", pattern=r"^[a-zA-Z0-9_.-]{1,15}$")
    ssid: str = Field("PHOSPHOR2000", min_length=1, max_length=32)
    gateway: str = Field("192.168.50.1", pattern=r"^\d{1,3}(\.\d{1,3}){3}$")
    prefix: int = Field(24, ge=8, le=30)
    band: Literal["bg", "a"] = "bg"
    channel: int = Field(6, ge=1, le=165)
    ap_connection: str = Field("phosphor-ap", pattern=r"^[a-zA-Z0-9_-]{1,32}$")
    sta_connection: str = Field("phosphor-sta", pattern=r"^[a-zA-Z0-9_-]{1,32}$")
    beacon_interval: float = Field(1.0, gt=0.05, le=10)
    suspect_after: float = Field(3.5, gt=0)
    failure_after: float = Field(8.0, gt=0)
    discovery_window: float = Field(10.0, gt=0)
    backoff_slot: float = Field(12.0, gt=0)
    backoff_jitter: float = Field(1.0, ge=0)
    min_leader_tenure: float = Field(20.0, ge=0)
    yield_holddown: float = Field(30.0, ge=0)
    hotspot_start_timeout: float = Field(25.0, gt=0)
    connect_timeout: float = Field(20.0, gt=0)
    degraded_retry: float = Field(30.0, gt=0)
    udp_port: int = Field(47800, ge=1024, le=65535)
    local_port: int = Field(47810, ge=1024, le=65535)

    @model_validator(mode="after")
    def _order(self) -> "NetworkConfig":
        if self.failure_after <= self.suspect_after:
            raise ValueError("failure_after must exceed suspect_after")
        return self


class ServerConfig(Strict):
    host: str = "0.0.0.0"
    port: int = Field(8080, ge=1, le=65535)
    render_local_only: bool = True


class SyncConfig(Strict):
    ping_interval: float = Field(1.0, gt=0.05)
    burst_count: int = Field(6, ge=1, le=64)
    window: int = Field(32, ge=4, le=512)
    max_slew_ppm: float = Field(2000, gt=0)
    step_threshold: float = Field(0.050, gt=0)
    outlier_delay_factor: float = Field(2.0, ge=1.0)


class TimelineConfig(Strict):
    default_bpm: float = Field(120.0, ge=20, le=300)
    beats_per_bar: int = Field(4, ge=1, le=16)
    min_bpm: float = 60.0
    max_bpm: float = 200.0
    slew_threshold_beats: float = Field(0.06, gt=0, lt=0.5)
    slew_duration_beats: float = Field(4.0, gt=0)
    global_visual_offset_ms: float = Field(0.0, ge=-500, le=500)
    display_latency_ms: float = Field(40.0, ge=0, le=500)
    schedule_lead_s: float = Field(0.6, ge=0.05, le=10)


class PlaylistItem(Strict):
    scene: str
    bars: int = Field(8, ge=1, le=1024)

    @field_validator("scene")
    @classmethod
    def _scene(cls, v: str) -> str:
        if v not in PLAYABLE_SCENES:
            raise ValueError(f"unknown scene {v!r}")
        return v


class IndividualAssignment(Strict):
    scene: str | None = None
    playlist: list[PlaylistItem] | None = None
    rotate: int = Field(0, ge=0, le=64)

    @field_validator("scene")
    @classmethod
    def _scene(cls, v: str | None) -> str | None:
        if v is not None and v not in PLAYABLE_SCENES:
            raise ValueError(f"unknown scene {v!r}")
        return v


class ChoreoStep(Strict):
    bars: int = Field(8, ge=1, le=1024)
    assign: dict[str, str] | None = None
    pattern: Literal["rotate", "alternate", "wave"] | None = None
    scenes: list[str] | None = None

    @model_validator(mode="after")
    def _check(self) -> "ChoreoStep":
        names = list((self.assign or {}).values()) + list(self.scenes or [])
        for s in names:
            if s not in PLAYABLE_SCENES:
                raise ValueError(f"unknown scene {s!r}")
        if self.assign is None and not self.scenes:
            raise ValueError("choreography step needs assign or scenes")
        for k in (self.assign or {}):
            if k != "*" and not NODE_ID_RE.match(k):
                raise ValueError(f"bad node id {k!r}")
        return self


class ScenesConfig(Strict):
    mode: Literal["GLOBAL", "INDIVIDUAL", "CHOREOGRAPHED"] = "GLOBAL"
    quantize: Literal["bar", "beat", "eighth", "sixteenth"] = "bar"
    playlist: list[PlaylistItem] = Field(default_factory=lambda: [PlaylistItem(scene="boot", bars=8)])
    individual: dict[str, IndividualAssignment] = {}
    choreography: list[ChoreoStep] = []
    countdown_target: str = Field("2027-02-05T20:00:00", pattern=r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?)?$")
    countdown_label: str = Field("MATTLIBALL", max_length=24)
    intensity: float = Field(0.8, ge=0.0, le=1.0)
    phosphor: str = Field("#39ff14", pattern=r"^#[0-9a-fA-F]{6}$")  # P1 green; amber #ffb000, blue #7dd3fc, white #e8f4ff

    @field_validator("playlist")
    @classmethod
    def _nonempty(cls, v: list[PlaylistItem]) -> list[PlaylistItem]:
        if not v:
            raise ValueError("playlist must not be empty")
        return v

    @field_validator("individual")
    @classmethod
    def _ids(cls, v: dict[str, IndividualAssignment]) -> dict[str, IndividualAssignment]:
        for k in v:
            if not NODE_ID_RE.match(k):
                raise ValueError(f"bad node id {k!r}")
        return v


class SafetyConfig(Strict):
    reduced_flashing: bool = False
    max_flash_hz: float = Field(3.0, gt=0, le=3.0)  # WCAG 2.3.1: <= 3 flashes/s
    max_flash_luminance: float = Field(0.85, ge=0.05, le=1.0)


class CalibrationConfig(Strict):
    bpm: float = Field(120.0, ge=40, le=180)  # flash rate <= 3 Hz
    min_taps: int = Field(16, ge=4, le=64)
    max_taps: int = Field(32, ge=4, le=128)
    outlier_ms: float = Field(150.0, gt=0)
    double_press_fraction: float = Field(0.35, gt=0, lt=1)
    offsets_ms: dict[str, float] = {}

    @field_validator("offsets_ms")
    @classmethod
    def _offs(cls, v: dict[str, float]) -> dict[str, float]:
        for k, ms in v.items():
            if not NODE_ID_RE.match(k):
                raise ValueError(f"bad node id {k!r}")
            if not -500.0 <= ms <= 500.0:
                raise ValueError("offset must be within ±500 ms")
        return v


class Overscan(Strict):
    top: float = Field(0.06, ge=0, le=0.25)
    bottom: float = Field(0.06, ge=0, le=0.25)
    left: float = Field(0.07, ge=0, le=0.25)
    right: float = Field(0.07, ge=0, le=0.25)


class DisplayConfig(Strict):
    virtual_width: int = 640
    virtual_height: int = 480
    overscan: Overscan = Overscan()
    fps_cap: int = Field(25, ge=5, le=60)
    glow: int = Field(1, ge=0, le=2)          # phosphor glow (canvas shadowBlur): 0 off, 1 light, 2 strong
    crt_overlay: bool = True                  # scanlines + vignette
    hud: bool = False                         # small status line (always shown in abnormal states)


class AudioConfig(Strict):
    enabled: Literal["auto", "on", "off"] | bool = "auto"
    device: str | None = None
    samplerate: int = Field(48000, ge=8000, le=192000)
    blocksize: int = Field(512, ge=64, le=8192)
    channels: int = Field(1, ge=1, le=8)
    silence_rms: float = Field(0.003, ge=0)
    silence_hold_s: float = Field(2.5, gt=0)
    input_latency_ms: float | None = None
    min_bpm: float = 80.0
    max_bpm: float = 180.0


class SiteConfig(Strict):
    # Wi-Fi regulatory country (ISO 3166 alpha-2), applied on every Pi by
    # phosphor-country@XX.service. "" = leave the OS setting alone.
    wifi_country: str = Field("", pattern=r"^([A-Z]{2})?$")


class SharedConfig(Strict):
    """The replicated, revisioned part of the configuration."""

    site: SiteConfig = SiteConfig()
    scenes: ScenesConfig = ScenesConfig()
    safety: SafetyConfig = SafetyConfig()
    calibration: CalibrationConfig = CalibrationConfig()
    timeline: TimelineConfig = TimelineConfig()
    display: DisplayConfig = DisplayConfig()


class Secrets(Strict):
    wifi_psk: str = ""
    cluster_key: str = ""
    admin_password_hash: str = ""
    session_secret: str = ""

    @field_validator("wifi_psk")
    @classmethod
    def _psk(cls, v: str) -> str:
        if v and not (8 <= len(v) <= 63):
            raise ValueError("WPA2 passphrase must be 8..63 characters")
        return v


class AppConfig(Strict):
    node: NodeConfig = NodeConfig()
    network: NetworkConfig = NetworkConfig()
    server: ServerConfig = ServerConfig()
    sync: SyncConfig = SyncConfig()
    timeline: TimelineConfig = TimelineConfig()
    scenes: ScenesConfig = ScenesConfig()
    safety: SafetyConfig = SafetyConfig()
    calibration: CalibrationConfig = CalibrationConfig()
    display: DisplayConfig = DisplayConfig()
    site: SiteConfig = SiteConfig()
    audio: AudioConfig = AudioConfig()
    secrets: Secrets = Secrets()

    def shared(self) -> SharedConfig:
        return SharedConfig(
            site=self.site,
            scenes=self.scenes,
            safety=self.safety,
            calibration=self.calibration,
            timeline=self.timeline,
            display=self.display,
        )

    def with_shared(self, shared: SharedConfig) -> "AppConfig":
        return self.model_copy(
            update={
                "site": shared.site,
                "scenes": shared.scenes,
                "safety": shared.safety,
                "calibration": shared.calibration,
                "timeline": shared.timeline,
                "display": shared.display,
            }
        )


def deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    return data


def load_config(
    default_path: Path = DEFAULT_CONFIG,
    node_path: Path | None = None,
    secrets_path: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> AppConfig:
    node_path = node_path or ETC_DIR / "node.yaml"
    secrets_path = secrets_path or ETC_DIR / "secrets.yaml"
    data = _read_yaml(default_path)
    data = deep_merge(data, _read_yaml(node_path))
    if secrets_path.exists():
        mode = secrets_path.stat().st_mode & 0o077
        if mode:
            raise PermissionError(f"{secrets_path} must not be group/world accessible (chmod 600)")
        data = deep_merge(data, {"secrets": _read_yaml(secrets_path)})
    data = deep_merge(data, overrides or {})
    cfg = AppConfig.model_validate(data)
    # Fill priority from the table when node.yaml only provides the id.
    node_over = _read_yaml(node_path).get("node", {}) if node_path.exists() else {}
    if "priority" not in node_over and cfg.node.id in cfg.node.priority_table and "priority" not in (overrides or {}).get("node", {}):
        cfg.node.priority = cfg.node.priority_table[cfg.node.id]
    return cfg
