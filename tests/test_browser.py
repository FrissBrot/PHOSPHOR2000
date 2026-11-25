"""Headless Chromium tests of the real renderer (desktop x86; NOT Pi hardware).

* every scene renders without JS errors (demo mode)
* full stack: core + FastAPI + WebSocket + renderer, browser restart recovers
* frame-time benchmark with CPU throttling as a rough Pi 3 proxy (reported, see docs/hardware.md)
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest

pw = pytest.importorskip("playwright.sync_api")

OUT = Path(os.environ.get("CS2K_ARTIFACTS", "/tmp/cs2k-artifacts"))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    import uvicorn

    from backend.api.app import create_app
    from backend.config import load_config
    from backend.network.base import ExternalNetworkAdapter
    from backend.services.core import Core

    tmp = tmp_path_factory.mktemp("core")
    port = free_port()
    cfg = load_config(node_path=tmp / "n.yaml", secrets_path=tmp / "s.yaml",
                      overrides={"node": {"id": "pi-01"}, "network": {"discovery_window": 0.5, "backoff_jitter": 0},
                                 "server": {"port": port}})
    core = Core(cfg, adapter=ExternalNetworkAdapter(), state_dir=tmp, has_audio=False)
    app = create_app(core)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    loop = asyncio.new_event_loop()

    def run():
        asyncio.set_event_loop(loop)
        loop.create_task(core.run_ticks())
        loop.run_until_complete(srv.serve())

    th = threading.Thread(target=run, daemon=True)
    th.start()
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}", core
    srv.should_exit = True
    core.stop()
    th.join(timeout=5)


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        exe = os.environ.get("CS2K_CHROMIUM")
        if not exe:  # fall back to any cached Playwright chromium build
            import glob
            found = sorted(glob.glob(os.path.expanduser(
                "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell")))
            exe = found[-1] if found else None
        try:
            b = p.chromium.launch(executable_path=exe,
                                  args=["--autoplay-policy=no-user-gesture-required"])
        except Exception as exc:  # browsers not installed
            pytest.skip(f"chromium unavailable: {exc}")
        yield b
        b.close()


SCENES = ["boot", "dance", "spectrum", "grid", "rain", "countdown"]


@pytest.mark.parametrize("scene", SCENES + ["cal"])
def test_scene_renders_without_errors(server, browser, scene):
    base, _ = server
    page = browser.new_page(viewport={"width": 720, "height": 576})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: m.type == "error" and errors.append(m.text))
    q = "cal=single" if scene == "cal" else f"scene={scene}"
    page.goto(f"{base}/?demo=1&{q}")
    page.wait_for_timeout(2500)
    OUT.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(OUT / f"scene-{scene}.png"))
    frames = page.evaluate("window.__cs2k.frames")
    page.close()
    assert not errors, errors
    assert frames > 20
    # content check: canvas is not black
    assert (OUT / f"scene-{scene}.png").stat().st_size > 5000


def test_full_stack_and_browser_restart(server, browser):
    base, core = server
    deadline = time.time() + 20
    while time.time() < deadline and not core.node.is_leader():
        time.sleep(0.2)
    assert core.node.is_leader(), core.node.state
    ctx = browser.new_context(viewport={"width": 720, "height": 576})
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(base + "/")
    page.wait_for_function("window.__cs2k && window.__cs2k.state && window.__cs2k.clock.ready()", timeout=10000)
    page.wait_for_timeout(3000)
    beat1 = page.evaluate("window.__cs2k.timeline.beatAt(window.__cs2k.clock.clusterAt(performance.now()))")
    # backend beat at the same moment
    now_c = core.node.cluster_now(time.monotonic())
    beat_srv = core.node.timeline.beat_at(now_c)
    assert abs(beat1 - beat_srv) < 0.05, (beat1, beat_srv)  # < ~25 ms at 120 BPM incl. IPC jitter
    assert core.node.renderer.get("fps", 0) > 5
    page.screenshot(path=str(OUT / "fullstack.png"))
    # browser restart: new page in same profile renders from cache instantly, then resyncs
    page.close()
    page2 = ctx.new_page()
    page2.goto(base + "/")
    page2.wait_for_function("window.__cs2k && window.__cs2k.state", timeout=3000)
    seg1 = page2.evaluate("window.__cs2k.state.segments[0].seed")
    assert seg1 == core.node.render_state(time.monotonic())["segments"][0]["seed"]
    ctx.close()
    assert not errors, errors


def test_benchmark_scenes_throttled(server, browser):
    """Frame draw time with 6x CPU throttling (rough Pi 3 proxy). Results written to artifacts."""
    base, _ = server
    page = browser.new_context(viewport={"width": 720, "height": 576}, bypass_csp=True).new_page()
    cdp = page.context.new_cdp_session(page)
    cdp.send("Emulation.setCPUThrottlingRate", {"rate": 6})
    page.goto(f"{base}/?bench=1&secs=3")
    page.wait_for_function("window.__bench && window.__bench.done", timeout=90000, polling=500)
    res = page.evaluate("window.__bench.results")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "bench-throttled-6x.json").write_text(json.dumps(res, indent=2))
    page.close()
    for scene, r in res.items():
        assert r["draw_ms_p50"] < 40, (scene, r)  # 25 fps budget = 40 ms per frame
