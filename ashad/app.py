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
from .coordinator import Coordinator, InstanceConflict
from .credentials import Credentials
from .models import Plan, MODEL, SUPPORTED_MODELS, AI_API, ACTIVE_STATES, RecalculateGoalsRequest
from .repository import Repository, sha256_file
from .reports import generate_report
from .agent_profiles import AgentProfiles
from .projects import Projects
from .workspace_import import import_workspace, IMPORT_ID as WORKSPACE_IMPORT_ID
from .instances import Instances, InstanceRequest
from .store import Store, uid

PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_SETTINGS = {"id": "main", "concurrent_sessions": 1, "storage_reserve_bytes": 1024**3, "duration_seconds": None, "spend_usd": None, "pricing": SUBSCRIPTION_PRICING}


async def disk_mutation(operation, *args, **kwargs):
    """Keep the caller's admission hold until actual disk work finishes, even on HTTP cancellation."""
    task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


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
        if not existing:
            # Startup may discover a legacy build, never replace pinned identity or
            # erase explicit project membership. Changed revisions need a new ID.
            store.put("builds", record)


def create_app(data_root=None, credentials=None, start_services=True, initialize=True, ai_backend=None, controller_lock=None):
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
    app.state.controller_lock = controller_lock
    app.state.operations = {}
    app.state.tuning_task = None
    app.state.tuning_request_id = None
    app.state.instance_requests = {}

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
        mutation = request.method in {"POST", "PUT", "PATCH", "DELETE"}
        if mutation and coordinator.import_interrupted():
            return JSONResponse({"detail": "Workspace import is incomplete. Editing, sign-in and admission are blocked until operator reconciliation; keep the verified backup and journal."}, status_code=409)
        if mutation and coordinator.draining:
            return JSONResponse({"detail": "The controller is draining; wait for shutdown or retry afterward."}, status_code=409)
        operation = asyncio.current_task() if mutation else None
        if operation:
            app.state.operations[operation] = {"kind": "tuning" if request.url.path == "/api/agents/tune" else "mutation",
                                               "path": request.url.path}
        try:
            response = await call_next(request)
        finally:
            if operation:
                app.state.operations.pop(operation, None)
        response.headers.update({"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store", "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"})
        return response

    @app.exception_handler(InstanceConflict)
    async def occupied_instance(request, exc):
        return JSONResponse({"detail": str(exc), "code": "active_instance_conflict", "owning_instances": exc.owners}, status_code=409)

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
        settings.update(saved_concurrent_sessions=settings.get("concurrent_sessions", 1), concurrent_sessions=1, max_active_instances=1)
        memory = psutil.virtual_memory()
        settings["hardware"] = {"cpu_percent": psutil.cpu_percent(), "memory_percent": memory.percent, "memory_available": memory.available, "disk_free": psutil.disk_usage(str(store.root)).free, "gpu": None}
        builds, workers = store.list("builds"), store.list("workers")
        # Persisted account-status rows are history, not current authentication.
        # In particular an import/restart must validate this backend's own home.
        ai_status = {**ai.status, "api": AI_API}
        model_options = [{"id": model, "label": model.replace("gpt-", "GPT-").replace("-luna", " Luna").replace("-sol", " Sol").replace("-astra", " Astra")} for model in SUPPORTED_MODELS]
        owners = coordinator.occupied_instances()
        interrupted = coordinator.import_interrupted()
        blocked_reason = ("Workspace import is interrupted; operator reconciliation is required" if interrupted else
                          "The controller is completing setup or shutdown; wait before starting" if coordinator.draining else
                          "One instance already owns the global slot" if owners else None)
        return {"settings": settings, "ai": ai_status, "model_options": model_options, "builds": builds, "workers": workers, "accounts": store.list("accounts"), "agents": store.list("agents"), "runs": store.list("runs"), "usage": store.list("usage"), "leases": store.leases(), "projects": store.list("projects"),
                "instance_slot": {"available": blocked_reason is None, "owners": owners, "limit": 1, "blocked_reason": blocked_reason},
                "workspace_import": {"interrupted": coordinator.import_interrupted(), "completed": store.get("migrations", WORKSPACE_IMPORT_ID),
                                     "readiness_verified": False}}

    @app.post("/api/workspace/import")
    async def workspace_import(body: dict):
        if app.state.controller_lock is None or app.state.controller_lock.closed:
            raise ValueError("Import requires the process lock held by the owned ASHAD controller entry")
        for name in ("source_root", "backup_dir"):
            if not isinstance(body.get(name), str) or not Path(body[name]).is_absolute():
                raise ValueError("Import requires explicit absolute source and fresh backup paths")
        async with coordinator.lock:
            if coordinator.draining or coordinator.stopping or coordinator.occupied_instances():
                raise ValueError("Import requires an idle, reconciled target controller")
            login = getattr(ai.backend, "login_wait", None)
            if app.state.tuning_task or len(app.state.operations) > 1 or (login and not login.done()):
                raise ValueError("Wait for provider, sign-in and setup operations before importing")
            previous = store.get("migrations", WORKSPACE_IMPORT_ID)
            coordinator.draining = True
            try:
                result = await disk_mutation(import_workspace, store,
                    source_root=body["source_root"], backup_dir=body["backup_dir"],
                    external_inventory=body.get("external_inventory"), confirmed=body.get("confirmed") is True,
                    source_idle_confirmed=body.get("source_idle_confirmed") is True,
                    external_backup_confirmed=body.get("external_backup_confirmed") is True,
                    target_drained=True)  # Authority is server-derived, never a body flag.
                if not previous:
                    ai.status = {"configured": False, "status": "not_validated", "api": AI_API,
                                 "auth_method": "chatgpt", "model": MODEL,
                                 "message": "Imported history is not readiness. Re-check ChatGPT sign-in and compatible desktops in this workspace."}
                return result
            except RuntimeError as exc:
                return JSONResponse({"detail": "Import refused: " + str(exc)}, status_code=409)
            finally:
                coordinator.draining = False

    @app.post("/api/projects")
    async def create_project(body: dict):
        if set(body) != {"name", "request_id"}:
            raise ValueError("Project creation requires only name and a stable request_id")
        return Projects(store).create(body["name"], body["request_id"])

    @app.get("/api/projects/{project_id}")
    async def project_detail(project_id: str):
        return {**store.require("projects", project_id),
                **{kind: [record for record in store.list(kind) if record.get("project_id") == project_id]
                   for kind in ("builds", "agents", "runs")}}

    @app.post("/api/projects/migration/preview")
    async def migration_preview(body: dict):
        login = getattr(ai.backend, "login_wait", None)
        operations = bool(app.state.tuning_task) or len(app.state.operations) > 1 or bool(login and not login.done())
        return Projects(store).preview(body.get("groups"), operations_active=operations)

    @app.post("/api/projects/migration/apply")
    async def migration_apply(body: dict):
        if app.state.controller_lock is None or app.state.controller_lock.closed:
            raise ValueError("Migration requires the process lock held by the owned ASHAD controller entry")
        if not isinstance(body.get("backup_dir"), str) or not Path(body["backup_dir"]).is_absolute():
            raise ValueError("Choose an absolute, fresh migration backup directory")
        if not isinstance(body.get("preview_token"), str):
            raise ValueError("Review a migration preview before confirming")
        # Own the same admission authority as launch/resume. The store helper also
        # rejects every queued/live/unresolved lease and changing source snapshot.
        async with coordinator.lock:
            if coordinator.draining:
                raise ValueError("Controller is already draining")
            operations = bool(app.state.tuning_task) or len(app.state.operations) > 1
            login = getattr(ai.backend, "login_wait", None)
            operations = operations or bool(login and not login.done())
            coordinator.draining = True
            try:
                return await disk_mutation(Projects(store).apply, body.get("groups"),
                    preview_token=body.get("preview_token"), backup_dir=body.get("backup_dir"),
                    confirmed=body.get("confirmed") is True, operations_active=operations,
                    codex_home=store.root / "codex")
            finally:
                coordinator.draining = False

    @app.post("/api/agents/tune")
    async def tune_agent(body: dict):
        model = Plan(model=body.get("model", MODEL)).model
        if app.state.tuning_task:
            request_id = body.get("request_id")
            if isinstance(request_id, str) and store.command("tuning", request_id):
                return await profiles.tune(body.get("build_id"), model, project_id=body.get("project_id"),
                                           name=body.get("name"), request_id=request_id)
            raise ValueError("An agent is already being tuned. Wait for it to finish.")
        if any(key in body for key in ("project_id", "name", "request_id")) and not body.get("request_id"):
            raise ValueError("Use a stable tuning request_id; explicit retune uses a new ID")
        app.state.tuning_task = asyncio.current_task()
        app.state.tuning_request_id = body.get("request_id")
        try:
            if any(key in body for key in ("project_id", "name", "request_id")):
                return await profiles.tune(body.get("build_id"), model, project_id=body.get("project_id"),
                                           name=body.get("name"), request_id=body.get("request_id"))
            return await profiles.tune(body.get("build_id"), model)
        except asyncio.CancelledError:
            return JSONResponse({"detail": "Agent tuning was cancelled; no incomplete profile was saved."}, status_code=409)
        finally:
            app.state.tuning_task = None
            app.state.tuning_request_id = None

    @app.get("/api/agents/tuning/{request_id}")
    async def tuning_request(request_id: str):
        saved = store.command("tuning", request_id)
        if saved:
            return {"status": "completed", "request_id": request_id, "agent": store.require("agents", saved["agent_id"])}
        running = bool(app.state.tuning_task and app.state.tuning_request_id == request_id)
        return {"status": "in_progress" if running else "unknown", "request_id": request_id}

    @app.get("/api/instances/requests/{request_id}")
    async def instance_request(request_id: str):
        launch = store.command("launch", request_id)
        if launch:
            return {"status": "accepted", "request_id": request_id, "run": store.require("runs", launch["run_id"])}
        prepared = store.command("instance", request_id)
        legacy_run = store.get("runs", prepared.get("run_id")) if prepared and prepared.get("run_id") else None
        if legacy_run:
            return {"status": "accepted", "request_id": request_id, "run": legacy_run}
        status = ("in_progress" if app.state.instance_requests.get(request_id) else
                  "prepared" if prepared else "unknown")
        original = prepared.get("request") if prepared else None
        original = {key: original[key] for key in ("agent_id", "project_id", "primary_goal", "transport", "request_id") if key in original} if isinstance(original, dict) else None
        return {"status": status, "request_id": request_id, "request": original}

    @app.post("/api/instances")
    async def new_instance(body: InstanceRequest):
        tasks = app.state.instance_requests.setdefault(body.request_id, set())
        current = asyncio.current_task()
        tasks.add(current)
        try:
            return await instances.start(body)
        finally:
            tasks.discard(current)
            if not tasks:
                app.state.instance_requests.pop(body.request_id, None)


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
        if "concurrent_sessions" in body and (type(body["concurrent_sessions"]) is not int or body["concurrent_sessions"] != 1):
            raise ValueError("This release allows exactly one active instance; concurrency cannot be increased")
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
        if not isinstance(body.get("manifest_path"), str) or not Path(body["manifest_path"]).is_absolute():
            raise ValueError("Select an absolute manifest path")
        record = build_record(body["manifest_path"], body.get("name"), body.get("id"))
        return Projects(store).register_build(record, body.get("project_id"))

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
        return await coordinator.reconcile(run_id)

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
