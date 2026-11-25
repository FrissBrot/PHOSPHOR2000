"""Generate the SIMULATED results table for docs/test-report.md.

    python -m tools.sim_report > /tmp/sim.md
"""
import math
import statistics

from backend.cluster.simulation import SimWorld


def run_until(w, pred, limit, step=0.25):
    t0 = w.now
    while w.now - t0 < limit:
        if pred():
            return w.now - t0
        w.run_for(step)
    return math.nan


def settled(w, leader=None):
    ls = w.leaders()
    return len(ls) == 1 and (leader is None or ls[0] == leader) and \
        all(s in ("LEADER", "FOLLOWER") for s in w.states().values())


def stats(xs):
    xs = [x for x in xs if not math.isnan(x)]
    if not xs:
        return "n/a"
    return f"{min(xs):.1f} / {statistics.median(xs):.1f} / {max(xs):.1f}"


def main():
    seeds = range(1, 21)
    print("| scenario | nodes | runs | result (min / median / max) |")
    print("|---|---|---|---|")
    for n in (3, 5):
        boot = []
        fo = []
        spread = []
        for s in seeds:
            w = SimWorld(n, seed=s)
            boot.append(run_until(w, lambda: settled(w, "pi-01"), 200))
            w.run_for(20)
            w.crash("pi-01")
            fo.append(run_until(w, lambda: settled(w, "pi-02"), 200))
            w.run_for(20)
            spread.append(w.beat_spread_ms())
        print(f"| simultaneous cold boot -> single leader (s) | {n} | {len(seeds)} | {stats(boot)} |")
        print(f"| leader crash -> replacement leader, all following (s) | {n} | {len(seeds)} | {stats(fo)} |")
        print(f"| beat spread between CRTs 20 s after failover (ms) | {n} | {len(seeds)} | {stats(spread)} |")
    sb = []
    for s in seeds:
        w = SimWorld(5, seed=100 + s)
        run_until(w, lambda: settled(w, "pi-01"), 200)
        w.partition(["pi-01", "pi-04"], ["pi-02", "pi-03", "pi-05"])
        w.run_for(120)
        w.heal()
        sb.append(run_until(w, lambda: settled(w), 600))
    print(f"| split brain (2 hotspots) healed -> single leader (s) | 5 | {len(seeds)} | {stats(sb)} |")
    for bpm in (100, 120, 128, 140, 160):
        sp, err = [], []
        for s in range(5):
            w = SimWorld(4, seed=200 + s)
            w.start_music(bpm, ["pi-03"])
            run_until(w, lambda: settled(w, "pi-03"), 300)  # audio Pi becomes master
            w.run_for(45)
            sp.append(w.beat_spread_ms())
            err.append(max(abs(w.music_phase_error_ms(nd)) for nd in w.handles))
        print(f"| LIVE {bpm} BPM: future-beat spread across CRTs (ms) | 4 | 5 | {stats(sp)} |")
        print(f"| LIVE {bpm} BPM: worst CRT beat vs. true music beat (ms) | 4 | 5 | {stats(err)} |")
    at = []
    for s in seeds:
        w = SimWorld(5, seed=400 + s)
        run_until(w, lambda: settled(w, "pi-01"), 200)
        w.run_for(30)
        w.start_music(128, ["pi-04"])  # audio interface appears on a follower
        at.append(run_until(w, lambda: settled(w, "pi-04"), 300))
    print(f"| audio Pi appears as follower -> it is master, all following (s) | 5 | {len(seeds)} | {stats(at)} |")
    od = []
    for s in range(10):
        w = SimWorld(3, seed=300 + s)
        run_until(w, lambda: settled(w, "pi-01"), 200)
        w.run_for(600)  # long baseline for skew estimation
        w.crash("pi-01")
        w.groups = [{"pi-02"}, {"pi-03"}]  # survivors cannot even hear each other
        w.run_for(120)
        od.append(w.beat_spread_ms(["pi-02", "pi-03"]))
    print(f"| 2 survivors isolated 120 s (no network at all): beat spread (ms) | 3 | 10 | {stats(od)} |")


if __name__ == "__main__":
    main()
