"""Golden vectors shared by Python and JavaScript tests (python -m tests.golden > tests/fixtures/golden.json)."""
import json

from backend.sync.prng import Mulberry32, fnv1a, mix32, seed_for
from backend.sync.timeline import Timeline, reconcile


def build() -> dict:
    out = {"fnv1a": {s: fnv1a(s) for s in ["", "dance", "rain", "countdown", "äöü-2000"]},
           "mix32": [[v, mix32(*v)] for v in ([1, 2, 3], [0], [0xFFFFFFFF, 7], [123456789, 987654321, 42])],
           "seed_for": [[0x2000, "rain", 64, seed_for(0x2000, "rain", 64)]],
           "mulberry": {}, "timelines": []}
    for seed in (0, 1, 0x2000, 0xDEADBEEF):
        r = Mulberry32(seed)
        out["mulberry"][str(seed)] = [r.next_u32() for _ in range(6)]
    a = Timeline(bpm=128, anchor_time=5000.25, anchor_beat=77.5)
    b = reconcile(Timeline(bpm=120, anchor_time=4990, anchor_beat=60.1), Timeline(bpm=121, anchor_time=4990, anchor_beat=60.12), 5001.0).timeline
    c = reconcile(Timeline(bpm=120, anchor_time=4990, anchor_beat=60.1), Timeline(bpm=124, anchor_time=4990, anchor_beat=69.7), 5001.0).timeline
    for tl in (a, b, c):
        ts = [4999.0 + i * 0.37 for i in range(40)]
        out["timelines"].append({"tl": tl.to_dict(), "t": ts, "beat": [tl.beat_at(t) for t in ts],
                                 "inv": [[bb, tl.time_of_beat(bb)] for bb in (70.0, 80.25, 99.5)]})
    return out


if __name__ == "__main__":
    print(json.dumps(build(), indent=1))
