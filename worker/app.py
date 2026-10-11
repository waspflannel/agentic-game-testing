"""Authenticated private HTTP contract. Only the coordinator receives a service token."""
from __future__ import annotations

from contextlib import asynccontextmanager
import hmac
import os
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

from .models import Binding, BoundCommand, FixtureImport, Heartbeat, Launch, Pin, SaveCapture, SaveRestore, SteamVerify, Stop
from .runtime import Runtime
from .models import GameRequest
from .prepare_probe import PrepareProbe, prepare_probe


def create_app(runtime: Runtime | None = None, token: str | None = None) -> FastAPI:
    service = runtime or Runtime(Path(os.environ.get("ASHAD_WORKER_DATA", "/data")))
    if token is None:
        secret_file = Path(os.environ.get("ASHAD_WORKER_TOKEN_FILE", "/run/secrets/worker_token"))
        token = secret_file.read_text().strip() if secret_file.is_file() else os.environ.get("ASHAD_WORKER_TOKEN", "")

    async def authenticate(authorization: str | None = Header(default=None)) -> None:
        if not token or len(token) < 24:
            raise HTTPException(503, "Worker service token has not been provisioned")
        supplied = authorization.removeprefix("Bearer ") if authorization and authorization.startswith("Bearer ") else ""
        if not hmac.compare_digest(supplied, token):
            raise HTTPException(401, "Worker authentication required", headers={"WWW-Authenticate": "Bearer"})

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            await service.start()
            yield
        finally:
            await service.close()

    app = FastAPI(title="ASHAD desktop worker", version="1.0.0", lifespan=lifespan,
        dependencies=[Depends(authenticate)], docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtime = service

    @app.get("/health")
    @app.get("/status")
    async def status():
        return service.status()

    @app.post("/prepare/probe")
    async def probe_preparation(body: PrepareProbe):
        return await prepare_probe(service, body.build)

    @app.post("/bind")
    async def bind(body: Binding):
        return await service.bind(body)

    @app.post("/launch")
    async def launch(body: Launch):
        return await service.launch(body)

    @app.post("/heartbeat")
    async def heartbeat(body: Heartbeat):
        return await service.heartbeat(body)

    @app.post("/pause")
    async def pause(body: BoundCommand):
        return await service.pause(body)

    @app.post("/resume")
    async def resume(body: BoundCommand):
        return await service.resume(body)

    @app.post("/desktop/start")
    async def start_desktop(body: BoundCommand):
        return await service.start_desktop(body)

    @app.post("/desktop/finish")
    async def finish_desktop(body: BoundCommand):
        return await service.finish_desktop(body)

    @app.post("/stop")
    async def stop(body: Stop):
        return await service.stop(body)

    @app.post("/disconnect")
    async def disconnect(body: Stop):
        return await service.stop(body, disconnect=True)


    @app.post("/game")
    async def game(body: GameRequest):
        return await service.game_request(body)


    @app.get("/observation")
    async def observation(fresh: bool = False):
        return await service.capture() if fresh else service.latest_observation()

    @app.get("/frame.jpg")
    async def frame(observation_id: str | None = None):
        if not service._capture_allowed():
            raise HTTPException(409, "Game capture is unavailable")
        observation = service.observations.get(observation_id) if observation_id else service.latest_observation()
        if not observation:
            raise HTTPException(404, "Observation is no longer retained")
        return FileResponse(service.evidence_path(observation["frame_path"]), media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/telemetry")
    async def telemetry():
        return service.telemetry()

    @app.get("/live.mjpeg")
    async def live(run_id: str, player_id: str, epoch: str):
        command = BoundCommand(run_id=run_id, player_id=player_id, epoch=epoch)
        service.check_binding(command)
        if not service._capture_allowed():
            raise HTTPException(409, "Live view unavailable")
        if service.live_viewers >= 4:
            raise HTTPException(429, "Close another live view before opening more")
        return StreamingResponse(service.live_frames(command),
            media_type="multipart/x-mixed-replace; boundary=frame",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.post("/pin")
    async def pin(body: Pin):
        return await service.pin(body)

    @app.get("/evidence")
    async def evidence(path: str = Query(...), run_id: str | None = None):
        return FileResponse(service.evidence_path(path, run_id=run_id))

    @app.get("/evidence/index")
    async def evidence_index(run_id: str | None = None):
        return service.evidence_index(run_id)

    @app.post("/fixtures/export")
    async def export_fixture(body: dict):
        return service.export_fixture(body)

    @app.post("/fixtures/import")
    async def import_fixture(body: FixtureImport):
        return service.import_fixture(body)

    @app.post("/saves/capture")
    async def capture_save(body: SaveCapture):
        return await service.capture_save(body)

    @app.post("/saves/restore")
    async def restore_save(body: SaveRestore):
        return service.restore_save(body)

    @app.post("/setup/steam/start")
    async def start_steam_setup():
        return await service.start_steam_setup()

    @app.post("/setup/steam/finish")
    async def finish_steam_setup():
        return await service.finish_steam_setup()

    @app.post("/setup/steam/verify")
    async def verify_steam(body: SteamVerify):
        return await service.verify_steam(body)

    return app
