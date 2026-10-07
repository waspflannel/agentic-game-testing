"""Loopback dashboard and local controller API."""
from __future__ import annotations
import asyncio
import anyio
import httpx
from contextlib import AsyncExitStack, asynccontextmanager
import json
import os
from pathlib import Path
import time
from urllib.parse import urlparse
import psutil
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles
from .ai import OpenAI, ProviderError, SUBSCRIPTION_PRICING
from .codex_backend import CodexError
from .coordinator import Coordinator
from .credentials import Credentials
from .models import Plan, MODEL, SUPPORTED_MODELS, AI_API, ACTIVE_STATES, RecalculateGoalsRequest
from .repository import Repository, sha256_file
from .reports import generate_report
from .agent_profiles import AgentProfiles
from .instances import Instances, InstanceRequest
from .store import Store, uid

PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_SETTINGS = {"id": "main", "concurrent_sessions": 1, "storage_reserve_bytes": 1024**3, "duration_seconds": None, "spend_usd": None, "pricing": SUBSCRIPTION_PRICING}


def build_record(path, name=None, build_id=None):
    path = Path(path).resolve()
    manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    manifest["id"] = build_id or manifest.get("id") or uid("build_")
    manifest["name"] = name or manifest.get("name") or path.parent.name
    manifest["manifest_path"] = str(path)
    manifest["source_revision"] = manifest.get("source_revision") or json.dumps(manifest.get("commits", {}), sort_keys=True)
    for key in ("source_root", "catalog_path", "brief_path"):
        if manifest.get(key) and not Path(manifest[key]).is_absolute():
            manifest[key] = str((path.parent / manifest[key]).resolve())
    Repository(manifest)
    if not manifest.get("command"):
        raise ValueError("Build manifest must contain the worker launch command")
    manifest["manifest_sha256"] = sha256_file(path)
    return manifest


def initialize_local(store, credentials):
    if not store.get("settings", "main"):
        store.put("settings", DEFAULT_SETTINGS)
    registration = Path(os.environ.get("ASHAD_WORKER_REGISTRATION", PROJECT / ".data" / "workers.local.json"))
    if registration.exists():
        entries = json.loads(registration.read_text(encoding="utf-8-sig"))
        if isinstance(entries, dict):
            entries = entries.get("workers", [])
        for entry in entries:
            if entry.get("vnc_password_file"):
                password_path = Path(entry["vnc_password_file"])
                if not password_path.is_absolute():
                    password_path = PROJECT / password_path
                if password_path.is_file():
                    credentials.save("desktop:" + entry["id"], password_path.read_text().strip())
            if store.get("workers", entry["id"]):
                continue
            reference = "worker:" + entry["id"]
            token_path = Path(entry["token_file"])
            if not token_path.is_absolute():
                token_path = PROJECT / token_path
            credentials.save(reference, token_path.read_text().strip())
            record = {k: v for k, v in entry.items() if k not in {"token_file", "vnc_password_file"}}
            record.update(credential_ref=reference, enabled=True)
            store.put("workers", record)
    default_manifest = Path(os.environ.get("ASHAD_BUILD_MANIFEST", PROJECT / "builds" / "agronauts-decisions" / "manifest.json"))
    if default_manifest.exists():
        record = build_record(default_manifest)
        existing = store.get("builds", record["id"])
        if not existing or (existing.get("manifest_sha256") != record["manifest_sha256"] and not any(r["plan"]["build_id"] == record["id"] for r in store.list("runs"))):
            store.put("builds", record)


