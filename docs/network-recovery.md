# Network, failover and split brain

## Physical reality (read first)

* A Pi 3 has **one Wi-Fi radio**. It is either an access point *or* a station. We never attempt
  both. When the hotspot owner dies, **every follower loses the network** until a replacement
  AP is up and everybody has re-associated. In simulation this takes ~24–26 s (see
  test-report.md); on hardware expect 20–45 s. Rendering never stops during that time.
* While disconnected, nodes **cannot vote**. This is *not* a consensus system. Leadership is
  decided from cached roster knowledge + scans with staggered backoff, and conflicts are
  reconciled deterministically once the nodes can see each other again.
* 2.4 GHz channel 6 (`band: bg`) is the default so Pi 3 B (2.4 GHz only) and B+ can be mixed.

## Addressing

Every leader runs the AP `PHOSPHOR2000` (WPA2-PSK, CCMP) with **gateway 192.168.50.1/24**
and DHCP (NetworkManager `ipv4.method=shared`, dnsmasq). The admin URL is therefore always
`http://192.168.50.1:8080`, whoever leads. Followers get DHCP addresses; nodes are identified by
their persistent id (`pi-01` … `pi-05`), never by IP.

## States

| state | meaning | leaves to |
|---|---|---|
| `BOOTING` | process start, cache restored | DISCOVERING |
| `DISCOVERING` | scanning for `PHOSPHOR2000` (10 s after boot, 4 s after a failure) | RECONNECTING (found) / ELECTION_BACKOFF |
| `ELECTION_BACKOFF` | no AP found; wait `rank × 8 s + jitter`, keep scanning | RECONNECTING (someone else won) / STARTING_HOTSPOT |
| `STARTING_HOTSPOT` | `nmcli connection up phosphor-ap` | LEADER / DISCOVERING (failed) / DEGRADED (3 failures) |
| `LEADER` | owns the AP, authority for replicated state | RECONNECTING (yield/handover) / STARTING_HOTSPOT (link lost) |
| `RECONNECTING` | associating as station (optionally pinned to a BSSID) | RECOVERING (leader beacon heard) / DISCOVERING |
| `RECOVERING` | comparing revisions, adopting leader state (slew/defer) | FOLLOWER |
| `FOLLOWER` | synced to leader | SUSPECTED_FAILURE |
| `SUSPECTED_FAILURE` | no leader beacon for 3.5 s (or link lost) | FOLLOWER (beacons resume) / DISCOVERING (8 s) |
| `DEGRADED` | radio unusable / repeated failures; render standalone | DISCOVERING every 30 s |

Every transition is logged with a reason (journal, SQLite event log, admin dashboard).

**Rank** = number of nodes with higher priority that are believed alive (roster from beacons in
the last 90 s, excluding the failed leader; the configured priority table on a fresh boot). With
default priorities pi-02 takes over from pi-01 after ~8 s of failure detection + scan; pi-03
would wait one more 8 s slot and normally finds pi-02's AP first.

## Leader claims and reconciliation

A leader's claim is `(epoch, priority, node_id)`. Epoch = highest epoch ever seen + 1, persisted
in the cache, so a node never reuses an epoch; equal numbers chosen in separate partitions are
disambiguated by priority and id, giving a **total order**:

> lower epoch wins → higher priority wins → lower node id wins

"Lower epoch wins" implements *do not preempt a healthy leader*: the longer-established leader
always survives a merge; a returning former leader (e.g. pi-01 after a reboot) first scans, finds
the existing AP and joins as follower.

### Detecting competing hotspots

Two APs with the same SSID cannot see each other's beacons on IP level. Detection therefore uses:

1. **Follower scans** every ~45–60 s: a BSSID with our SSID that is not our leader is reported
   (`foreign_ap`) to the leader.
2. **Scouting**: the leader sends its *least important* follower to the foreign BSSID. The scout
   connects, hears the foreign leader's claim, announces its home leader's claim there
   (`leader_claim`), returns home and announces the foreign claim. Both leaders now know both
   claims; the loser **yields**: it broadcasts `yield {winner_bssid}` three times, waits 1.5 s,
   stops its AP and reconnects to the winner; its followers reconnect pinned to the winner BSSID.
3. **Lone leaders** (no followers for > 20 s) briefly give up their AP every ~90–110 s to rescan,
   because nobody else could ever report them.
4. On an externally managed network (`network.manage: false`), competing leaders hear each
   other's beacons directly and the loser yields immediately.

### Anti-oscillation

* Total order → the same pair always resolves the same way.
* A node that yielded gets a hold-down before it may start an AP again: 30 s, doubling per
  repeated yield, max 10 min (protects against asymmetric range where the loser cannot reach the
  winner and would otherwise flap).
* Followers never start hotspots while they hear a leader; backoff increases while a controlled
  handover is expected.
* Simulation: 300 s after a merge, zero further state transitions (`test_no_oscillation_after_merge`).

### Split-brain limits (honest list)

* During a partition there **are** two leaders with diverging timelines/configs. Each side stays
  internally consistent. After the merge the winner's timeline is adopted (small phase errors
  slewed, large ones applied on the next bar downbeat) and the winner's configuration wins.
* Configuration edits made in the losing partition are **lost**, except calibration offsets,
  which are merged per CRT by last-writer-wins on wall-clock version stamps.
* Detection depends on someone scanning: worst case is roughly one scan interval plus scout
  time (sim: 16–128 s).
* A "zombie" AP (radio up, core dead) is avoided for 60 s after an association without leader
  beacons, but it can still attract stations; the core's systemd restart normally fixes it in seconds.
* Wall-clock version stamps rely on roughly correct clocks; without an RTC set the cluster clock
  from the admin dashboard after a cold start.

## Controlled failover

Admin → Cluster → *make leader* on a healthy follower. The leader announces `handover`, waits
1.5 s, stops its AP; the target starts its AP immediately; everybody else waits for it. The network
still drops for ~10–25 s (radio switch + re-association); rendering continues.

## Recovery plans (when networking goes wrong)

1. **Ethernet is never touched.** Plug a cable in and SSH to the Pi.
2. **Safe mode**: create an empty file `phosphor-safe-mode` on the boot partition (readable
   from any PC: `/boot/firmware/` on the Pi). The core will not touch the radio; the node renders
   standalone or follows over an externally configured network.
3. `sudo /opt/phosphor2000/scripts/configure-network.sh --rollback` restores the
   NetworkManager connections and config exactly as before the first install.
4. `network.manage: false` in `/etc/phosphor2000/node.yaml` permanently switches to
   external-network mode (venue router or Ethernet switch, all nodes on one L2 network).
