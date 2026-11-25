"""FastAPI application: renderer WebSocket, admin API/WebSocket, static files."""
from __future__ import annotations

import asyncio
import json
import re
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.cluster.messages import seal

from backend.api.auth import COOKIE, CSRF_HEADER, RateLimiter, Sessions, hash_password, verify_password
from backend.api.models import COMMANDS, Login, NetModeBody, PasswordChange
from backend.cluster.node import CommandError
from backend.config import REPO_ROOT, SharedConfig

if TYPE_CHECKING:
    from backend.services.core import Core

log = logging.getLogger("phos.api")
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
    app = FastAPI(title="PHOSPHOR2000", docs_url=None, redoc_url=None, openapi_url=None)
    node = core.node
    # one login for the whole cluster: key from cluster key + current password version
    sessions = Sessions(core.cfg.secrets.session_secret,
                        key_material=node.admin_key_material if core.cfg.secrets.cluster_key else None)
    limiter = RateLimiter()

    def pw_hash() -> str:
        return node.admin.get("hash") or ""

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
        return {"ok": True, "node": node.id, "state": node.state.value, "leader": node.election.current_leader_id(),
                "version": node.version}

    @app.post("/api/login")
    async def login(body: Login, request: Request, response: Response):
        ip = request.client.host if request.client else "?"
        if not limiter.allow(ip) or not limiter.allow("*global*"):
            raise HTTPException(429, "too many attempts, wait a minute")
        if not pw_hash():
            raise HTTPException(503, "no admin password configured: run `sudo phosphor-passwd` on the Pi")
        ok = await asyncio.get_running_loop().run_in_executor(None, verify_password, body.password, pw_hash())
        if not ok:
            node._log("warning", f"failed admin login from {ip}")
            raise HTTPException(401, "wrong password")
        response.set_cookie(COOKIE, sessions.issue(), httponly=True, samesite="strict", max_age=12 * 3600, path="/")
        return {"ok": True}

    @app.post("/api/admin/password")
    async def change_password(body: PasswordChange, request: Request):
        """Cluster-wide password change: hashed here, replicated by the leader, all sessions end."""
        require(request, mutating=True)
        loop = asyncio.get_running_loop()
        if not await loop.run_in_executor(None, verify_password, body.current, pw_hash()):
            raise HTTPException(403, "current password is wrong")
        try:
            h = await loop.run_in_executor(None, hash_password, body.new)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        try:
            res = node.command("set_admin_hash", {"hash": h}, time.monotonic(), origin="admin")
        except CommandError as exc:
            raise HTTPException(409, str(exc))
        core.persist()
        return {"ok": True, **res}

    @app.post("/api/logout")
    async def logout(request: Request, response: Response):
        sessions.revoke(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/session")
    async def session(request: Request):
        return {"authenticated": authed(request), "password_configured": bool(pw_hash()), "node": node.id}

    # -------------------------------------------------------------- status
    def cluster_view(now: float) -> dict:
        own = {**node.status(now), "metrics": core.metrics, "ip": None, "self": True}
        nodes: dict[str, dict] = {node.id: own}
        for pid, p in node.election.peers.items():
            age = now - p.last_seen
            entry = {"id": pid, "priority": p.priority, "state": p.state, "leader": p.leader_id,
                     "beacon_age": round(age, 1), "ip": node.peer_ips.get(pid),
                     "has_audio": p.info.get("has_audio"), "audio_live": p.info.get("audio_live"),
                     "ver": p.info.get("ver"),
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
            "update": upd.status() if upd else None,
            "network": {**core.netmode.status(), "share_internet": node.shared.site.share_internet,
                        "pending": node.shared.site.net_mode, "wlan_ssid": core.cfg.network.wlan_ssid,
                        "internet": core.inet.ok} if hasattr(core, "netmode") else None,
            "packages": {"job": core.packages.job(), "rollout": core.packages.rollout} if hasattr(core, "packages") else None,
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
        core.persist()
        return res

    # ------------------------------------------------------------- updates
    upd = getattr(core, "updater", None)

    @app.get("/api/update/status")
    async def update_status(request: Request):
        require(request)
        return upd.status()

    @app.post("/api/update/check")
    async def update_check(request: Request):
        require(request, mutating=True)
        await asyncio.get_running_loop().run_in_executor(None, lambda: upd.check_github(blocking=True))
        return upd.status()

    def _upd(fn):
        try:
            return fn()
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(409, str(exc))

    @app.post("/api/update/self")
    async def update_self(request: Request):
        """Update THIS Pi from GitHub (needs internet on this Pi)."""
        require(request, mutating=True)
        _upd(upd.update_self_github)
        return {"ok": True, "message": "update started on " + node.id}

    @app.post("/api/update/rollout")
    async def update_rollout(request: Request):
        require(request, mutating=True)
        loop = asyncio.get_running_loop()
        r = await loop.run_in_executor(None, lambda: _upd(lambda: upd.start_rollout(time.monotonic())))
        return {"ok": True, "rollout": r}

    @app.post("/api/update/cancel")
    async def update_cancel(request: Request):
        require(request, mutating=True)
        upd.cancel_rollout()
        return {"ok": True}

    @app.post("/api/update/rollback/{target}")
    async def update_rollback(target: str, request: Request):
        require(request, mutating=True)
        if not re.fullmatch(r"pi-0[1-9]", target):
            raise HTTPException(422, "bad node id")
        _upd(lambda: upd.rollback_node(target))
        return {"ok": True}

    @app.get("/api/update/manifest")
    async def update_manifest(sig: str = ""):
        # other Pis only: authenticated with the cluster key
        if not upd.check_sig("manifest", sig):
            raise HTTPException(403, "forbidden")
        return await asyncio.get_running_loop().run_in_executor(None, upd.manifest)

    @app.get("/api/update/bundle")
    async def update_bundle(v: str = "", sig: str = ""):
        if not upd.check_sig("bundle|" + v, sig) or v != node.version:
            raise HTTPException(403 if not upd.check_sig("bundle|" + v, sig) else 404, "not available")
        path, sha = await asyncio.get_running_loop().run_in_executor(None, upd.bundle)
        return FileResponse(path, media_type="application/gzip", headers={"X-SHA256": sha})

    # ------------------------------------------------------------- network + packages
    @app.post("/api/network/mode")
    async def network_mode(body: NetModeBody, request: Request):
        """Switch ALL Pis to another network mode (applied after 15 s, automatic fallback after 3 min)."""
        require(request, mutating=True)
        if body.wlan_psk and not 8 <= len(body.wlan_psk) <= 63:
            raise HTTPException(422, "Wi-Fi password must be 8..63 characters")
        if not core.cfg.secrets.cluster_key and body.wlan_psk:
            raise HTTPException(409, "no cluster key configured: cannot distribute a Wi-Fi password securely")
        sealed = seal(core.cfg.secrets.cluster_key, body.wlan_psk) if body.wlan_psk else ""
        try:
            return node.command("set_network_mode", {"mode": body.mode, "wlan_ssid": body.wlan_ssid,
                                                     "wlan_psk_sealed": sealed}, time.monotonic(), origin="admin")
        except CommandError as exc:
            raise HTTPException(409, str(exc))

    @app.post("/api/packages/self")
    async def packages_self(request: Request):
        require(request, mutating=True)
        _upd(core.packages.run_local)
        return {"ok": True, "message": f"package update started on {node.id}"}

    @app.post("/api/packages/all")
    async def packages_all(request: Request, force: bool = False):
        require(request, mutating=True)
        return {"ok": True, "rollout": _upd(lambda: core.packages.start_all(time.monotonic(), force=force))}

    @app.post("/api/internet/check")
    async def internet_check(request: Request):
        require(request, mutating=True)
        core.inet.force()
        core.inet.tick(time.monotonic(), wanted=True)
        await asyncio.sleep(5)
        return {"internet": core.inet.ok}

    @app.get("/api/config/export")
    async def export_config(request: Request):
        require(request)
        data = {"format": "phosphor2000/shared-config", "version": 1, "shared_rev": node.shared_rev,
                "exported_by": node.id, "shared": node.shared.model_dump()}
        return JSONResponse(data, headers={"Content-Disposition": "attachment; filename=phosphor-config.json"})

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