def create_app(data_root=None, credentials=None, start_services=True, initialize=True, ai_backend=None):
    store = Store(data_root or os.environ.get("ASHAD_DATA", PROJECT / ".data" / "controller"))
    credentials = credentials or Credentials()
    if initialize:
        initialize_local(store, credentials)
    elif not store.get("settings", "main"):
        store.put("settings", DEFAULT_SETTINGS)
    settings = store.require("settings", "main")
    if settings.get("pricing") != SUBSCRIPTION_PRICING or "reasoning_effort" in settings:
        settings.pop("reasoning_effort", None)
        settings["pricing"] = SUBSCRIPTION_PRICING
        store.put("settings", settings)
    previous_status = store.get("settings", "ai_status", {})
    if previous_status and previous_status.get("api") != AI_API:
        store.put("settings", {"configured": False, "status": "configuration_required", "model": MODEL,
                  "api": AI_API, "auth_method": "chatgpt", "billing": "subscription",
                  "message": "Sign in with ChatGPT in AI Settings to connect your Codex subscription."}, "ai_status")
    ai = OpenAI(store, credentials, backend=ai_backend)
    profiles = AgentProfiles(store, ai)
    coordinator = Coordinator(store, credentials, ai)
    instances = Instances(coordinator)
    from .game_mcp import GameMCP
    game_mcp = GameMCP(coordinator)

    @asynccontextmanager
    async def lifespan(app):
        tasks = []
        try:
            if start_services:
                await ai.validate()
                tasks.append(asyncio.create_task(coordinator.loop()))
                tasks.append(asyncio.create_task(heartbeats()))
            async with game_mcp.server.session_manager.run():
                yield
        finally:
            coordinator.stopping = True
            # Drain work before closing its clients and database. Every resource
            # must close even if another client's cleanup raises.
            async with AsyncExitStack() as cleanup:
                cleanup.callback(store.close)
                cleanup.push_async_callback(ai.close)
                for client in coordinator.clients.values():
                    cleanup.push_async_callback(client.close)
                tasks += list(coordinator.tasks.values()) + list(coordinator.ticks.values())
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def heartbeats():
        while True:
            game_mcp.cleanup()
            calls = []
            for run in store.list("runs"):
                if run["state"] in ACTIVE_STATES:
                    for ps in run["players"].values():
                        if ps["bound"]:
                            calls.append(coordinator.request(ps["worker_id"], "POST", "/heartbeat", {"epoch": ps["epoch"], "deadline": run.get("investigation_deadline")}))
            if calls:
                await asyncio.gather(*calls, return_exceptions=True)
            await asyncio.sleep(3)

    app = FastAPI(title="ASHAD Bot", version="1.0.0", lifespan=lifespan)
    app.state.store, app.state.coordinator, app.state.ai, app.state.credentials = store, coordinator, ai, credentials
    app.state.profiles = profiles

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        if request.url.path.startswith("/mcp"):
            try:
                game_mcp.resolve(request.headers.get("authorization", ""))
            except ValueError:
                return JSONResponse({"detail": "Player-bound MCP authentication required"}, status_code=401)
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "Cross-origin MCP request blocked"}, status_code=403)
        elif request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if request.headers.get("x-ashad-request") != "1" or (origin and urlparse(origin).netloc != request.headers.get("host")):
                return JSONResponse({"detail": "Use the local ASHAD dashboard; cross-origin writes are blocked."}, status_code=403)
        response = await call_next(request)
        response.headers.update({"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store", "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"})
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(ProviderError)
    @app.exception_handler(CodexError)
    async def provider(request, exc):
        return JSONResponse({"detail": exc.message, "status": exc.status}, status_code=409 if exc.status == "configuration_required" else 502)

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "model": MODEL, "api": AI_API, "local_only": True}

    @app.get("/api/runs/{run_id}/players/{player_id}/context")
    async def player_context(run_id: str, player_id: str):
        return await coordinator.structured.observe(run_id, player_id)

    @app.post("/api/runs/{run_id}/players/{player_id}/mcp-session")
    async def mcp_session(run_id: str, player_id: str, body: dict):
        return game_mcp.issue(run_id, player_id, control=body.get("control") is True)

    app.mount("/mcp", game_mcp.app)

    @app.get("/api/bootstrap")
    async def bootstrap():
        settings = dict(store.require("settings", "main"))
        memory = psutil.virtual_memory()
        settings["hardware"] = {"cpu_percent": psutil.cpu_percent(), "memory_percent": memory.percent, "memory_available": memory.available, "disk_free": psutil.disk_usage(str(store.root)).free, "gpu": None}
        builds, workers = store.list("builds"), store.list("workers")
        ai_status = {**store.get("settings", "ai_status", ai.status), "api": AI_API}
        model_options = [{"id": model, "label": model.replace("gpt-", "GPT-").replace("-luna", " Luna").replace("-sol", " Sol").replace("-astra", " Astra")} for model in SUPPORTED_MODELS]
        return {"settings": settings, "ai": ai_status, "model_options": model_options, "builds": builds, "workers": workers, "accounts": store.list("accounts"), "agents": store.list("agents"), "runs": store.list("runs"), "usage": store.list("usage"), "leases": store.leases()}

    @app.post("/api/agents/tune")
    async def tune_agent(body: dict):
        model = Plan(model=body.get("model", MODEL)).model
        return await profiles.tune(body.get("build_id"), model)

    @app.post("/api/instances")
    async def new_instance(body: InstanceRequest):
        return await instances.start(body)


    @app.get("/api/runs/{run_id}")
    async def run_detail(run_id: str):
        run = dict(store.require("runs", run_id))
        run["external_control"] = coordinator.external_control(run_id)
        run["event_states"] = run["events"]
        run["events"] = store.events(run_id, after=max(0, store.count_events(run_id)-200), limit=200)
        if run["state"] in {"Investigating", "Paused"} and not run.get("pause"):
            alerts = store.events(run_id, limit=store.count_events(run_id), search="investigation_started")
            latest = next((event for event in reversed(alerts) if event["kind"] == "investigation_started"), None)
            if latest and run["state"] == "Investigating":
                payload = latest["payload"]
                run["pause"] = coordinator.pause_details(payload.get("category", "investigation"), payload.get("reason", "Review the latest incident."), payload.get("players") or list(run["players"]), latest["timestamp"])
            else:
                run["pause"] = coordinator.pause_details("manual_pause", "Bot inputs are paused. Review the run before continuing.", list(run["players"]))
        for kind in ("incidents", "usage"):
            run[kind] = [r for r in store.list(kind) if r.get("run_id") == run_id]
        return run

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: str, after: int = 0, limit: int = 200, search: str = ""):
        store.require("runs", run_id)
        entries = store.events(run_id, after=max(0, after), limit=max(1, min(limit, 1000)), search=search)
        return {"events": entries, "next": entries[-1]["seq"] if entries else after, "total": store.count_events(run_id), "has_more": len(entries) >= min(limit, 1000)}

    @app.get("/api/runs/{run_id}/report")
    async def report(run_id: str):
        return {"markdown": generate_report(store, run_id)}

    @app.get("/api/runs/{run_id}/evidence")
    async def evidence(run_id: str):
        run = store.require("runs", run_id)
        files = run.get("retained_files", [])
        from urllib.parse import quote
        return {"files": [{**f, "url": f"/api/runs/{run_id}/files/{quote(f['name'])}"} for f in files], "gaps": run.get("limitations", []), "status": run["evidence_status"]}

    @app.post("/api/runs/{run_id}/evidence/collect")
    async def collect_evidence(run_id: str):
        await coordinator.collect_evidence(run_id)
        return await evidence(run_id)

    @app.get("/api/runs/{run_id}/files/{path:path}")
    async def file(run_id: str, path: str):
        root = store.run_dir(run_id)
        target = (root / path).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise HTTPException(404, "Evidence file not found")
        return FileResponse(target, filename=target.name)

    @app.get("/api/runs/{run_id}/players/{player_id}/frame")
    async def frame(run_id: str, player_id: str):
        run = store.require("runs", run_id)
        ps = run["players"].get(player_id)
        if not ps or not ps["launched"]:
            raise HTTPException(503, "Player feed is waiting or ended")
        try:
            return Response(await coordinator.client(ps["worker_id"]).screenshot(), media_type="image/jpeg")
        except Exception:
            raise HTTPException(503, "Player capture unavailable; this does not prove a game freeze") from None

    @app.get("/api/runs/{run_id}/players/{player_id}/live")
    async def live_view(run_id: str, player_id: str, epoch: str):
        run = store.require("runs", run_id)
        ps = run["players"].get(player_id)
        if run["state"] not in ACTIVE_STATES or not ps or not ps.get("launched"):
            raise HTTPException(503, "Player camera is waiting or ended")
        if epoch != ps["epoch"]:
            raise HTTPException(409, "Camera belongs to an earlier player binding")
        try:
            upstream = await coordinator.client(ps["worker_id"]).live_stream(run_id, player_id, epoch)
        except Exception:
            raise HTTPException(503, "Player camera is unavailable") from None
        async def frames():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            except httpx.HTTPError:
                return
            finally:
                with anyio.CancelScope(shield=True):
                    await upstream.aclose()
        return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
            background=BackgroundTask(upstream.aclose))

    @app.post("/api/runs/{run_id}/control")
    async def control(run_id: str, body: dict):
        if body.get("command") in {"takeover", "takeover_finish"}:
            raise ValueError("Use the player Take over or Hand back controls")
        return await coordinator.control(run_id, **body)

    @app.post("/api/runs/{run_id}/recalculate-goals")
    async def recalculate_goals(run_id: str, body: RecalculateGoalsRequest):
        run = await coordinator.recalculate_goals(run_id, body.command_id)
        return {**run, "external_control": coordinator.external_control(run_id)}

    @app.post("/api/runs/{run_id}/players/{player_id}/takeover")
    async def takeover(run_id: str, player_id: str):
        run = store.require("runs", run_id)
        ps = run["players"].get(player_id)
        if not ps:
            raise ValueError("Unknown player")
        worker = store.require("workers", ps["worker_id"])
        desktop_url = worker.get("desktop_url", "")
        parsed = urlparse(desktop_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Configure this worker's remote desktop URL before taking over")
        password = credentials.get("desktop:" + worker["id"])
        if not password:
            raise ValueError("Desktop access password is unavailable. Provision this worker's local VNC password and restart the controller.")
        run = await coordinator.control(run_id, "takeover", command_id=uid("human_"), player_id=player_id, desktop_url=desktop_url)
        # Only this explicit human request returns the desktop-only password. Never persist it in a run or ledger.
        return {"desktop_url": desktop_url, "password": password, "run": run}

    @app.post("/api/runs/{run_id}/takeover/finish")
    async def finish_takeover(run_id: str, body: dict):
        return await coordinator.control(run_id, "takeover_finish", command_id=uid("human_"),
            reason=body.get("reason", ""), resume=body.get("resume", True))

    @app.post("/api/runs/{run_id}/flag")
    async def flag(run_id: str, body: dict):
        async with coordinator.run_lock(run_id):
            await coordinator.cancel_tick(run_id)
            return await coordinator.flag(run_id, body["description"], body.get("expected", "Unknown; requires review"), body.get("players"))


    @app.post("/api/settings")
    async def settings(body: dict):
        current = store.require("settings", "main")
        if "concurrent_sessions" in body and (not isinstance(body["concurrent_sessions"], int) or not 1 <= body["concurrent_sessions"] <= 32):
            raise ValueError("Concurrent session cap must be 1–32")
        for key in ("duration_seconds", "spend_usd"):
            if key in body and body[key] is not None and (not isinstance(body[key], (int, float)) or body[key] <= 0):
                raise ValueError(f"{key} must be positive or unset")
        if "storage_reserve_bytes" in body and (not isinstance(body["storage_reserve_bytes"], int) or body["storage_reserve_bytes"] < 0):
            raise ValueError("Storage reserve must be a nonnegative byte count")
        if "reasoning_effort" in body:
            raise ValueError("Gameplay selections use the configured Codex effort. Refresh the dashboard.")
        allowed = {"concurrent_sessions", "storage_reserve_bytes", "duration_seconds", "spend_usd"}
        current.update({k: v for k, v in body.items() if k in allowed})
        return store.put("settings", current)

    @app.get("/api/settings/codex/status")
    async def codex_status():
        return await ai.validate()

    def require_ai_idle():
        if profiles.lock.locked():
            raise ValueError("Wait for agent tuning to finish before changing AI sign-in.")
        if any(r["state"] in {"Queued", "Starting", "Running", "Stopping"} for r in store.list("runs")):
            raise ValueError("Pause or stop active bots and stop queued runs before changing Codex sign-in.")

    @app.post("/api/settings/codex/login")
    async def codex_login():
        async with coordinator.lock:
            require_ai_idle()
            login = await ai.backend.login_start()
            await ai.validate()
            return login

    @app.post("/api/settings/codex/login/cancel")
    async def codex_login_cancel():
        async with coordinator.lock:
            await ai.backend.login_cancel()
            return await ai.validate()

    @app.post("/api/settings/codex/logout")
    async def codex_logout():
        async with coordinator.lock:
            require_ai_idle()
            await ai.backend.logout()
            return await ai.validate()

    @app.post("/api/builds")
    async def add_build(body: dict):
        return store.put("builds", build_record(body["manifest_path"], body.get("name"), body.get("id")))

    @app.get("/api/builds/{build_id}/catalog")
    async def catalog(build_id: str):
        build = store.require("builds", build_id)
        return Repository(build).catalog(build)

    @app.post("/api/workers")
    async def add_worker(body: dict):
        worker_id = body.get("id") or uid("worker_")
        url = urlparse(body["url"])
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            raise ValueError("Worker requires an HTTP(S) private service URL without embedded credentials")
        reference = "worker:" + worker_id
        if body.get("token"):
            credentials.save(reference, body["token"])
        if not credentials.get(reference):
            raise ValueError("Worker service token is required")
        record = {k: body[k] for k in ("name", "url", "desktop_url", "host_address", "enabled") if k in body}
        record.update(id=worker_id, credential_ref=reference)
        if worker_id in coordinator.clients:
            await coordinator.clients.pop(worker_id).close()
        return store.put("workers", record)

    @app.post("/api/workers/{worker_id}/probe")
    async def probe_worker(worker_id: str):
        worker = store.require("workers", worker_id)
        try:
            status = await coordinator.client(worker_id).health()
        except Exception as exc:
            status = {"status": "unavailable", "message": str(exc)}
        worker["last_probe"] = status
        worker["last_probe_at"] = time.time()
        store.put("workers", worker)
        return status

    @app.post("/api/workers/{worker_id}/steam/start")
    async def steam_start(worker_id: str):
        if any(l["kind"] == "worker" and l["resource_id"] == worker_id for l in store.leases()):
            raise ValueError("Finish the active worker lease before Steam account setup")
        return await coordinator.client(worker_id).steam_setup_start()

    @app.post("/api/workers/{worker_id}/steam/finish")
    async def steam_finish(worker_id: str):
        return await coordinator.client(worker_id).steam_setup_finish()

    @app.post("/api/workers/{worker_id}/steam/status")
    async def steam_status(worker_id: str):
        status = await coordinator.client(worker_id).status()
        steam = status.get("steam", {})
        last = steam.get("last_verification", {})
        for account in store.list("accounts"):
            if account["worker_id"] == worker_id:
                verified = ((steam.get("authenticated") and steam.get("identity_verified") and steam.get("app_id_verified")) or last.get("verified")) and str(steam.get("steam_id")) == str(account["steam_id"]) and str(steam.get("app_id")) == str(account["app_id"])
                account["readiness"] = "ready" if verified else "human_sign_in_required"
                account["last_check"] = time.time()
                store.put("accounts", account)
        return steam

    @app.post("/api/workers/{worker_id}/steam/verify")
    async def steam_verify(worker_id: str, body: dict):
        if any(l["kind"] == "worker" and l["resource_id"] == worker_id for l in store.leases()):
            raise ValueError("Steam verification needs an unleased worker")
        account = store.require("accounts", body["account_id"])
        build = store.require("builds", body["build_id"])
        if account["worker_id"] != worker_id or account["app_id"] != build.get("steam_app_id"):
            raise ValueError("Account desktop and actual game App ID must match the selected build")
        result = await coordinator.client(worker_id).steam_verify(account["app_id"], account["steam_id"], build["command"])
        account.update(readiness="ready" if result.get("verified") else "human_sign_in_required", last_check=time.time(), verification=result)
        store.put("accounts", account)
        return result

    @app.post("/api/accounts")
    async def add_account(body: dict):
        if not str(body.get("steam_id", "")).isdigit() or int(body.get("app_id") or 0) in {0, 480}:
            raise ValueError("Specify the actual Steam ID64 and Agronauts App ID (not 480)")
        store.require("workers", body["worker_id"])
        for account in store.list("accounts"):
            if account["id"] != body.get("id") and (account["steam_id"] == str(body["steam_id"]) or account["worker_id"] == body["worker_id"]):
                raise ValueError("Steam identity and persistent worker must be unique in the reusable account pool")
        record = {k: body[k] for k in ("name", "worker_id", "steam_id", "app_id")}
        record.update(id=body.get("id") or uid("account_"), steam_id=str(body["steam_id"]), readiness="human_sign_in_required")
        return store.put("accounts", record)


    @app.post("/api/runs/{run_id}/reconcile")
    async def reconcile(run_id: str):
        async with coordinator.lock:
            run = store.require("runs", run_id)
            if run["state"] != "Ended":
                raise ValueError("Reconciliation is for ended runs with unresolved teardown")
            results = []
            for pid, ps in run["players"].items():
                if ps["teardown_confirmed"]:
                    continue
                try:
                    result = await coordinator.request(ps["worker_id"], "POST", "/stop", {"epoch": ps["epoch"], "terminate": True, "reason": "human_reconciliation"})
                    ps["teardown_confirmed"] = result.get("teardown_confirmed") is True
                    if ps["teardown_confirmed"]:
                        ps.update(phase="ended", launched=False)
                    results.append({"player_id": pid, "confirmed": ps["teardown_confirmed"]})
                except Exception as exc:
                    results.append({"player_id": pid, "confirmed": False, "reason": type(exc).__name__})
            if all(p["teardown_confirmed"] for p in run["players"].values()):
                store.release(run_id)
            store.put("runs", run)
            store.event(run_id, "teardown_reconciled", {"results": results, "actor": "human"})
            await coordinator.collect_evidence(run_id)
            return store.require("runs", run_id)

    @app.delete("/api/{kind}/{record_id}")
    async def delete_catalog(kind: str, record_id: str):
        if kind not in {"builds", "workers", "accounts"}:
            raise HTTPException(404)
        if any(l["resource_id"] == record_id for l in store.leases()):
            raise ValueError("This resource has an active or unresolved lease")
        for run in store.list("runs"):
            if run["state"] in ACTIVE_STATES | {"Queued"} and record_id in json.dumps(run["plan"]):
                raise ValueError("Resource is referenced by an approved run")
        store.delete(kind, record_id)
        return {"deleted": record_id, "files_preserved": True}

    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    async def index():
        return FileResponse(static / "index.html")

    return app
