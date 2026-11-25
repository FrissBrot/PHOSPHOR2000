"""Software update: update.sh in a fake root (test mode), signed bundles, rollout orchestration."""
import hashlib
import http.server
import io
import json
import os
import shutil
import subprocess
import tarfile
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.auth import hash_password
from backend.config import REPO_ROOT, load_config
from backend.network.base import ExternalNetworkAdapter
from backend.services.core import Core
from backend.update import Updater, valid_version

ROOT = Path(REPO_ROOT)
V_OLD, V_NEW = "20261101T1000-aaaaaaa", "20261125T1200-bbbbbbb"


def make_bundle(path: Path, broken: bool = False) -> str:
    """A minimal release tarball (top folder 'release/') like Updater.bundle() produces."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in ("backend/services/core.py", "requirements-pi.txt", "scripts/lib.sh", "scripts/update.sh"):
            if broken and rel.startswith("backend"):
                continue
            tar.add(ROOT / rel, arcname=f"release/{rel}")
    path.write_bytes(buf.getvalue())
    return hashlib.sha256(buf.getvalue()).hexdigest()


class Health(http.server.BaseHTTPRequestHandler):
    version = V_OLD

    def do_GET(self):  # noqa: N802
        body = json.dumps({"ok": True, "version": Health.version}).encode()
        self.send_response(200); self.end_headers(); self.wfile.write(body)

    def log_message(self, *a):
        pass


class Files(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


@pytest.fixture()
def fake(tmp_path):
    rel_dir = tmp_path / "releases"
    old = rel_dir / V_OLD
    (old / "scripts").mkdir(parents=True)
    (old / "VERSION").write_text(V_OLD + "\n")
    shutil.copy(ROOT / "requirements-pi.txt", old / "requirements-pi.txt")
    link = tmp_path / "phosphor2000"
    link.symlink_to(old)
    venv = tmp_path / "venv"
    venv.mkdir()
    (venv / ".requirements.sha256").write_text(hashlib.sha256((ROOT / "requirements-pi.txt").read_bytes()).hexdigest())
    run = tmp_path / "run"
    health = http.server.HTTPServer(("127.0.0.1", 0), Health)
    threading.Thread(target=health.serve_forever, daemon=True).start()
    www = tmp_path / "www"
    (www / "api" / "update").mkdir(parents=True)
    files = http.server.HTTPServer(("127.0.0.1", 0), lambda *a, **k: Files(*a, directory=str(www), **k))
    threading.Thread(target=files.serve_forever, daemon=True).start()
    env = dict(os.environ, PHOS_UPDATE_TEST="1", PHOS_PREFIX=str(link), PHOS_RELEASES=str(rel_dir),
               PHOS_VENV=str(venv), PHOS_RUN=str(run), PHOS_HEALTH_TRIES="3", PHOS_HEALTH_SLEEP="0.1",
               PHOS_HEALTH_URL=f"http://127.0.0.1:{health.server_port}/", PHOS_BACKUP_DIR=str(tmp_path / "bk"))
    yield {"tmp": tmp_path, "link": link, "env": env, "www": www, "port": files.server_port, "run": run}
    health.shutdown(); files.shutdown()


def run_update(fake, *args):
    p = subprocess.run(["bash", str(ROOT / "scripts" / "update.sh"), *args], env=fake["env"],
                       capture_output=True, text=True, timeout=60)
    st = json.loads((fake["run"] / "update-status.json").read_text())
    return p, st


def peer_args(fake, sha):
    url = f"http://127.0.0.1:{fake['port']}/api/update/bundle?v={V_NEW}&sig={'0' * 64}"
    return ["--peer", url, "--sha256", sha, "--version", V_NEW]


def test_peer_update_installs_and_switches(fake):
    sha = make_bundle(fake["www"] / "api" / "update" / "bundle")
    Health.version = V_NEW
    p, st = run_update(fake, *peer_args(fake, sha))
    assert p.returncode == 0, p.stdout + p.stderr
    assert st["state"] == "done" and st["version"] == V_NEW
    assert fake["link"].resolve().name == V_NEW
    assert (fake["link"] / "VERSION").read_text().strip() == V_NEW
    assert (fake["link"] / "venv").is_symlink()
    assert (fake["tmp"] / "releases" / "PREVIOUS").read_text().strip().endswith(V_OLD)


def test_checksum_mismatch_is_rejected(fake):
    make_bundle(fake["www"] / "api" / "update" / "bundle")
    p, st = run_update(fake, *peer_args(fake, "f" * 64))
    assert p.returncode != 0 and st["state"] == "error" and "checksum" in st["message"]
    assert fake["link"].resolve().name == V_OLD


def test_not_a_release_is_rejected(fake):
    sha = make_bundle(fake["www"] / "api" / "update" / "bundle", broken=True)
    p, st = run_update(fake, *peer_args(fake, sha))
    assert st["state"] == "error" and fake["link"].resolve().name == V_OLD


def test_unhealthy_update_rolls_back_automatically(fake):
    sha = make_bundle(fake["www"] / "api" / "update" / "bundle")
    Health.version = V_OLD          # new version never reports healthy
    p, st = run_update(fake, *peer_args(fake, sha))
    assert st["state"] == "error" and "rolled back" in st["message"]
    assert fake["link"].resolve().name == V_OLD


def test_manual_rollback(fake):
    sha = make_bundle(fake["www"] / "api" / "update" / "bundle")
    Health.version = V_NEW
    run_update(fake, *peer_args(fake, sha))
    Health.version = V_OLD
    p, st = run_update(fake, "--rollback")
    assert st["state"] == "done" and fake["link"].resolve().name == V_OLD


def test_request_file_validation(fake, tmp_path):
    req = tmp_path / "req.json"
    req.write_text(json.dumps({"source": "peer", "url": "http://evil.example/x.tgz", "sha256": "a" * 64, "version": V_NEW}))
    p, st = run_update(fake, "--request", str(req))
    assert st["state"] == "error" and "invalid update request" in st["message"]
    req.write_text(json.dumps({"source": "shell; rm -rf /"}))
    p, st = run_update(fake, "--request", str(req))
    assert st["state"] == "error"


# ------------------------------------------------------------------ orchestration

@pytest.fixture()
def core(tmp_path):
    cfg = load_config(node_path=tmp_path / "n.yaml", secrets_path=tmp_path / "s.yaml",
                      overrides={"node": {"id": "pi-01"},
                                 "network": {"discovery_window": 0.2, "backoff_jitter": 0, "backoff_slot": 0.2},
                                 "secrets": {"cluster_key": "k" * 32, "admin_password_hash": hash_password("pw123456")}})
    c = Core(cfg, adapter=ExternalNetworkAdapter(), state_dir=tmp_path, has_audio=False)
    c.updater.run_dir = tmp_path / "run"
    c.node.version = V_OLD
    deadline = time.time() + 5
    while not c.node.is_leader() and time.time() < deadline:
        c.node.tick(time.monotonic()); time.sleep(0.05)
    return c


def add_peer(core, nid, prio, ver, state="FOLLOWER", ip=None):
    now = time.monotonic()
    core.node.election._on_beacon({"id": nid, "prio": prio, "state": state, "leader_id": "pi-01", "ver": ver}, now)
    core.node.peer_ips[nid] = ip or f"192.168.50.{int(nid[-1]) + 10}"


def test_version_format():
    assert valid_version(V_NEW) and valid_version("20261125T1200-bbbbbbb+mod")
    assert not valid_version("unknown") and not valid_version("1.0")


def test_rollout_order_source_and_leader_last(core):
    sent = []
    core.node.send = lambda m, to: sent.append((m, to))
    up = core.updater
    up.start_unit = lambda: True
    add_peer(core, "pi-02", 90, V_OLD)
    add_peer(core, "pi-03", 80, V_NEW)           # got the new version from GitHub
    add_peer(core, "pi-04", 70, V_OLD)
    man = {"version": V_NEW, "sha256": "c" * 64}
    r = up.start_rollout(time.monotonic(), manifest_fetch=lambda ip: man)
    assert r["source"] == "pi-03" and r["queue"] == ["pi-04", "pi-02"] and r["leader_last"]
    now = time.monotonic()
    up._rollout_tick(now)
    cmd, to = sent[-1]
    assert to == "pi-04" and cmd["t"] == "update_cmd" and cmd["source"] == "pi-03" and cmd["sha256"] == "c" * 64
    assert up.check_sig("bundle|" + V_NEW, cmd["path"].split("sig=")[1])
    add_peer(core, "pi-04", 70, V_NEW)           # pi-04 came back updated
    up._rollout_tick(now + 1)
    assert sent[-1][1] == "pi-02"
    add_peer(core, "pi-02", 90, V_NEW)
    up._rollout_tick(now + 2)                    # followers done -> leader updates itself last
    assert r["state"] == "done" and up.requests[-1]["source"] == "peer"
    assert up.requests[-1]["url"].startswith("http://192.168.50.13:")   # pi-03's address


def test_rollout_stops_when_a_pi_does_not_come_back(core):
    core.node.send = lambda m, to: None
    up = core.updater
    add_peer(core, "pi-02", 90, V_OLD)
    core.node.version = V_NEW
    up._bundle = None
    r = up.start_rollout(time.monotonic(), manifest_fetch=None)
    now = time.monotonic()
    up._rollout_tick(now)
    add_peer(core, "pi-02", 90, V_OLD)
    up._rollout_tick(now + 500)
    assert r["state"] == "failed" and "pi-02" in r["message"]


def test_follower_accepts_update_only_from_leader(core):
    up = core.updater
    up.start_unit = lambda: True
    core.node.election.state = core.node.election.state.__class__.FOLLOWER
    from backend.cluster.election import LeaderClaim
    core.node.election.leader = LeaderClaim(1, 100, "pi-05")
    core.node.peer_ips["pi-05"] = "192.168.50.1"
    path = f"/api/update/bundle?v={V_NEW}&sig={'a' * 64}"
    msg = {"t": "update_cmd", "to": "pi-01", "source": "pi-05", "path": path, "sha256": "d" * 64, "version": V_NEW}
    up.on_message({**msg, "_from": "pi-04"}, 0)               # not the leader -> ignored
    assert up.requests == []
    up.on_message({**msg, "_from": "pi-05"}, 0)
    assert up.requests[-1]["url"] == f"http://192.168.50.1:8080{path}"
    up.on_message({**msg, "_from": "pi-05", "path": "/etc/passwd"}, 0)   # rejected path
    assert len(up.requests) == 1


def test_bundle_and_manifest_endpoints_need_cluster_signature(core):
    client = TestClient(create_app(core))
    up = core.updater
    assert client.get("/api/update/manifest?sig=bad").status_code == 403
    man = client.get(f"/api/update/manifest?sig={up.sign('manifest')}").json()
    assert man["version"] == V_OLD and up.check_sig(f"manifest|{V_OLD}|{man['sha256']}", man["sig"])
    assert client.get(f"/api/update/bundle?v={V_OLD}&sig=x").status_code == 403
    r = client.get(f"/api/update/bundle?v={V_OLD}&sig={up.sign('bundle|' + V_OLD)}")
    assert r.status_code == 200 and hashlib.sha256(r.content).hexdigest() == man["sha256"]
    with tarfile.open(fileobj=io.BytesIO(r.content)) as t:
        names = t.getnames()
    assert "release/backend/services/core.py" in names
    assert not any("/.git/" in n or "/.venv/" in n or "__pycache__" in n for n in names)


def test_update_endpoints_require_login(core):
    client = TestClient(create_app(core))
    assert client.post("/api/update/rollout", headers={"X-PHOS": "1"}).status_code == 401
    client.post("/api/login", json={"password": "pw123456"})
    assert client.post("/api/update/rollout").status_code == 403         # CSRF header
    r = client.post("/api/update/rollout", headers={"X-PHOS": "1"})
    assert r.status_code == 409 and "already run" in r.json()["detail"]   # nothing to roll out
    st = client.get("/api/update/status").json()
    assert st["version"] == V_OLD


def test_auto_update_waits_for_music_pause(core):
    from backend.sync.timeline import TimingSource
    from dataclasses import replace
    up = core.updater
    core.node.shared = core.node.shared.model_copy(update={"update": core.node.shared.update.model_copy(
        update={"auto": True, "idle_minutes": 10})})
    up.next_github = float("inf")
    started = []
    up.start_rollout = lambda now, manifest_fetch=None: started.append(now)
    add_peer(core, "pi-02", 90, V_NEW)
    core.node.timeline = replace(core.node.timeline, source=TimingSource.LIVE)
    now = time.monotonic()
    up.tick(now)
    assert started == []                                   # music is playing
    core.node.timeline = replace(core.node.timeline, source=TimingSource.PREDICTED)
    up.next_auto = 0
    up.tick(now + 300)
    assert started == []                                   # only 5 min without music
    up.next_auto = 0
    core.node.election.peers["pi-02"].last_seen = now + 700   # peer still beaconing
    up.tick(now + 700)
    assert started                                         # > 10 min pause -> rollout
