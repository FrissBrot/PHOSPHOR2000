"""Cluster software updates, controlled from the dashboard.

Model
-----
* Every Pi runs one *release* (``/opt/phosphor2000`` -> ``/opt/phosphor2000-releases/<version>``).
  Versions look like ``20261125T1207-85ac3f6`` (commit time UTC + commit) and sort as strings.
* A Pi with internet can update itself from GitHub (``update.sh --github``).
* Inside the (offline) cluster the newest release present on any Pi is rolled out to the others:
  the leader asks the source Pi for a signed manifest (version + sha256), then orders one Pi after
  the other (least important first, leader last) to download the bundle from the source over HTTP
  and verify the sha256 that arrived in the HMAC-authenticated cluster command.
* Each Pi installs with ``phosphor-update.service`` (root oneshot; polkit allows exactly that),
  health-checks itself and rolls back automatically.
* Optional automatic mode: GitHub check every few hours and cluster rollout, but only while no
  LIVE music has been detected for ``idle_minutes`` (never during the show by default).
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
import os
import re
import tarfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

from backend.config import REPO_ROOT

if TYPE_CHECKING:
    from backend.services.core import Core

log = logging.getLogger("phos.update")

VERSION_RE = re.compile(r"^\d{8}T\d{4}-([0-9a-f]{7,40}|local)(\+mod)?$")
EXCLUDE = {".git", ".venv", "venv", "__pycache__", "node_modules", ".pytest_cache"}
STEP_TIMEOUT = 420.0          # one Pi: download + install + restart + health check
RESEND = 20.0


def valid_version(v: str | None) -> bool:
    return bool(v and VERSION_RE.match(v))


class Updater:
    def __init__(self, core: "Core", run_dir: Path) -> None:
        self.core = core
        self.node = core.node
        self.run_dir = run_dir
        self.key = hashlib.sha256(b"phos-update|" + core.cfg.secrets.cluster_key.encode()).digest()
        self.github: dict = {}
        self.last_live = time.monotonic()
        self.next_auto = 0.0
        self.next_github = 0.0
        self._bundle: tuple[str, Path, str] | None = None     # (version, path, sha256)
        self._lock = threading.Lock()
        self.requests: list[dict] = []                       # sent to update.sh (tests inspect this)
        self.start_unit = None                               # injectable: callable() -> bool

    # ------------------------------------------------------------ basics
    @property
    def rollout(self) -> dict | None:
        """Replicated to all Pis with the leader state, so a new leader continues a rollout.
        All times inside are cluster times (comparable on every Pi)."""
        return self.node.extra.get("rollout")

    @rollout.setter
    def rollout(self, value: dict | None) -> None:
        self.node.extra["rollout"] = value

    @property
    def version(self) -> str:
        return self.node.version

    def sign(self, text: str) -> str:
        return hmac.new(self.key, text.encode(), hashlib.sha256).hexdigest()

    def check_sig(self, text: str, sig: str | None) -> bool:
        return bool(sig) and hmac.compare_digest(self.sign(text), sig or "")

    def job(self) -> dict:
        try:
            return json.loads((self.run_dir / "update-status.json").read_text())
        except (OSError, ValueError):
            return {}

    def busy(self) -> bool:
        j = self.job()
        return j.get("state") in ("downloading", "installing", "rolling_back", "rebooting") and \
            time.time() - j.get("at", 0) < 900

    def status(self) -> dict:
        return {"version": self.version, "github": self.github, "job": self.job(),
                "rollout": self.rollout, "newest": self.newest_in_cluster(),
                "idle": self.idle(time.monotonic()),
                "policy": self.node.shared.update.model_dump()}

    # ------------------------------------------------------------ bundle
    def bundle(self) -> tuple[Path, str]:
        """tar.gz of the running release (cached per version in RAM-backed /run)."""
        with self._lock:
            if self._bundle and self._bundle[0] == self.version and self._bundle[1].exists():
                return self._bundle[1], self._bundle[2]
            root = Path(REPO_ROOT).resolve()
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=6) as tar:
                for p in sorted(root.rglob("*")):
                    rel = p.relative_to(root)
                    if any(part in EXCLUDE for part in rel.parts) or p.is_symlink():
                        continue
                    if p.is_file():
                        tar.add(p, arcname=f"release/{rel.as_posix()}", recursive=False)
            data = buf.getvalue()
            self.run_dir.mkdir(parents=True, exist_ok=True)
            path = self.run_dir / f"bundle-{self.version}.tar.gz"
            path.write_bytes(data)
            sha = hashlib.sha256(data).hexdigest()
            self._bundle = (self.version, path, sha)
            return path, sha

    def manifest(self) -> dict:
        _, sha = self.bundle()
        return {"node": self.node.id, "version": self.version, "sha256": sha,
                "sig": self.sign(f"manifest|{self.version}|{sha}")}

    # ------------------------------------------------------------ requests to update.sh
    def _request(self, req: dict) -> bool:
        if self.busy():
            raise RuntimeError("an update is already running on this Pi")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.run_dir / "update-request.json.tmp"
        tmp.write_text(json.dumps(req))
        tmp.replace(self.run_dir / "update-request.json")
        self.requests.append(req)
        self.node._log("warning", f"software update requested: {req.get('source')} {req.get('version', '')}")
        if self.start_unit:
            return self.start_unit()
        from backend.services.system import request_update
        return request_update()

    def update_self_github(self) -> bool:
        return self._request({"source": "github"})

    def update_self_peer(self, url: str, sha: str, version: str) -> bool:
        if not valid_version(version) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise ValueError("invalid version/checksum")
        if version == self.version:
            return True
        return self._request({"source": "peer", "url": url, "sha256": sha, "version": version})

    def rollback_self(self) -> bool:
        return self._request({"source": "rollback"})

    # ------------------------------------------------------------ cluster messages
    def on_message(self, m: dict, local: float) -> None:
        """update_cmd / update_rollback from the current leader (HMAC already verified)."""
        if m.get("_from") != self.node.election.current_leader_id() or m.get("to") != self.node.id:
            return
        try:
            if m["t"] == "update_cmd":
                if self.busy() or m.get("version") == self.version:
                    return
                if not re.fullmatch(r"/api/update/bundle\?v=[0-9A-Za-z+T-]+&sig=[0-9a-f]{64}", m.get("path", "")):
                    raise ValueError("bad bundle path")
                self.update_self_peer(self.source_url(m["source"], m["path"]), m["sha256"], m["version"])
            elif m["t"] == "update_rollback":
                self.rollback_self()
        except (KeyError, ValueError, RuntimeError) as exc:
            self.node._log("error", f"update command rejected: {exc}")

    # ------------------------------------------------------------ GitHub
    def check_github(self, blocking: bool = False) -> None:
        def run() -> None:
            src = self.core.cfg.update_source
            url = f"https://api.github.com/repos/{src.repo}/commits/{src.branch}"
            try:
                req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                           "User-Agent": "phosphor2000-updater"})
                d = json.load(urllib.request.urlopen(req, timeout=15))
                sha = d["sha"]
                t = time.strptime(d["commit"]["committer"]["date"], "%Y-%m-%dT%H:%M:%SZ")
                ver = time.strftime("%Y%m%dT%H%M", t) + "-" + sha[:7]
                self.github = {"version": ver, "message": d["commit"]["message"].splitlines()[0][:120],
                               "checked_at": time.time(), "error": None,
                               "newer": ver > self.newest_in_cluster()}
            except Exception as exc:   # offline is the normal case at a venue
                self.github = {**self.github, "checked_at": time.time(),
                               "error": f"GitHub not reachable ({type(exc).__name__})"}
        if blocking:
            run()
        else:
            threading.Thread(target=run, daemon=True, name="github-check").start()

    # ------------------------------------------------------------ cluster view
    def versions(self, now: float) -> dict[str, str]:
        out = {self.node.id: self.version}
        for pid, p in self.node.election.peers.items():
            if now - p.last_seen < 10 and p.info.get("ver"):
                out[pid] = p.info["ver"]
        return out

    def newest_in_cluster(self) -> str:
        vs = [v for v in self.versions(time.monotonic()).values() if valid_version(v)]
        return max(vs) if vs else self.version

    def idle(self, now: float) -> bool:
        pol = self.node.shared.update
        return (not pol.only_when_idle) or now - self.last_live >= pol.idle_minutes * 60

    # ------------------------------------------------------------ rollout (leader)
    def start_rollout(self, now: float, manifest_fetch=None) -> dict:
        if not self.node.is_leader():
            raise RuntimeError("only the leader can roll out; open the dashboard at 192.168.50.1")
        if self.rollout and self.rollout.get("state") == "running":
            raise RuntimeError("a rollout is already running")
        vers = self.versions(now)
        target = self.newest_in_cluster()
        if all(v == target for v in vers.values()):
            raise RuntimeError(f"all Pis already run {target}")
        source = self.node.id if vers.get(self.node.id) == target else \
            sorted(k for k, v in vers.items() if v == target)[0]
        if source == self.node.id:
            man = self.manifest()
        else:
            src_ip = self.node.peer_ips.get(source)
            if not src_ip:
                raise RuntimeError(f"no address known for {source}")
            man = (manifest_fetch or self.fetch_manifest)(src_ip)
            if man.get("version") != target:
                raise RuntimeError("source manifest does not match")
        # every Pi resolves the source's address itself (it hears the source's datagrams)
        path = f"/api/update/bundle?v={target}&sig={self.sign('bundle|' + target)}"
        prios = {pid: p.priority for pid, p in self.node.election.peers.items()}
        queue = sorted([k for k, v in vers.items() if v != target and k != self.node.id],
                       key=lambda k: (prios.get(k, 0), k))
        self.rollout = {"state": "running", "target": target, "source": source, "path": path,
                        "sha256": man["sha256"], "queue": queue, "done": [], "current": None,
                        "current_since": None, "last_sent": 0.0, "leader_last": vers.get(self.node.id) != target,
                        "started": time.time(), "message": f"rolling out {target} from {source}"}
        self.node._log("warning", f"software rollout {target} from {source}: order {queue + ([self.node.id] if self.rollout['leader_last'] else [])}")
        return self.rollout

    def source_url(self, source: str, path: str) -> str:
        ip = "127.0.0.1" if source == self.node.id else self.node.peer_ips.get(source)
        if not ip:
            raise ValueError(f"no address known for update source {source}")
        return f"http://{ip}:{self.core.cfg.server.port}{path}"

    def fetch_manifest(self, ip: str) -> dict:
        url = f"http://{ip}:{self.core.cfg.server.port}/api/update/manifest?sig={self.sign('manifest')}"
        d = json.load(urllib.request.urlopen(url, timeout=10))
        if not self.check_sig(f"manifest|{d.get('version')}|{d.get('sha256')}", d.get("sig")):
            raise RuntimeError("manifest signature invalid")
        return d

    def cancel_rollout(self) -> None:
        if self.rollout and self.rollout["state"] == "running":
            self.rollout.update(state="cancelled", message="cancelled by admin")

    def _rollout_tick(self, now: float) -> None:
        r = self.rollout
        if not r or r["state"] != "running":
            return
        if not self.node.is_leader():
            r.update(state="failed", message="lost leadership during rollout")
            return
        vers = self.versions(now)
        c = self.node.cluster_now(now)
        cur = r["current"]
        if cur is not None:
            peer = self.node.election.peers.get(cur)
            if vers.get(cur) == r["target"] and peer and peer.state in ("FOLLOWER", "RECOVERING"):
                r["done"].append(cur)
                r["current"] = None
                r["message"] = f"{cur} updated"
                cur = None
            elif c - r["current_since"] > STEP_TIMEOUT:
                r.update(state="failed", message=f"{cur} did not come back with {r['target']} within "
                                                 f"{STEP_TIMEOUT / 60:.0f} min; rollout stopped")
                self.node._log("error", r["message"])
                return
            elif c - r["last_sent"] > RESEND:
                self._send_cmd(cur, now)
                return
            else:
                return
        if r["queue"]:
            nxt = r["queue"].pop(0)
            r.update(current=nxt, current_since=c, message=f"updating {nxt}")
            self._send_cmd(nxt, now)
            return
        if r["leader_last"]:
            r.update(state="done", message=f"followers done; updating leader {self.node.id} last")
            try:
                self.update_self_peer(self.source_url(r["source"], r["path"]), r["sha256"], r["target"])
            except Exception as exc:
                r.update(state="failed", message=f"leader update failed: {exc}")
            return
        r.update(state="done", message=f"all Pis run {r['target']}")
        self.node._log("info", r["message"])

    def _send_cmd(self, nid: str, now: float) -> None:
        r = self.rollout
        assert r is not None
        r["last_sent"] = self.node.cluster_now(now)
        self.node.send({"t": "update_cmd", "to": nid, "source": r["source"], "path": r["path"],
                        "sha256": r["sha256"], "version": r["target"]}, nid)

    def rollback_node(self, nid: str) -> None:
        if nid == self.node.id:
            self.rollback_self()
        else:
            if not self.node.is_leader():
                raise RuntimeError("only the leader can roll back other Pis")
            self.node.send({"t": "update_rollback", "to": nid}, nid)

    # ------------------------------------------------------------ periodic
    def tick(self, now: float) -> None:
        from backend.sync.timeline import TimingSource
        if self.node.timeline.source == TimingSource.LIVE:
            self.last_live = now
        self._rollout_tick(now)
        pol = self.node.shared.update
        if not pol.auto or not self.node.is_leader() or now < self.next_auto:
            return
        self.next_auto = now + 60.0
        if not self.idle(now) or self.busy() or (self.rollout and self.rollout["state"] == "running"):
            return
        if now >= self.next_github:
            self.next_github = now + pol.github_check_hours * 3600
            self.check_github()
        gh = self.github.get("version")
        if valid_version(gh) and gh > self.newest_in_cluster() and not self.github.get("error"):
            try:
                self.update_self_github()          # the leader fetches, then rolls out after restart
            except RuntimeError:
                pass
            return
        vers = self.versions(now)
        if len(set(vers.values())) > 1:
            try:
                self.start_rollout(now)
            except Exception as exc:
                self.node._log("warning", f"automatic rollout not started: {exc}")
