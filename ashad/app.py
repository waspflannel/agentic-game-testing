"""Loopback dashboard and local controller API."""
from __future__ import annotations
import asyncio
import anyio
import httpx
from contextlib import AsyncExitStack, asynccontextmanager
import json
import math
import mimetypes
from pathlib import Path
import re
import time
from urllib.parse import urlparse
import psutil
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from .ai import OpenAI, ProviderError, SUBSCRIPTION_PRICING
from .claude_backend import ClaudeError
from .codex_backend import CodexError
from .coordinator import Coordinator, InstanceConflict
from .credentials import Credentials
from .models import Plan, MODEL, MODEL_OPTIONS, CLAUDE_MODEL, provider_of, AI_API, ACTIVE_STATES, RecalculateGoalsRequest
from .repository import Repository, sha256_file
from .reports import generate_report
from .game_controls import BUSY, MODEL_NOT_ALLOWED, SIGN_IN_WAIT, GameControls, migrate_configuration_names, migrate_tuned_configurations
from .projects import Projects
from .workspace_environment import inspect_fresh_environment, prepare_environment, validate_build
from .instances import Instances, InstanceRequest, default_model
from .store import Store, uid

PROJECT = Path(__file__).resolve().parents[1]
# The renderer uses module scripts, which browsers refuse unless served as JavaScript;
# Windows registry entries can otherwise map .js to text/plain.
mimetypes.add_type("text/javascript", ".js")
# DNS rebinding defense: a browser only sends one of these Host values to this process.
LOOPBACK_HOST = re.compile(r"(?:127\.0\.0\.1|localhost|\[::1\])(?::\d{1,5})?", re.IGNORECASE)
DEFAULT_SETTINGS = {"id": "main", "concurrent_sessions": 1, "storage_reserve_bytes": 1024**3, "duration_seconds": None, "spend_usd": None, "pricing": SUBSCRIPTION_PRICING}


