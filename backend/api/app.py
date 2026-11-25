"""FastAPI application: renderer WebSocket, admin API/WebSocket, static files."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from backend.api.auth import COOKIE, CSRF_HEADER, RateLimiter, Sessions, verify_password
from backend.api.models import COMMANDS, Login
from backend.cluster.node import CommandError
from backend.config import REPO_ROOT, SharedConfig

if TYPE_CHECKING:
    from backend.services.core import Core

log = logging.getLogger("cs2k.api")
FRONTEND = REPO_ROOT / "frontend"
ADMIN = REPO_ROOT / "admin"
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; connect-src 'self' ws: wss:; img-src 'self' data:; "
                               "style-src 'self'; script-src 'self'; object-src 'none'; frame-ancestors 'none'",
    "Cache-Control": "no-store",
}


def create_app(core: "Core") -> FastAPI:
    app = FastAPI(title="CYBERSPACE 2000", docs_url=None, redoc_url=None, openapi_url=None)
    sessions = Sessions(core.cfg.secrets.session_secret)
    limiter = RateLimiter()
    pw_hash = core.cfg.secrets.admin_password_hash
    node = core.node

    @app.middleware("http")
    async def headers(request: Request, call_next):
        resp = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            resp.headers.setdefault(k, v)
        return resp

    def authed(request: Request) -> bool:
        return sessions.verify(request.cookies.get(COOKIE)) is not None

    def require(request: Request, mutating: bool = False) -> None:
        if not authed(request):
            raise HTTPException(401, "login required")
        if mutating and request.headers.get(CSRF_HEADER) != "1":
            raise HTTPException(403, "missing CSRF header")

    # ------------------------------------------------------------- static
    @app.get("/")
    async def index():
        return FileResponse(FRONTEND / "index.html")

    @app.get("/admin")
    async def admin_redirect():
        return RedirectResponse("/admin/")

    app.mount("/static", StaticFiles(directory=FRONTEND), name="frontend")
    app.mount("/admin/", StaticFiles(directory=ADMIN, html=True), name="admin")

    # --------------------------------------------------------------- auth
    @app.get("/api/health")
    async def health():
        return {"ok": True, "node": node.id, "state": node.state.value, "leader": node.election.current_leader_id()}

    @app.post("/api/login")
    async def login(body: Login, request: Request, response: Response):
        ip = request.client.host if request.client else "?"
        if not limiter.allow(ip) or not limiter.allow("*global*"):
            raise HTTPException(429, "too many attempts, wait a minute")
        if not pw_hash:
            raise HTTPException(503, "no admin password configured: run `sudo cyberspace-passwd` on the Pi")
        ok = await asyncio.get_running_loop().run_in_executor(None, verify_password, body.password, pw_hash)
        if not ok:
            node._log("warning", f"failed admin login from {ip}")
            raise HTTPException(401, "wrong password")
        response.set_cookie(COOKIE, sessions.issue(), httponly=True, samesite="strict", max_age=12 * 3600, path="/")
        return {"ok": True}

    @app.post("/api/logout")
    async def logout(request: Request, response: Response):
        sessions.revoke(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/session")
    async def session(request: Request):
        return {"authenticated": authed(request), "password_configured": bool(pw_hash), "node": node.id}

    # -------------------------------------------------------------- status
    def cluster_view(now: float) -> dict:
        own = {**node.status(now), "metrics": core.metrics, "ip": None, "self": True}
        nodes: dict[str, dict] = {node.id: own}
        for pid, p in node.election.peers.items():
            age = now - p.last_seen
            entry = {"id": pid, "priority": p.priority, "state": p.state, "leader": p.leader_id,
                     "beacon_age": round(age, 1), "ip": node.peer_ips.get(pid),
                     "has_audio": p.info.get("has_audio"), "audio_live": p.info.get("audio_live"),
                     "online": age < 10}
            st = node.peer_status.get(pid)
            if st and now - st["_rx"] < 10:
                entry.update({k: v for k, v in st.items() if not k.startswith("_")})
                entry["status_age"] = round(now - st["_rx"], 1)
            nodes[pid] = entry
        return nodes

    def status_payload() -> dict:
        now = time.monotonic()
        now_c = node.cluster_now(now)
        el = node.election
        return {
            "t": "status",
            "node": node.id, "state": node.state.value, "is_leader": node.is_leader(),
            "leader": el.current_leader_id(), "epoch": node.timeline.epoch,
            "wall": now_c + node.wall_offset,
            "timeline": {**node.timeline.to_dict(), "beat_now": node.timeline.beat_at(now_c),
                         "manual": node.manual, "audio_src": node.audio_src},
            "nodes": cluster_view(now),
            "shared": node.shared.model_dump(), "shared_rev": node.shared_rev,
            "schedule": {"revision": node.schedule.revision,
                         "overrides": [o.to_dict() for o in node.schedule.overrides]},
            "cal": {**node.cal.to_dict(), "tap_times": None},
            "transitions": [{**t.__dict__, "wall": time.time() - (now - t.at)} for t in el.transitions[-30:]],
            "safe_mode": not node.cfg.network.manage,
        }

    @app.get("/api/status")
    async def status(request: Request):
        require(request)
        return status_payload()

    @app.get("/api/logs")
    async def logs(request: Request, n: int = 200):
        require(request)
        return {"events": core.events.recent(max(1, min(n, 2000))),
                "calibrations": core.events.calibrations(50)}

    # ------------------------------------------------------------ commands
    @app.post("/api/cmd/{name}")
    async def cmd(name: str, request: Request):
        require(request, mutating=True)
        model = COMMANDS.get(name)
        if model is None:
            raise HTTPException(404, "unknown command")
        try:
            raw = await request.json() if (await request.body()) else {}
            args = model.model_validate(raw).model_dump(exclude_none=True)
        except (ValidationError, ValueError) as exc:
            raise HTTPException(422, str(exc)[:500])
        try:
            res = node.command(name, args, time.monotonic(), origin="admin")
        except CommandError as exc:
            raise HTTPException(409, str(exc))
        if name == "cal_save" and node.is_leader():
            for nid, val in (res.get("saved") or {}).items():
                core.events.add_calibration(time.time(), nid, val, node.cal.result or {})
        core.persist()
        return res

    @app.get("/api/config/export")
    async def export_config(request: Request):
        require(request)
        data = {"format": "cyberspace-2000/shared-config", "version": 1, "shared_rev": node.shared_rev,
                "exported_by": node.id, "shared": node.shared.model_dump()}
        return JSONResponse(data, headers={"Content-Disposition": "attachment; filename=cyberspace-config.json"})

    @app.post("/api/config/import")
    async def import_config(request: Request):
        require(request, mutating=True)
        body = await request.body()
        if len(body) > 256 * 1024:
            raise HTTPException(413, "too large")
        try:
            data = json.loads(body)
            shared = SharedConfig.model_validate(data.get("shared", data))
        except (ValueError, ValidationError) as exc:
            raise HTTPException(422, str(exc)[:500])
        try:
            return node.command("import_shared", {"shared": shared.model_dump()}, time.monotonic(), origin="admin")
        except CommandError as exc:
            raise HTTPException(409, str(exc))

    # ---------------------------------------------------------- websockets
    @app.websocket("/ws/admin")
    async def ws_admin(ws: WebSocket):
        origin = ws.headers.get("origin")
        host = ws.headers.get("host")
        if sessions.verify(ws.cookies.get(COOKIE)) is None or (origin and host and origin.split("//")[-1] != host):
            await ws.close(code=4401)
            return
        await ws.accept()
        try:
            while True:
                await ws.send_text(json.dumps(status_payload(), default=str))
                try:
                    msg = await asyncio.wait_for(ws.receive_text(), timeout=0.5)
                    m = json.loads(msg)
                    if m.get("t") == "sync":  # admin clock sync for browser taps / click audio
                        await ws.send_text(json.dumps({"t": "sync", "c": m.get("c"),
                                                       "s": node.cluster_now(time.monotonic())}))
                except asyncio.TimeoutError:
                    pass
        except (WebSocketDisconnect, RuntimeError):
            return

    @app.websocket("/ws/render")
    async def ws_render(ws: WebSocket):
        client = ws.client.host if ws.client else ""
        if core.cfg.server.render_local_only and client not in LOCAL_HOSTS \
                and sessions.verify(ws.cookies.get(COOKIE)) is None:
            await ws.close(code=4403)
            return
        await ws.accept()
        last_key = None
        last_full = 0.0
        last_feat = None

        async def reader():
            while True:
                m = json.loads(await ws.receive_text())
                t = m.get("t")
                if t == "sync":
                    await ws.send_text(json.dumps({"t": "sync", "c": m.get("c"),
                                                   "s": node.cluster_now(time.monotonic())}))
                elif t == "report":
                    node.command("renderer_report", {k: m[k] for k in m if k != "t"}, time.monotonic())

        rtask = asyncio.create_task(reader())
        try:
            while not rtask.done():
                now = time.monotonic()
                key = (node.timeline.revision, node.timeline.epoch, node.schedule.revision, node.shared_rev,
                       node.cal.rev, node.sync.step_epoch, node.state.value, node.manual)
                if key != last_key or now - last_full > 1.0:
                    st = node.render_state(now)
                    st["kbd_hud"] = now < core.kbd_active_until
                    await ws.send_text(json.dumps(st))
                    last_key, last_full = key, now
                f = node.features
                if f is not None and f is not last_feat and node.features_rx and now - node.features_rx < 0.6:
                    last_feat = f
                    await ws.send_text(json.dumps({"t": "features", **f}))
                await asyncio.sleep(0.04)
            rtask.result()
        except (WebSocketDisconnect, RuntimeError, ValueError):
            pass
        finally:
            rtask.cancel()

    return app
