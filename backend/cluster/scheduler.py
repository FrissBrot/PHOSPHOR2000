"""Deterministic scene scheduling on the musical timeline.

Everything is expressed in *beats* of the distributed timeline, so every node
computes the same plan from the same replicated ``ScheduleState`` without
exchanging per-beat messages. Plans are recomputed locally while disconnected.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable

from backend.config import ScenesConfig
from backend.sync.prng import seed_for

QUANT = {"bar": None, "beat": 1.0, "eighth": 0.5, "sixteenth": 0.25}
RES = 0.25  # planning resolution: sixteenth notes


def quantum_beats(quantize: str, beats_per_bar: int) -> float:
    q = QUANT[quantize]
    return float(beats_per_bar) if q is None else q


@dataclass
class Override:
    """Manual / choreographed scene change starting on a grid boundary."""

    scene: str
    start_beat: float
    end_beat: float | None = None           # None = until cleared
    targets: list[str] | None = None         # None = all nodes
    id: int = 0

    def applies(self, node_id: str, beat: float) -> bool:
        if beat < self.start_beat or (self.end_beat is not None and beat >= self.end_beat):
            return False
        return self.targets is None or node_id in self.targets

    def to_dict(self) -> dict:
        return {"scene": self.scene, "start_beat": self.start_beat, "end_beat": self.end_beat,
                "targets": self.targets, "id": self.id}

    @classmethod
    def from_dict(cls, d: dict) -> "Override":
        return cls(scene=d["scene"], start_beat=float(d["start_beat"]),
                   end_beat=None if d.get("end_beat") is None else float(d["end_beat"]),
                   targets=d.get("targets"), id=int(d.get("id", 0)))


@dataclass
class ScheduleEra:
    """A scenes configuration that is valid from ``origin_beat`` onwards."""

    origin_beat: float
    scenes: ScenesConfig


@dataclass
class ScheduleState:
    eras: list[ScheduleEra]
    overrides: list[Override] = field(default_factory=list)
    nodes: list[str] = field(default_factory=lambda: [f"pi-0{i}" for i in range(1, 6)])
    seed: int = 0x2000
    beats_per_bar: int = 4
    revision: int = 0

    def era_at(self, beat: float) -> ScheduleEra:
        best = self.eras[0]
        for e in self.eras:
            if e.origin_beat <= beat + 1e-9 and e.origin_beat >= best.origin_beat:
                best = e
        return best

    def to_dict(self) -> dict:
        return {
            "eras": [{"origin_beat": e.origin_beat, "scenes": e.scenes.model_dump()} for e in self.eras],
            "overrides": [o.to_dict() for o in self.overrides],
            "nodes": list(self.nodes), "seed": self.seed,
            "beats_per_bar": self.beats_per_bar, "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ScheduleState":
        return cls(
            eras=[ScheduleEra(float(e["origin_beat"]), ScenesConfig.model_validate(e["scenes"])) for e in d["eras"]],
            overrides=[Override.from_dict(o) for o in d.get("overrides", [])],
            nodes=list(d.get("nodes", [])), seed=int(d.get("seed", 0x2000)),
            beats_per_bar=int(d.get("beats_per_bar", 4)), revision=int(d.get("revision", 0)),
        )

    # ---- mutations (leader only) ------------------------------------------
    def set_scenes(self, scenes: ScenesConfig, from_beat: float) -> None:
        """New scene config becomes valid at ``from_beat`` (a grid boundary)."""
        keep = [e for e in self.eras if e.origin_beat < from_beat]
        # Keep only the era active right before the change; older ones are irrelevant.
        keep = sorted(keep, key=lambda e: e.origin_beat)[-1:]
        self.eras = keep + [ScheduleEra(from_beat, scenes)]
        self.revision += 1

    def add_override(self, ov: Override, now_beat: float) -> None:
        if not ov.id:
            ov.id = max([o.id for o in self.overrides] + [0]) + 1
        # Expire finished overrides; a new global override supersedes open-ended ones for the same targets.
        alive: list[Override] = []
        for o in self.overrides:
            if o.end_beat is not None and o.end_beat <= now_beat:
                continue
            if o.end_beat is None and _targets_cover(ov.targets, o.targets):
                o.end_beat = max(ov.start_beat, o.start_beat)
            alive.append(o)
        alive.append(ov)
        self.overrides = alive[-32:]
        self.revision += 1

    def clear_overrides(self, at_beat: float, targets: list[str] | None = None, scene: str | None = None) -> None:
        for o in self.overrides:
            if scene is not None and o.scene != scene:
                continue
            if _targets_cover(targets, o.targets) and (o.end_beat is None or o.end_beat > at_beat):
                o.end_beat = max(at_beat, o.start_beat)
        self.revision += 1


def _targets_cover(a: list[str] | None, b: list[str] | None) -> bool:
    """True if target set a covers target set b."""
    if a is None:
        return True
    if b is None:
        return False
    return set(b) <= set(a)


@dataclass(frozen=True)
class Segment:
    scene: str
    start_beat: float
    end_beat: float
    seed: int
    origin_beat: float  # beat at which this scene instance began (for scene-local time)

    def to_dict(self) -> dict:
        return {"scene": self.scene, "start_beat": self.start_beat, "end_beat": self.end_beat,
                "seed": self.seed, "origin_beat": self.origin_beat}


def _node_index(state: ScheduleState, node_id: str) -> int:
    if node_id in state.nodes:
        return state.nodes.index(node_id)
    try:
        return int(node_id.split("-")[1]) - 1
    except (IndexError, ValueError):
        return 0


def _playlist_at(items, rel_beat: float, bpb: int, rotate: int = 0) -> tuple[str, float]:
    lengths = [it.bars * bpb for it in items]
    cycle = sum(lengths)
    n = len(items)
    # rotation: start the cycle at item `rotate`
    shift = sum(lengths[: rotate % n])
    pos = (rel_beat + shift) % cycle
    cycle_start = rel_beat - pos  # relative beat where this rotated cycle started
    acc = 0.0
    for i in range(n):
        if pos < acc + lengths[i] - 1e-9:
            # item i starts at acc within rotated cycle
            return items[i].scene, cycle_start + acc
        acc += lengths[i]
    return items[-1].scene, cycle_start + acc - lengths[-1]


def base_scene_at(state: ScheduleState, node_id: str, beat: float) -> tuple[str, float]:
    """(scene, instance_origin_beat) from the era configuration, ignoring overrides."""
    era = state.era_at(beat)
    sc = era.scenes
    bpb = state.beats_per_bar
    rel = beat - era.origin_beat
    idx = _node_index(state, node_id)
    if sc.mode == "INDIVIDUAL":
        a = sc.individual.get(node_id)
        if a and a.scene:
            return a.scene, era.origin_beat
        items = (a.playlist if a and a.playlist else sc.playlist)
        rot = a.rotate if a and a.playlist else idx
        scene, start = _playlist_at(items, rel, bpb, rot)
        return scene, era.origin_beat + start
    if sc.mode == "CHOREOGRAPHED" and sc.choreography:
        steps = sc.choreography
        lengths = [s.bars * bpb for s in steps]
        cycle = sum(lengths)
        pos = rel % cycle
        cyc0 = rel - pos
        acc = 0.0
        for st, ln in zip(steps, lengths):
            if pos < acc + ln - 1e-9:
                in_step = pos - acc
                step_origin = era.origin_beat + cyc0 + acc
                if st.assign is not None:
                    scene = st.assign.get(node_id, st.assign.get("*", sc.playlist[0].scene))
                    return scene, step_origin
                scenes = st.scenes or [sc.playlist[0].scene]
                n = len(scenes)
                if st.pattern == "alternate":
                    k = math.floor(in_step / bpb)
                    return scenes[(idx + k) % min(2, n)], step_origin + k * bpb
                if st.pattern == "wave":
                    # each node switches one beat after its predecessor
                    shifted = in_step - idx
                    k = math.floor(shifted / bpb)
                    return scenes[k % n], step_origin + k * bpb + idx
                # rotate (default): node i shows scene (i + bar) every bar
                k = math.floor(in_step / bpb)
                return scenes[(idx + k) % n], step_origin + k * bpb
            acc += ln
    scene, start = _playlist_at(sc.playlist, rel, bpb)
    return scene, era.origin_beat + start


def scene_at(state: ScheduleState, node_id: str, beat: float) -> tuple[str, float]:
    active = [o for o in state.overrides if o.applies(node_id, beat)]
    if active:
        o = max(active, key=lambda o: (o.start_beat, o.id))
        return o.scene, o.start_beat
    return base_scene_at(state, node_id, beat)


def plan(state: ScheduleState, node_id: str, from_beat: float, to_beat: float) -> list[Segment]:
    """Segments covering [from_beat, to_beat) at sixteenth-note resolution."""
    start = math.floor(from_beat / RES) * RES
    segs: list[Segment] = []
    cur: tuple[str, float] | None = None
    seg_start = start
    b = start
    while b < to_beat:
        key = scene_at(state, node_id, b)
        if cur is None:
            cur, seg_start = key, b
        elif key != cur:
            segs.append(_mk(state, cur, seg_start, b))
            cur, seg_start = key, b
        b += RES
    if cur is not None:
        segs.append(_mk(state, cur, seg_start, b))
    return segs


def _mk(state: ScheduleState, key: tuple[str, float], start: float, end: float) -> Segment:
    scene, origin = key
    return Segment(scene, start, end, seed_for(state.seed, scene, int(round(origin * 4))), origin)


def default_schedule(scenes: ScenesConfig, nodes: Iterable[str], seed: int = 0x2000, beats_per_bar: int = 4) -> ScheduleState:
    return ScheduleState(eras=[ScheduleEra(0.0, scenes)], nodes=sorted(nodes), seed=seed, beats_per_bar=beats_per_bar)
