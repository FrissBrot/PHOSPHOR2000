"""Validated request bodies for every admin command (extra fields rejected)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.config import NODE_ID_RE, PLAYABLE_SCENES


class M(BaseModel):
    model_config = ConfigDict(extra="forbid")


NodeId = Field(pattern=NODE_ID_RE.pattern)


class Login(M):
    password: str = Field(min_length=1, max_length=256)


class SetBpm(M):
    bpm: float = Field(ge=40, le=240)


class Tap(M):
    t: float | None = Field(None, description="operator device time in seconds (intervals only)")
    phase: bool = True


class Nudge(M):
    ms: float = Field(ge=-500, le=500)


class Empty(M):
    pass


class Manual(M):
    enabled: bool


Quantize = Literal["bar", "beat", "eighth", "sixteenth"]


class Scene(M):
    scene: str
    targets: list[str] | None = None
    quantize: Quantize | None = None
    bars: int | None = Field(None, ge=1, le=1024)

    @field_validator("scene")
    @classmethod
    def _s(cls, v: str) -> str:
        if v not in PLAYABLE_SCENES:
            raise ValueError("unknown scene")
        return v

    @field_validator("targets")
    @classmethod
    def _t(cls, v):
        if v is not None:
            for x in v:
                if not NODE_ID_RE.match(x):
                    raise ValueError("bad node id")
        return v


class ClearScene(M):
    targets: list[str] | None = None
    quantize: Quantize | None = None


class SetShared(M):
    patch: dict[str, dict]


class ImportShared(M):
    shared: dict


class Handover(M):
    target: str = NodeId


class WallTime(M):
    unix: float


class Restart(M):
    target: str = NodeId
    what: Literal["renderer", "core", "audio", "reboot"] = "renderer"


class CalStart(M):
    mode: Literal["single", "all", "click"] = "single"
    target: str | None = Field(None, pattern=NODE_ID_RE.pattern)
    bpm: float | None = Field(None, ge=40, le=180)  # <= 3 flashes/s


class CalTap(M):
    cluster: float
    value: int = Field(1, ge=0, le=2)


class CalAdjust(M):
    delta_ms: float | None = Field(None, ge=-100, le=100)
    offset_ms: float | None = Field(None, ge=-500, le=500)
    apply_suggestion: bool = False


class CalReset(M):
    target: str | None = Field(None, pattern=NODE_ID_RE.pattern)


COMMANDS: dict[str, type[M]] = {
    "set_bpm": SetBpm, "tap": Tap, "nudge": Nudge, "downbeat": Empty, "manual": Manual,
    "scene": Scene, "clear_scene": ClearScene, "set_shared": SetShared, "import_shared": ImportShared,
    "handover": Handover, "set_wall_time": WallTime, "restart": Restart,
    "cal_start": CalStart, "cal_tap": CalTap, "cal_adjust": CalAdjust, "cal_save": Empty,
    "cal_cancel": Empty, "cal_reset": CalReset,
}