async def settled_operation(operation):
    """Keep admission held until the actual operation finishes, even on HTTP cancellation."""
    task = asyncio.create_task(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # ASGI cancellation is level-triggered; cleanup must not cancel real work.
        with anyio.CancelScope(shield=True):
            await asyncio.shield(task)
        raise


def finite_number(value):
    """JSON numbers only: rejects bools, NaN and +/-Infinity (ints are always finite)."""
    return type(value) is int or (type(value) is float and math.isfinite(value))


def build_record(path, name=None, build_id=None):
    path = Path(path).resolve()
    manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(manifest, dict):
        raise ValueError("Build manifest must be a JSON object")
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


def create_app(data_root, credentials=None, start_services=True, ai_backend=None, claude_backend=None, controller_lock=None):
    store = Store(data_root)
    credentials = credentials or Credentials()
    if not store.get("settings", "main"):
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
    ai = OpenAI(store, credentials, backend=ai_backend, claude_backend=claude_backend)
    migrate_configuration_names(store)
    migrate_tuned_configurations(store)
    controls = GameControls(store, ai)
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
                await ai.validate(CLAUDE_MODEL)
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
            try:
                game_mcp.cleanup()
                calls = []
                for run in store.list("runs"):
                    if run["state"] in ACTIVE_STATES:
                        for ps in run["players"].values():
                            if ps["bound"]:
                                calls.append(coordinator.request(ps["worker_id"], "POST", "/heartbeat", {"epoch": ps["epoch"], "deadline": run.get("investigation_deadline")}))
                if calls:
                    await asyncio.gather(*calls, return_exceptions=True)
            except Exception as exc:
                print(f"ASHAD heartbeat error: {type(exc).__name__}", flush=True)
            await asyncio.sleep(3)

    app = FastAPI(title="ASHAD Bot", version="1.0.0", lifespan=lifespan)
    app.state.store, app.state.coordinator, app.state.ai, app.state.credentials = store, coordinator, ai, credentials
    app.state.controls = controls
    app.state.controller_lock = controller_lock
    app.state.operations = {}
    app.state.controls_task = None
    app.state.controls_request_id = None
    app.state.instance_requests = {}

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        # Every route, method, /mcp and static asset: a rebound DNS name must not
        # reach this loopback service even when its Origin matches its Host.
        hosts = request.headers.getlist("host")
        if len(hosts) != 1 or not LOOPBACK_HOST.fullmatch(hosts[0]):
            return JSONResponse({"detail": "ASHAD accepts only loopback Host headers (127.0.0.1, localhost or [::1])."}, status_code=403)
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
            app.state.operations[operation] = {"kind": "controls" if request.url.path == "/api/configurations/controls" else "mutation",
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
    @app.exception_handler(ClaudeError)
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

    def instance_slot():
        owners = coordinator.occupied_instances()
        reason = ("Workspace import is interrupted; operator reconciliation is required" if coordinator.import_interrupted() else
                  "The controller is completing setup or shutdown; wait before starting" if coordinator.draining or coordinator.stopping else
                  "One instance already owns the global slot" if owners else None)
        return {"available": reason is None, "owners": owners, "limit": 1, "blocked_reason": reason}

    def operations_active():
        """Another HTTP mutation (besides the caller), Get game controls, or pending sign-in."""
        login = getattr(ai.backend, "login_wait", None)
        return bool(app.state.controls_task) or len(app.state.operations) > 1 or bool(login and not login.done())

    def require_unleased_worker(worker_id, message):
        if any(l["kind"] == "worker" and l["resource_id"] == worker_id for l in store.leases()):
            raise ValueError(message)

    def require_unreferenced(record_id):
        if any(l["resource_id"] == record_id for l in store.leases()):
            raise ValueError("This resource has an active or unresolved lease")
        for run in store.list("runs"):
            if run["state"] in ACTIVE_STATES | {"Queued"}:
                plan = run["plan"]
                players = plan.get("players", [])
                if record_id == plan.get("build_id") or any(record_id in (p.get("worker_id"), p.get("account_id")) for p in players):
                    raise ValueError("Resource is referenced by an approved run")

    def require_controller_lock(action):
        if app.state.controller_lock is None or app.state.controller_lock.closed:
            raise ValueError(f"{action} requires the process lock held by the owned ASHAD controller entry")

    @app.get("/api/usage")
    async def usage():
        # Full history only for the Usage page; bootstrap polling carries usage_count.
        return {"usage": await asyncio.to_thread(store.list, "usage")}

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
        model_options = [dict(o) for o in MODEL_OPTIONS]
        return {"settings": settings, "ai": ai_status, "ai_claude": ai.claude_status, "model_options": model_options, "default_model": default_model(ai), "builds": builds, "workers": workers, "accounts": store.list("accounts"), "configurations": store.list("configurations"), "runs": store.list("runs"), "usage_count": store.count("usage"), "leases": store.leases(), "projects": store.list("projects"),
                "instance_slot": instance_slot(),
                "workspace_import": {"interrupted": coordinator.import_interrupted()}}

    @app.post("/api/projects")
    async def create_project(body: dict):
        if set(body) != {"name", "request_id"}:
            raise ValueError("Project creation requires only name and a stable request_id")
        return Projects(store).create(body["name"], body["request_id"])

    @app.get("/api/projects/{project_id}")
    async def project_detail(project_id: str):
        return {**store.require("projects", project_id),
                **{kind: [record for record in store.list(kind) if record.get("project_id") == project_id]
                   for kind in ("builds", "configurations", "runs")}}

    @app.get("/api/projects/{project_id}/readiness")
    async def project_readiness(project_id: str, build_id: str, configuration_id: str | None = None, model: str | None = None):
        async def provider_check(model):
            try:
                status = await ai.validate(model)
                return {"ready": status.get("configured") is True and status.get("status") == "ready",
                        "status": status.get("status", "unknown"), "message": status.get("message", "Unknown AI connection"), "model": model}
            except (ProviderError, CodexError, ClaudeError) as exc:
                return {"ready": False, "status": exc.status, "message": exc.message, "model": model}

        Projects(store).project_for_build(build_id, project_id)
        build = store.require("builds", build_id)
        if configuration_id:
            Projects(store).project_for_configuration(configuration_id, project_id)
            selected = store.require("configurations", configuration_id)
            if selected["build_id"] != build_id:
                raise ValueError("Saved configuration belongs to a different build")
        build_validation = {"ready": False, "reason": "Build has not been validated"}
        try:
            await asyncio.to_thread(validate_build, build)
            Repository(build)  # Play reads the pinned source snapshot; validate_build does not check it.
            if configuration_id:
                await asyncio.to_thread(controls.validated, configuration_id)
            build_validation = {"ready": True, "reason": "Exact supported manifest, engine and artifact pins verified"}
        except (OSError, ValueError, KeyError) as exc:
            build_validation["reason"] = str(exc)
        if model:
            provider = await provider_check(Plan(model=model).model)
            provider["providers"] = [provider_of(provider["model"])] if provider["ready"] else []
        else:  # No model chosen yet: either connected subscription is enough.
            checks = await asyncio.gather(provider_check(MODEL), provider_check(CLAUDE_MODEL))
            ready = [check for check in checks if check["ready"]]
            providers = sorted(provider_of(check["model"]) for check in ready)  # fixed order: claude, codex
            provider = {**(ready[0] if ready else {**checks[0], "message": "Set up AI in Settings › Account."}), "providers": providers}
        environments = []
        if build_validation["ready"]:
            occupied = {lease["resource_id"] for lease in store.leases() if lease["kind"] == "worker"}
            accounts = {record["worker_id"]: record for record in store.list("accounts") if record.get("app_id") == build.get("steam_app_id")}
            workers = store.list("workers")
            checks = await asyncio.gather(*(inspect_fresh_environment(build, worker, credentials,
                occupied=worker["id"] in occupied, account=accounts.get(worker["id"])) for worker in workers))
            environments = [{"id": worker["id"], "name": worker.get("name", worker["id"]), **check} for worker, check in zip(workers, checks)]
        slot = instance_slot()
        steam_ready = any(environment["steam"]["ready"] for environment in environments)
        return {"project_id": project_id, "build_id": build_id, "provider": provider,
                "build_validation": build_validation, "environments": environments, "slot": slot,
                "steam": {"ready": steam_ready, "reason": "Fresh matching licensed identity verified" if steam_ready else "No fresh matching licensed Steam identity is proven; no App ID 480 or ENet fallback"},
                "ready": provider["ready"] and build_validation["ready"] and slot["available"] and any(environment["ready"] for environment in environments)}

    @app.post("/api/projects/{project_id}/environments/prepare")
    async def project_prepare(project_id: str, body: dict):
        if set(body) != {"build_id", "request_id", "confirmed"} or body.get("confirmed") is not True:
            raise ValueError("Preparation requires build_id, stable request_id and explicit confirmation of image/volume/credential creation")
        require_controller_lock("Preparation")
        Projects(store).project_for_build(body["build_id"], project_id)
        build = store.require("builds", body["build_id"])
        async with coordinator.lock:
            coordinator.require_instance_slot()
            if coordinator.draining or coordinator.stopping or operations_active():
                raise ValueError("Wait for admission, Get game controls, sign-in and setup operations before preparation")
            coordinator.draining = True
            try:
                resources = PROJECT / "ashad_resources" if (PROJECT / "ashad_resources").is_dir() else PROJECT
                return await settled_operation(prepare_environment(store, credentials, project_id=project_id,
                    build=build, request_id=body["request_id"], confirmed=True, target_drained=True, resource_root=resources))
            finally:
                coordinator.draining = False

    @app.post("/api/configurations/controls")
    async def get_controls(body: dict):
        if "model" in body:
            raise ValueError(MODEL_NOT_ALLOWED)
        if app.state.controls_task:
            request_id = body.get("request_id")
            if isinstance(request_id, str) and store.command("tuning", request_id):
                return await controls.gather(body.get("build_id"), project_id=body.get("project_id"),
                                             name=body.get("name"), request_id=request_id)
            raise ValueError(BUSY)
        if any(key in body for key in ("project_id", "name", "request_id")) and not body.get("request_id"):
            raise ValueError("Use a stable Get game controls request_id; Get controls again uses a new ID")
        app.state.controls_task = asyncio.current_task()
        app.state.controls_request_id = body.get("request_id")
        try:
            return await controls.gather(body.get("build_id"), project_id=body.get("project_id"),
                                         name=body.get("name"), request_id=body.get("request_id"))
        except asyncio.CancelledError:
            # Only the owned quit drain (DesktopLifecycle) cancels Get game controls on purpose; server
            # shutdown or a dropped client must keep propagating cancellation.
            if coordinator.stopping or not coordinator.draining:
                raise
            return JSONResponse({"detail": "Get game controls was cancelled; nothing was saved."}, status_code=409)
        finally:
            app.state.controls_task = None
            app.state.controls_request_id = None

    @app.get("/api/configurations/controls/{request_id}")
    async def controls_request(request_id: str):
        saved = store.command("tuning", request_id)
        if saved:
            return {"status": "completed", "request_id": request_id, "configuration": store.require("configurations", saved["configuration_id"])}
        running = bool(app.state.controls_task and app.state.controls_request_id == request_id)
        if not running:
            return {"status": "unknown", "request_id": request_id}
        return {"status": "in_progress", "request_id": request_id, "progress": controls_progress()}

    def controls_progress():
        """The running Get game controls stage and batches, plus its model calls so far (from usage rows), or None."""
        progress = controls.progress
        if not progress:
            return None
        rows = store.usage_since("tuning", progress["started_at"])
        return {**progress, "model_calls": len(rows), "rejected_replies": sum(status != "completed" for status, _ in rows),
                "last_call_at": max((at for _, at in rows), default=None)}

    @app.get("/api/configurations/{configuration_id}/controls")
    async def configuration_controls(configuration_id: str):
        return (await asyncio.to_thread(controls.validated, configuration_id))[1]

    @app.get("/api/instances/requests/{request_id}")
    async def instance_request(request_id: str):
        # Instances.start stores its launch under "instance_"; bare keys are direct launches.
        launch = store.command("launch", "instance_" + request_id) or store.command("launch", request_id)
        if launch:
            return {"status": "accepted", "request_id": request_id, "run": store.require("runs", launch["run_id"])}
        prepared = store.command("instance", request_id)
        legacy_run = store.get("runs", prepared.get("run_id")) if prepared and prepared.get("run_id") else None
        if legacy_run:
            return {"status": "accepted", "request_id": request_id, "run": legacy_run}
        status = ("in_progress" if app.state.instance_requests.get(request_id) else
                  "prepared" if prepared else "unknown")
        original = prepared.get("request") if prepared else None
        original = {key: original[key] for key in ("configuration_id", "project_id", "primary_goal", "transport", "request_id") if key in original} if isinstance(original, dict) else None
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


    def event_page(run_id, after=0, limit=200, search="", *, before=None, tail=False):
        reverse = tail or before is not None
        with store.lock:
            entries = store.events(run_id, after=after, limit=limit + 1, search=search, before=before, tail=tail)
            total = store.count_events(run_id)
            has_more = len(entries) > limit
            entries = entries[-limit:] if reverse else entries[:limit]
            # Counts describe all ledger rows outside the page, not search matches.
            if entries:
                older_count = store.count_events(run_id, before=entries[0]["seq"])
                newer_count = store.count_events(run_id, after=entries[-1]["seq"])
            else:
                boundary = before if before is not None else (None if tail else after + 1)
                older_count = store.count_events(run_id, before=boundary) if boundary is not None else total
                newer_count = total - older_count
        return {"events": entries, "next": entries[-1]["seq"] if entries else after,
                "before": entries[0]["seq"] if entries else before,
                "total": total, "has_more": has_more,
                "older_count": older_count, "newer_count": newer_count}

    @app.get("/api/runs/{run_id}")
    async def run_detail(run_id: str):
        run = dict(store.require("runs", run_id))
        run["external_control"] = coordinator.external_control(run_id)
        run["event_states"] = run["events"]
        with store.lock:
            page = event_page(run_id, limit=200, tail=True)
            run["events"] = page.pop("events")
            recent = run["events"][-100:]
            page["recent_100_older_count"] = store.count_events(run_id, before=recent[0]["seq"]) if recent else 0
        run["events_page"] = page
        if run["state"] in {"Investigating", "Paused"} and not run.get("pause"):
            latest = store.latest_event(run_id, "investigation_started") if run["state"] == "Investigating" else None
            if latest and run["state"] == "Investigating":
                payload = latest["payload"]
                run["pause"] = coordinator.pause_details(payload.get("category", "investigation"), payload.get("reason", "Review the latest incident."), payload.get("players") or list(run["players"]), latest["timestamp"])
            else:
                run["pause"] = coordinator.pause_details("manual_pause", "Bot inputs are paused. Review the run before continuing.", list(run["players"]))
        for kind in ("incidents", "usage"):
            run[kind] = await asyncio.to_thread(store.records_for_run, kind, run_id)
        return run

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: str, after: int = 0, limit: int = 200, search: str = "", before: int | None = None, tail: bool = False):
        store.require("runs", run_id)
        if after > 0 and (before is not None or tail):
            raise ValueError("Use after for newer events, or before/tail for older events, not both")
        return event_page(run_id, after=max(0, after), limit=max(1, min(limit, 1000)), search=search,
                          before=max(0, before) if before is not None else None, tail=tail)

    @app.get("/api/runs/{run_id}/report")
    async def report(run_id: str):
        return {"markdown": await asyncio.to_thread(generate_report, store, run_id)}

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
        if not store.get("runs", run_id):  # run_dir creates directories; never for unknown IDs.
            raise HTTPException(404, "Evidence file not found")
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
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.post("/api/runs/{run_id}/control")
    async def control(run_id: str, body: dict):
        body = dict(body)
        command = body.pop("command", None)
        if not isinstance(command, str) or "run_id" in body:
            raise ValueError("Control requires a command; the run ID comes from the URL")
        if command in {"takeover", "takeover_finish"}:
            raise ValueError("Use the player Take over or Hand back controls")
        return await coordinator.control(run_id, command, **body)

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
        if not isinstance(body.get("description"), str) or not body["description"].strip():
            raise ValueError("Describe what happened before flagging")
        async with coordinator.run_lock(run_id):
            await coordinator.cancel_tick(run_id)
            return await coordinator.flag(run_id, body["description"], body.get("expected", "Unknown; requires review"), body.get("players"))


    @app.post("/api/settings")
    async def settings(body: dict):
        current = store.require("settings", "main")
        if "concurrent_sessions" in body and (type(body["concurrent_sessions"]) is not int or body["concurrent_sessions"] != 1):
            raise ValueError("This release allows exactly one active instance; concurrency cannot be increased")
        for key in ("duration_seconds", "spend_usd"):
            if key in body and body[key] is not None and (not finite_number(body[key]) or body[key] <= 0):
                raise ValueError(f"{key} must be a finite positive number or unset")
        if "storage_reserve_bytes" in body and (type(body["storage_reserve_bytes"]) is not int or body["storage_reserve_bytes"] < 0):
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
        if controls.lock.locked():
            raise ValueError(SIGN_IN_WAIT)
        runs = store.list("runs")
        if any(r["state"] in {"Queued", "Starting", "Running", "Stopping"} for r in runs):
            raise ValueError("Pause or stop active bots and stop queued runs before changing AI sign-in.")
        # Goal recalculation runs inference while its run is Paused.
        if (any(r.get("goal_recalculation", {}).get("status") == "planning" for r in runs)
                or any(not task.done() for task in coordinator.tasks.values())):
            raise ValueError("Wait for goal recalculation and pending agent inference before changing AI sign-in.")

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

    @app.get("/api/settings/claude/status")
    async def claude_status():
        return await ai.validate(CLAUDE_MODEL)

    @app.post("/api/settings/claude/token")
    async def claude_token_save(body: dict):
        token = body.get("token")
        token = token.strip() if isinstance(token, str) else ""
        if not (token.startswith("sk-ant-oat") and 20 <= len(token) <= 512 and not any(c.isspace() for c in token)):
            raise ValueError("Paste the token printed by `claude setup-token`.")
        async with coordinator.lock:
            require_ai_idle()
            await asyncio.to_thread(ai.claude_backend.save_token, token)
            return await ai.validate(CLAUDE_MODEL)

    @app.delete("/api/settings/claude/token")
    async def claude_token_remove():
        async with coordinator.lock:
            require_ai_idle()
            await asyncio.to_thread(ai.claude_backend.clear_token)
            return await ai.validate(CLAUDE_MODEL)

    @app.post("/api/builds")
    async def add_build(body: dict):
        if not isinstance(body.get("manifest_path"), str) or not Path(body["manifest_path"]).is_absolute():
            raise ValueError("Select an absolute manifest path")
        try:
            record = build_record(body["manifest_path"], body.get("name"), body.get("id"))
        except (OSError, KeyError) as exc:
            raise ValueError(f"Build manifest is unreadable or incomplete: {exc}") from None
        record.pop("project_id", None)  # Membership comes only from the request, never the manifest.
        return Projects(store).register_build(record, body.get("project_id"))

    @app.get("/api/builds/{build_id}/catalog")
    async def catalog(build_id: str):
        build = store.require("builds", build_id)
        return await asyncio.to_thread(lambda: Repository(build).catalog(build))

    @app.post("/api/workers")
    async def add_worker(body: dict):
        worker_id = body.get("id") or uid("worker_")
        if not isinstance(worker_id, str) or not isinstance(body.get("url"), str):
            raise ValueError("Worker requires a text ID and service URL")
        if any(key in body and not isinstance(body[key], str) for key in ("name", "desktop_url", "host_address", "token")) or type(body.get("enabled", True)) is not bool:
            raise ValueError("Worker name, URLs, host address and token must be text; enabled must be true or false")
        url = urlparse(body["url"])
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            raise ValueError("Worker requires an HTTP(S) private service URL without embedded credentials")
        existing = store.get("workers", worker_id)
        if existing:
            require_unreferenced(worker_id)  # Same ownership rule as deletion.
        endpoint = lambda parsed: (parsed.scheme, parsed.hostname, parsed.port or {"http": 80, "https": 443}.get(parsed.scheme))
        if not body.get("token") and (not existing or endpoint(urlparse(existing.get("url", ""))) != endpoint(url)):
            # Never send a stored bearer token to a different scheme/host/port.
            raise ValueError("Enter the worker service token for a new or moved worker URL")
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
        require_unleased_worker(worker_id, "Finish the active worker lease before Steam account setup")
        return await coordinator.client(worker_id).steam_setup_start()

    @app.post("/api/workers/{worker_id}/steam/finish")
    async def steam_finish(worker_id: str):
        require_unleased_worker(worker_id, "Finish the active worker lease before closing Steam account setup")
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
        if not isinstance(body.get("account_id"), str) or not isinstance(body.get("build_id"), str):
            raise ValueError("Steam verification requires account_id and build_id")
        account = store.require("accounts", body["account_id"])
        build = store.require("builds", body["build_id"])
        if account["worker_id"] != worker_id or account["app_id"] != build.get("steam_app_id"):
            raise ValueError("Account desktop and actual game App ID must match the selected build")
        # Verification launches the game, so it owns the global slot like an instance.
        async with coordinator.lock:
            coordinator.require_instance_slot()
            require_unleased_worker(worker_id, "Steam verification needs an unleased worker")
            result = await coordinator.client(worker_id).steam_verify(account["app_id"], account["steam_id"], build["command"])
        account.update(readiness="ready" if result.get("verified") else "human_sign_in_required", last_check=time.time(), verification=result)
        store.put("accounts", account)
        return result

    @app.post("/api/accounts")
    async def add_account(body: dict):
        if not str(body.get("steam_id", "")).isdigit() or type(body.get("app_id")) is not int or body["app_id"] <= 0 or body["app_id"] == 480:
            raise ValueError("Specify the actual Steam ID64 and Agronauts App ID (not 480)")
        if not isinstance(body.get("name"), str) or not isinstance(body.get("worker_id"), str) or not isinstance(body.get("id") or "", str):
            raise ValueError("Account requires a text name and worker_id")
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

    @app.delete("/api/runs/{run_id}")
    async def delete_run(run_id: str):
        if operations_active():
            raise ValueError("Wait for the current operation to finish before deleting.")
        return await coordinator.delete_run(run_id)

    @app.delete("/api/projects/{project_id}")
    async def delete_project(project_id: str):
        if operations_active():
            raise ValueError("Wait for the current operation to finish before deleting.")
        return await coordinator.delete_project(project_id)

    @app.delete("/api/{kind}/{record_id}")
    async def delete_catalog(kind: str, record_id: str):
        if kind not in {"builds", "workers", "accounts"}:
            raise HTTPException(404)
        require_unreferenced(record_id)
        store.delete(kind, record_id)
        return {"deleted": record_id, "files_preserved": True}

    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    async def index():
        return FileResponse(static / "index.html")

    return app
