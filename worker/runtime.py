from __future__ import annotations

import asyncio
import anyio
import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import secrets
import time
import zipfile
from typing import Any

from fastapi import HTTPException

from .desktop import SEGMENT_SECONDS, Desktop
from .game_bridge import GameBridge
from .independent_watchdog import kill_run_game, revoke_desktop
from .models import GameAction
from .models import Binding, BoundCommand, FixtureImport, Heartbeat, Launch, Pin, SaveCapture, SaveRestore, SteamVerify, Stop, safe_id, timestamp, utc_now


CONTRACT_VERSION = "ashad-worker/1"
MAX_SAVE_BYTES = 100 * 1024 * 1024
EVIDENCE_ROOTS = {"frames", "pinned", "rolling", "game.log", "actions.jsonl", "events.jsonl", "observations.jsonl", "capture.log", "telemetry.json", "fixture-manifest.json", "binding.json", "build.json"}


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def executable_allowed(executable: Path) -> bool:
    roots = os.environ.get("ASHAD_EXECUTABLE_ROOTS", "/game:/opt/runtime").split(os.pathsep)
    return any(executable.resolve().is_relative_to(Path(root).resolve()) for root in roots)


def game_environment(env: dict) -> dict:
    return {key: value for key, value in env.items() if key not in {
        "ASHAD_WORKER_TOKEN", "ASHAD_WORKER_TOKEN_FILE", "ASHAD_VNC_PASSWORD", "ASHAD_VNC_PASSWORD_FILE", "OPENAI_API_KEY"}}


def _zip_game_dir(game_dir: Path) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in game_dir.rglob("*"):
            if path.is_file() and not path.is_symlink() and path.name != "ashad-fixture.json":
                archive.write(path, str(path.relative_to(game_dir)))
        empty = not archive.namelist()
    return b"" if empty else stream.getvalue()


class Runtime:
    def __init__(self, root: Path, desktop: Desktop | None = None, worker_id: str | None = None):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.worker_id = worker_id or os.environ.get("ASHAD_WORKER_ID", "worker-1")
        self.desktop = desktop or Desktop(os.environ.get("DISPLAY", ":99"), int(os.environ.get("ASHAD_WIDTH", "1280")), int(os.environ.get("ASHAD_HEIGHT", "720")))
        self.binding: Binding | None = None
        self.lifecycle = "unbound"
        self.game: asyncio.subprocess.Process | None = None
        self.launched_monotonic: float | None = None
        self.video_process: asyncio.subprocess.Process | None = None
        self.setup_processes: list[asyncio.subprocess.Process] = []
        self.desktop_session: str | None = None
        self.human_control = False
        self.steam: asyncio.subprocess.Process | None = None
        self.held_keys: set[str] = set()
        self.held_buttons: set[int] = set()
        self.last_heartbeat = time.monotonic()
        self.last_heartbeat_utc = time.time()
        self.held_input_expires_at: float | None = None
        self.heartbeat_timeout = 15.0
        self.observation_max_age = 12.0
        self.observations: dict[str, dict] = {}
        self.sequence = 0
        self.interrupt = asyncio.Event()
        self.state_lock = asyncio.Lock()
        self.capture_lock = asyncio.Lock()
        self.live_viewers = 0
        self.tasks: list[asyncio.Task] = []
        self.last_capture_at: float | None = None
        self.capture_error: str | None = None
        self.infrastructure_error: str | None = None
        self.deadline: str | float | None = None
        self.run_deadline: str | float | None = None
        self.pins: dict[str, dict] = {}
        self.steam_setup = False
        self.closed = False
        self.cpu_sample: tuple[float, int] | None = None
        self.resource_sample: dict = {}
        self.resource_sample_at = 0.0
        self.termination_result: dict | None = None
        self.build_record: dict | None = None
        self.game_bridge = GameBridge()
        self.game_observations: dict[str, dict] = {}
        self.game_control_epoch = secrets.token_hex(16)
        self._recover()

    def _recover(self) -> None:
        state = self.root / "worker-state.json"
        if not state.exists():
            return
        try:
            data = json.loads(state.read_text(encoding="utf-8"))
            # The viewer must not survive a service restart and share a later run's desktop.
            revoke_desktop(data)
            if data.get("binding"):
                self.binding = Binding.model_validate(data["binding"])
                # A process-only service restart must not orphan a game that can later share a desktop.
                pid = data.get("game_pid")
                if pid and os.name != "nt":
                    with contextlib.suppress(OSError, ValueError):
                        kill_run_game(int(pid), self.binding.run_id)
                # A service restart cannot silently reauthorize a previous controller.
                self.lifecycle = "ended"
                self.infrastructure_error = "worker_restarted; game process and inputs require reconciliation"
                self.deadline = data.get("investigation_deadline")
                self.run_deadline = data.get("run_deadline")
                self._event("worker_restarted", {"execution_outcome": "unknown; fresh binding required"})
        except (ValueError, OSError, KeyError):
            self.infrastructure_error = "worker state corrupt; manual reconciliation required"

    @property
    def run_dir(self) -> Path:
        if self.binding is None:
            raise HTTPException(409, "Worker has no run binding")
        return self.root / "runs" / self.binding.run_id

    @property
    def game_dir(self) -> Path:
        return self.run_dir / "game"

    @property
    def game_running(self) -> bool:
        return self.game is not None and self.game.returncode is None

    def _persist(self) -> None:
        atomic_json(self.root / "worker-state.json", {
            "contract_version": CONTRACT_VERSION, "worker_id": self.worker_id,
            "binding": self.binding.model_dump() if self.binding else None,
            "lifecycle": self.lifecycle, "game_pid": self.game.pid if self.game_running else None,
            "held_keys": sorted(self.held_keys), "held_buttons": sorted(self.held_buttons),
            "controller_last_seen": self.last_heartbeat_utc, "heartbeat_timeout_seconds": self.heartbeat_timeout,
            "held_input_expires_at": self.held_input_expires_at,
            "human_control": self.human_control, "desktop_session": self.desktop_session,
            "desktop_pids": [process.pid for process in self.setup_processes if process.returncode is None],
            "investigation_deadline": self.deadline, "run_deadline": self.run_deadline,
            "updated_at": utc_now(), "termination_result": self.termination_result,
        })

    def _append(self, filename: str, record: dict) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        with (self.run_dir / filename).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _event(self, kind: str, data: dict | None = None) -> None:
        if self.binding:
            self._append("events.jsonl", {"kind": kind, "at": utc_now(), "worker_id": self.worker_id, **self.binding.model_dump(exclude={"settings"}), "data": data or {}})


    async def start(self) -> None:
        await self.release_inputs("worker startup")
        self.tasks = [asyncio.create_task(self._watchdog()), asyncio.create_task(self._capture_loop())]

    async def close(self) -> None:
        self.closed = True
        self.interrupt.set()
        # Capture and watchdog tasks must stop before their processes are torn down.
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        async with contextlib.AsyncExitStack() as cleanup:
            cleanup.push_async_callback(self.finish_steam_setup)
            cleanup.push_async_callback(self._stop_video)
            if self.game_running:
                cleanup.push_async_callback(self._terminate_process, self.game)
            cleanup.push_async_callback(self._finish_human_control, "worker shutdown")
            await self.release_inputs("worker shutdown")

    def check_binding(self, command: BoundCommand) -> None:
        if not self.binding or any(getattr(command, name) != getattr(self.binding, name) for name in ("run_id", "player_id", "epoch")):
            raise HTTPException(409, "Run, player, or controller epoch does not own this worker")

    async def bind(self, binding: Binding) -> dict:
        async with self.state_lock:
            if self.binding and self.binding.model_dump() == binding.model_dump() and self.lifecycle != "ended":
                return self.status()
            if self.game_running or self.lifecycle not in ("unbound", "ended"):
                raise HTTPException(409, "Worker is already leased; teardown must be confirmed before rebinding")
            if self.steam_setup:
                raise HTTPException(409, "Finish the human Steam setup session before binding a run")
            if binding.transport == "steam" and (not binding.app_id or binding.app_id == 480):
                raise HTTPException(422, "Steam requires the selected real game App ID; App ID 480 is not accepted")
            if binding.transport == "steam" and not binding.identity.isdigit():
                raise HTTPException(422, "Steam identity must be a Steam ID supplied by the account pool")
            await self._finish_human_control("new binding")
            await self.release_inputs("new binding")
            self.binding = binding
            self.lifecycle = "bound"
            self.interrupt = asyncio.Event()
            self.observations, self.pins = {}, {}
            self.sequence = 0
            self.last_capture_at = None
            self.capture_error = self.infrastructure_error = None
            self.deadline = self.run_deadline = None
            self.termination_result = None
            self.last_heartbeat = time.monotonic()
            self.last_heartbeat_utc = time.time()
            self.heartbeat_timeout = max(5, min(float(binding.settings.get("heartbeat_timeout_seconds", 15)), 120))
            self.observation_max_age = max(1, min(float(binding.settings.get("observation_max_age_seconds", 12)), 60))
            self.desktop.capture_quality = max(20, min(int(binding.settings.get("quality", 75)), 100))
            self.game_dir.mkdir(parents=True, exist_ok=True)
            atomic_json(self.run_dir / "binding.json", binding.model_dump())
            self._persist()
            self._event("bound", {"identity": binding.identity, "transport": binding.transport})
            return self.status()

    async def launch(self, request: Launch) -> dict:
        async with self.state_lock:
            self.check_binding(request)
            if self.game_running:
                return self.status()
            if self.lifecycle not in ("bound", "disconnected"):
                raise HTTPException(409, "Worker must be bound before launch")
            if not request.command or not Path(request.command[0]).is_absolute():
                raise HTTPException(422, "Launch command requires an absolute executable path")
            executable = Path(request.command[0]).resolve()
            if not executable_allowed(executable):
                raise HTTPException(422, "Executable is outside the configured read-only build/runtime roots")
            if not executable.is_file():
                raise HTTPException(422, "Game executable is unavailable on this worker")
            self.build_record = {"command": request.command, "executable_sha256": await asyncio.to_thread(sha256_file, executable),
                "artifact_sha256": None, "source_revision": None, "verified_at": utc_now()}
            self.build_record["engine_sha256"] = self.build_record["executable_sha256"]
            steam_library = executable.parent / "libsteam_api.so"
            self.build_record["steam_api_sha256"] = await asyncio.to_thread(sha256_file, steam_library) if steam_library.is_file() else None
            if "--main-pack" in request.command:
                position = request.command.index("--main-pack") + 1
                pack = Path(request.command[position]).resolve() if position < len(request.command) else None
                if pack is None or not pack.is_file():
                    raise HTTPException(422, "Pinned game pack is missing")
                if not executable_allowed(pack):
                    raise HTTPException(422, "Game pack is outside the configured read-only build/runtime roots")
                self.build_record["artifact_sha256"] = await asyncio.to_thread(sha256_file, pack)
                manifest_path = pack.parent / "manifest.json"
                if manifest_path.is_file():
                    with contextlib.suppress(ValueError, OSError):
                        manifest = json.loads(manifest_path.read_text())
                        self.build_record["source_revision"] = manifest.get("source_revision", manifest.get("source_commit"))
            atomic_json(self.run_dir / "build.json", self.build_record)
            if self.binding.transport == "steam" and self.steam is None:
                await self._start_steam()
            env = {**os.environ, **request.env,
                "DISPLAY": self.desktop.display, "ASHAD_RUN_ID": self.binding.run_id,
                "ASHAD_PLAYER_ID": self.binding.player_id, "ASHAD_IDENTITY": self.binding.identity,
                "ASHAD_TRANSPORT": self.binding.transport, "ASHAD_DATA_DIR": str(self.game_dir),
                "ASHAD_TELEMETRY_PATH": str(self.run_dir / "telemetry.json"),
                "XDG_DATA_HOME": str(self.run_dir / "xdg-data"), "XDG_CONFIG_HOME": str(self.run_dir / "xdg-config"),
            }
            if self.binding.app_id:
                env["ASHAD_STEAM_APP_ID"] = env["SteamAppId"] = env["SteamGameId"] = str(self.binding.app_id)
            self.game_bridge = GameBridge()
            self.game_control_epoch = secrets.token_hex(16)
            self.game_observations.clear()
            env.update(ASHAD_GAME_BRIDGE_TOKEN=self.game_bridge.token,
                       ASHAD_GAME_BRIDGE_PORT=str(self.game_bridge.port))
            env = game_environment(env)
            telemetry = self.run_dir / "telemetry.json"
            if telemetry.exists():
                telemetry.unlink()  # A previous join cannot attest readiness of a new process.
            log = (self.run_dir / "game.log").open("ab")
            try:
                self.game = await asyncio.create_subprocess_exec(*request.command, cwd=str(executable.parent), env=env,
                    stdout=log, stderr=log, start_new_session=(os.name != "nt"))
            except OSError as exc:
                self.infrastructure_error = f"Game launch failed: {exc}"
                self._event("launch_failed", {"error": self.infrastructure_error})
                raise HTTPException(503, self.infrastructure_error) from exc
            finally:
                log.close()
            self.lifecycle = "running"
            self.launched_monotonic = time.monotonic()
            self.interrupt.clear()
            self.observations.clear()
            self.last_capture_at = None
            self.capture_error = self.infrastructure_error = None
            self.termination_result = None
            self.last_heartbeat = time.monotonic()
            self.last_heartbeat_utc = time.time()
            self._persist()
            self._event("process_launched", {"pid": self.game.pid, "command": request.command})
            return self.status()

    def telemetry(self) -> dict:
        if not self.binding:
            return {"available": False, "reason": "unbound"}
        path = self.run_dir / "telemetry.json"
        if not path.exists():
            return {"available": False, "reason": "telemetry_not_yet_written"}
        try:
            if path.stat().st_size > 2_000_000:
                raise ValueError("telemetry exceeds 2MB limit")
            result = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(result, dict):
                raise ValueError("telemetry must be a JSON object")
            if result.get("run_id") != self.binding.run_id or result.get("player_id") != self.binding.player_id:
                raise ValueError("telemetry ownership mismatch")
            result["available"] = True
            result["age_seconds"] = max(0, time.time() - path.stat().st_mtime)
            result["worker_received_at"] = utc_now()
            return result
        except (OSError, ValueError, TypeError) as exc:
            return {"available": False, "reason": str(exc)}

    def steam_status(self) -> dict:
        status = {"setup_active": self.steam_setup, "client_running": self.steam is not None and self.steam.returncode is None,
            "authenticated": False, "identity_verified": False, "app_id_verified": False,
            "requires_human_setup": True, "reason": "Identity and App ID require fresh read-only game telemetry"}
        if self.binding and self.binding.transport == "steam":
            telem = self.telemetry()
            current = telem.get("available") and telem.get("age_seconds", 999) < 5
            status.update(authenticated=bool(current and telem.get("authenticated")),
                identity_verified=bool(current and str(telem.get("steam_id")) == self.binding.identity),
                app_id_verified=bool(current and telem.get("app_id") == self.binding.app_id and self.binding.app_id != 480),
                license_verified=bool(current and telem.get("app_subscribed") is True),
                actual_transport=telem.get("transport"), steam_id=telem.get("steam_id"), app_id=telem.get("app_id"),
                transport_verified=bool(current and telem.get("transport") == "steam" and not telem.get("offline_fallback")))
            # Authenticated main-menu capture precedes ordinary UI hosting/joining. Actual peer transport
            # is a separate readiness predicate; requiring it here would prevent agents from creating it.
            valid = all(status[key] for key in ("authenticated", "identity_verified", "app_id_verified", "license_verified")) and status["actual_transport"] in ("steam", "none", None) and not telem.get("offline_fallback")
            status["requires_human_setup"] = not valid
            status["reason"] = "Verified by current game telemetry" if valid else "Steam authentication, expected identity, or real App ID is not verified"
        else:
            record = self.root / "private" / "steam-verification.json"
            if record.is_file():
                with contextlib.suppress(ValueError, OSError):
                    verified = json.loads(record.read_text())
                    status["last_verification"] = verified
                    status["steam_id"] = verified.get("steam_id")
                    status["app_id"] = verified.get("app_id")
                    status["reason"] = "Stored account probe; repeat Verify before admission if authentication may have changed"
        return status

    def _capture_allowed(self) -> bool:
        if not self.game_running or self.steam_setup or self.lifecycle not in ("running", "paused"):
            return False
        if self.binding.transport == "steam" and self.steam_status()["requires_human_setup"]:
            return False
        return True

    async def capture(self) -> dict:
        async with self.capture_lock:
            if not self._capture_allowed():
                raise HTTPException(409, "Capture unavailable; no running game or Steam authentication requires human setup")
            if shutil.disk_usage(self.root).free < 100 * 1024 * 1024:
                self.infrastructure_error = "Evidence disk has less than 100 MiB free"
                raise HTTPException(507, self.infrastructure_error)
            self.sequence += 1
            observation_id = f"{self.binding.epoch}-{self.sequence}"
            frames = self.run_dir / "frames"
            frames.mkdir(exist_ok=True)
            path = frames / f"{observation_id}.jpg"
            started = time.time()
            try:
                # Capture only after the real game owns focus; a Steam login/setup desktop
                # must never become an agent observation during process startup.
                await self.desktop.focus(self.game.pid)
                await self.desktop.capture(path)
            except (OSError, RuntimeError, asyncio.TimeoutError) as exc:
                self.capture_error = str(exc)
                raise HTTPException(503, f"Capture failed: {exc}") from exc
            self.last_capture_at = time.time()
            self.capture_error = None
            observation = {"contract_version": CONTRACT_VERSION, "id": observation_id, "observation_id": observation_id,
                "run_id": self.binding.run_id, "player_id": self.binding.player_id, "worker_id": self.worker_id,
                "epoch": self.binding.epoch, "sequence": self.sequence, "captured_at": utc_now(), "timestamp": self.last_capture_at,
                "width": self.desktop.width, "height": self.desktop.height, "frame_path": str(path.relative_to(self.run_dir)).replace("\\", "/"),
                "clock_uncertainty_ms": round((self.last_capture_at - started) * 1000), "freshness_seconds": 0.0}
            self.observations[observation_id] = observation
            self._append("observations.jsonl", observation)
            cutoff = time.time() - max(60, self.observation_max_age * 3)
            for old_id, old in list(self.observations.items()):
                if old["timestamp"] < cutoff:
                    (self.run_dir / old["frame_path"]).unlink(missing_ok=True)
                    del self.observations[old_id]
            return observation

    def latest_observation(self) -> dict:
        if not self._capture_allowed() or not self.observations:
            raise HTTPException(409, "No eligible game observation is available")
        result = dict(next(reversed(self.observations.values())))
        result["freshness_seconds"] = max(0, time.time() - result["timestamp"])
        if result["freshness_seconds"] > self.observation_max_age:
            raise HTTPException(503, "Latest capture is stale")
        return result

    async def live_frames(self, command: BoundCommand):
        self.check_binding(command)
        binding, game = self.binding, self.game
        def can_stream():
            return not self.closed and self.binding is binding and self.game is game and self._capture_allowed()
        if not can_stream() or self.live_viewers >= 4:
            return
        # ponytail: one encoder per viewer, capped at four; share an encoder if audience grows.
        self.live_viewers += 1
        process = None
        try:
            await self.desktop.focus(game.pid)
            if not can_stream():
                return
            process = await self.desktop.live_video()
            while can_stream():
                chunk = await asyncio.wait_for(process.stdout.read(65536), 5)
                if not chunk or not can_stream():
                    break
                yield chunk
        except (OSError, RuntimeError, asyncio.TimeoutError):
            return  # Viewing failures do not alter gameplay or agent observations.
        finally:
            self.live_viewers -= 1
            if process:
                with anyio.CancelScope(shield=True):
                    if process.returncode is None:
                        with contextlib.suppress(ProcessLookupError):
                            process.kill()
                    await process.wait()

    def bridge_error(self, exc) -> HTTPException:
        starting = (isinstance(exc, ConnectionRefusedError) and self.launched_monotonic is not None
                    and time.monotonic() - self.launched_monotonic < 15)
        return HTTPException(503, {
            "code": "game_bridge_starting" if starting else "game_bridge_unavailable",
            "message": ("Game bridge listener is starting; retry during the 15-second startup window." if starting
                        else f"Game bridge unavailable: {type(exc).__name__}: {exc}"),
        })

    async def heartbeat(self, request: Heartbeat) -> dict:
        self.check_binding(request)
        # Deadlines are explicit coordinator state, not extended by arrival of a heartbeat.
        self.last_heartbeat = time.monotonic()
        self.last_heartbeat_utc = time.time()
        self.deadline = request.investigation_deadline
        self.run_deadline = request.run_deadline
        self._persist()  # Preserve enforced deadlines even if the game bridge fails.
        bridge_status = "not_running"
        if self.game_running:
            try:
                await self.game_bridge.request("heartbeat", {"epoch": self.game_control_epoch,
                    "enabled": self.lifecycle == "running" and not self.human_control,
                    "lease_seconds": min(self.heartbeat_timeout, 10)})
                bridge_status = "ready"
            except (OSError, ValueError, asyncio.TimeoutError) as exc:
                error = self.bridge_error(exc)
                if error.detail["code"] != "game_bridge_starting":
                    raise error from exc
                bridge_status = "starting"  # The next regular heartbeat retries; no game input is replayed.
        return {"at": utc_now(), "lifecycle": self.lifecycle, "investigation_deadline": self.deadline,
                "run_deadline": self.run_deadline, "game_bridge": bridge_status}

    async def release_inputs(self, reason: str) -> None:
        if self.game_running and self.binding:
            self.game_control_epoch = secrets.token_hex(16)
            self.game_observations.clear()
            with contextlib.suppress(OSError, ValueError, asyncio.TimeoutError):
                await self.game_bridge.request("hold", {"reason": reason})
        held = {"keys": sorted(self.held_keys), "buttons": sorted(self.held_buttons)}
        await self.desktop.release_all(self.held_keys, self.held_buttons)
        self.held_keys.clear()
        self.held_buttons.clear()
        self.held_input_expires_at = None
        self._persist()
        if held["keys"] or held["buttons"]:
            self._event("inputs_released", {"reason": reason, **held})

    async def pause(self, request: BoundCommand) -> dict:
        async with self.state_lock:
            self.check_binding(request)
            self.interrupt.set()
            if self.lifecycle == "running":
                self.lifecycle = "paused"
            await self._finish_human_control("pause")
            await self.release_inputs("pause")
            self._event("paused", {"command_id": request.command_id, "world_may_continue": self.game_running})
            return self.status()

    async def resume(self, request: BoundCommand) -> dict:
        async with self.state_lock:
            self.check_binding(request)
            await self._finish_human_control("resume bots")
            if self.lifecycle == "paused" and self.game_running:
                if self.infrastructure_error not in (None, "controller_heartbeat_lost"):
                    raise HTTPException(409, "Resolve the worker infrastructure failure before resuming")
                # Only the recoverable heartbeat fault is cleared, after ownership/process checks.
                self.infrastructure_error = None
                self.game_control_epoch = secrets.token_hex(16)
                self.game_observations.clear()
                self.observations.clear()  # A new controller observation is mandatory after resume.
                self.interrupt.clear()
                self.lifecycle = "running"
                self.last_heartbeat = time.monotonic()
                self.last_heartbeat_utc = time.time()
                self.deadline = None
                self._persist()
                self._event("resumed", {"command_id": request.command_id})
            return self.status()

    async def game_request(self, request) -> dict:
        self.check_binding(request)
        if not self.game_running:
            raise HTTPException(409, "Structured game interface is not enabled")
        args = dict(request.arguments)
        operation = request.operation
        for field in ("target_id", "action_id", "generation"):
            if field in args and (not isinstance(args[field], str) or len(args[field]) > 200):
                raise HTTPException(422, "Invalid structured query identifier")
        if operation == "read_changes" and (not isinstance(args.get("after", 0), int) or args.get("after", 0) < 0):
            raise HTTPException(422, "Invalid change cursor")
        if operation == "perform_action":
            try:
                args = GameAction.model_validate(args).model_dump(mode="json", exclude_none=True)
            except ValueError as exc:
                raise HTTPException(422, "Invalid structured action parameters") from exc
            if self.lifecycle != "running" or self.interrupt.is_set() or self.human_control:
                raise HTTPException(409, "Game inputs are held")
            if time.monotonic() - self.last_heartbeat > self.heartbeat_timeout:
                raise HTTPException(409, "Controller heartbeat expired")
            observed = self.game_observations.get(args.get("observation_id"))
            if not observed or time.time() - observed["at"] > self.observation_max_age:
                raise HTTPException(409, "Fresh structured observation required")
            args["observation_id"] = observed["game_id"]
            if not isinstance(args.get("expires_at"), (int, float)) or not time.time() < args["expires_at"] <= time.time() + 35:
                raise HTTPException(409, "Invalid action expiry")
        captured_epoch = self.game_control_epoch
        args["epoch"] = captured_epoch
        try:
            result = await self.game_bridge.request(operation, args)
        except (OSError, ValueError, asyncio.TimeoutError) as exc:
            if operation == "perform_action":
                result = {"status": "unknown", "action_id": args.get("action_id"),
                          "reason": "Bridge acknowledgement lost; inspect status before another action"}
            else:
                raise self.bridge_error(exc) from exc
        if operation == "observe" and result.get("observation_id"):
            if captured_epoch != self.game_control_epoch:
                raise HTTPException(409, "Observation invalidated by pause or rebind")
            if result.get("run_id") != self.binding.run_id or result.get("player_id") != self.binding.player_id:
                raise HTTPException(409, "Game observation belongs to a different binding")
            game_id = result["observation_id"]
            result["observation_id"] = game_id + ":" + captured_epoch
            self.game_observations[result["observation_id"]] = {"at": time.time(), "game_id": game_id}
            self.game_observations = dict(list(self.game_observations.items())[-32:])
        if operation in {"perform_action", "cancel_action"}:
            self._event("structured_action", {"operation": operation, "request": args, "result": result})
        return result






    async def _terminate_process(self, process: asyncio.subprocess.Process, hard: bool = False) -> None:
        if process.returncode is not None:
            return
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL if hard else signal.SIGTERM)
            elif hard:
                process.kill()
            else:
                process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), 0.5 if hard else 3)
        except asyncio.TimeoutError:
            if os.name != "nt":
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            await asyncio.wait_for(process.wait(), 2)

    async def stop(self, request: Stop, disconnect: bool = False) -> dict:
        async with self.state_lock:
            self.check_binding(request)
            await self._finish_human_control("disconnect" if disconnect else request.reason)
            if self.lifecycle == "ended" and not self.game_running:
                if self.termination_result is None:
                    self.termination_result = {"confirmed": True, "graceful_attempted": False,
                        "save_status": "unverified", "reason": "worker_restart_reconciled", "at": utc_now()}
                    self._persist()
                return self.status()
            self.interrupt.set()
            self.lifecycle = "stopping"
            await self.release_inputs(request.reason)
            graceful_attempted = False
            if self.game_running:
                if not request.terminate:
                    # Window close is ordinary desktop input. Save support belongs to the game's close handler.
                    graceful_attempted = True
                    with contextlib.suppress(RuntimeError, OSError, asyncio.TimeoutError):
                        await self.desktop.focus(self.game.pid)
                        await self.desktop.input("key", "--clearmodifiers", "alt+F4")
                        await asyncio.wait_for(self.game.wait(), 5)
                if self.game_running:
                    await self._terminate_process(self.game, hard=request.terminate)
            await self._stop_video()
            for pin in self.pins.values():
                await self._finish_pin(pin, final=True)
            self.lifecycle = "disconnected" if disconnect else "ended"
            self.termination_result = {"confirmed": not self.game_running, "graceful_attempted": graceful_attempted,
                "save_status": "unverified", "reason": request.reason, "at": utc_now()}
            self._persist()
            self._event("disconnected" if disconnect else "ended", self.termination_result)
            return self.status()

    async def _stop_video(self) -> None:
        if self.video_process and self.video_process.returncode is None:
            self.video_process.terminate()
            try:
                await asyncio.wait_for(self.video_process.wait(), 3)
            except asyncio.TimeoutError:
                self.video_process.kill()
                await self.video_process.wait()
        self.video_process = None

    async def pin(self, request: Pin) -> dict:
        self.check_binding(request)
        if request.incident_id in self.pins:
            return self.pins[request.incident_id]
        now = time.time()
        pin = {"incident_id": request.incident_id, "requested_at": utc_now(), "from_timestamp": now - request.before_seconds,
            "to_timestamp": now + request.after_seconds, "segments": [], "frames": [], "status": "collecting", "limitations": []}
        self.pins[request.incident_id] = pin
        directory = self.run_dir / "pinned" / request.incident_id
        directory.mkdir(parents=True, exist_ok=True)
        for obs in self.observations.values():
            if obs["timestamp"] >= pin["from_timestamp"]:
                source = self.run_dir / obs["frame_path"]
                if source.exists():
                    shutil.copy2(source, directory / source.name)
                    pin["frames"].append(str((directory / source.name).relative_to(self.run_dir)).replace("\\", "/"))
        await self._finish_pin(pin)
        self._event("evidence_pin", {"incident_id": request.incident_id, "before_seconds": request.before_seconds, "after_seconds": request.after_seconds})
        return pin

    async def _finish_pin(self, pin: dict, final: bool = False) -> None:
        directory = self.run_dir / "pinned" / pin["incident_id"]
        directory.mkdir(parents=True, exist_ok=True)
        segments = sorted((self.run_dir / "rolling").glob("*.ts"))
        # The active last segment must close before copying. MPEG-TS stays readable after abrupt capture failure.
        for source in segments if final else segments[:-1]:
            end_time = source.stat().st_mtime
            if end_time >= pin["from_timestamp"] and end_time - SEGMENT_SECONDS <= pin["to_timestamp"]:
                relative = str((directory / source.name).relative_to(self.run_dir)).replace("\\", "/")
                if relative not in pin["segments"]:
                    shutil.copy2(source, directory / source.name)
                    pin["segments"].append(relative)
        if time.time() >= pin["to_timestamp"] + 6 or final:
            pin["status"] = "complete" if pin["segments"] else "missing_video"
            if not pin["segments"]:
                pin["limitations"] = ["No eligible rolling video segments were available"]
            if final and time.time() < pin["to_timestamp"]:
                pin["limitations"].append("Post-event capture ended before the requested window completed")
            if pin["segments"] and not pin.get("clip"):
                listing = directory / "segments.txt"
                listing.write_text("\n".join("file '" + Path(segment).name + "'" for segment in sorted(pin["segments"])) + "\n")
                clip = directory / "clip.mp4"
                try:
                    await self.desktop.command("ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "1", "-i", str(listing), "-c", "copy", "-movflags", "+faststart", str(clip), timeout=5)
                    pin["clip"] = str(clip.relative_to(self.run_dir)).replace("\\", "/")
                except (OSError, RuntimeError, asyncio.TimeoutError) as exc:
                    clip.unlink(missing_ok=True)
                    pin["limitations"].append(f"MP4 assembly failed; source video segments retained: {type(exc).__name__}")
        atomic_json(directory / "manifest.json", pin)

    async def _capture_loop(self) -> None:
        while not self.closed:
            if self._capture_allowed():
                try:
                    await self.capture()
                    if self.video_process is None or self.video_process.returncode is not None:
                        if self.video_process is not None:
                            self._event("video_capture_restarted", {"returncode": self.video_process.returncode})
                        fps = max(1, min(int(self.binding.settings.get("fps", self.binding.settings.get("capture_fps", 8))), 30))
                        self.video_process = await self.desktop.video(self.run_dir / "rolling", fps=fps)
                    for pin in self.pins.values():
                        if pin["status"] == "collecting":
                            await self._finish_pin(pin)
                    rolling_seconds = max(60, min(int(self.binding.settings.get("rolling_seconds", 120)), 3600))
                    cutoff = time.time() - rolling_seconds
                    protected = min((p["from_timestamp"] for p in self.pins.values() if p["status"] == "collecting"), default=time.time())
                    for path in (self.run_dir / "rolling").glob("*.ts"):
                        if path.stat().st_mtime < min(cutoff, protected):
                            path.unlink()
                except (HTTPException, OSError, RuntimeError, ValueError) as exc:
                    self.capture_error = str(exc)
            elif self.video_process:
                await self._stop_video()
            await asyncio.sleep(1)

    async def _watchdog(self) -> None:
        while not self.closed:
            try:
                await self.watchdog_tick()
            except Exception as exc:
                # Supervision must outlive a failed tick; an unsupervised run is paused, never left running.
                self.infrastructure_error = self.infrastructure_error or f"watchdog_tick_failed: {type(exc).__name__}"
                if self.lifecycle == "running":
                    self.interrupt.set()
                    self.lifecycle = "paused"
                    try:
                        await self.release_inputs("watchdog_tick_failed")
                        self._event("infrastructure_failure", {"reason": self.infrastructure_error})
                    except Exception:
                        pass  # Best effort; the paused lifecycle and recorded error already block input and resume.
            await asyncio.sleep(0.25)

    async def watchdog_tick(self) -> None:
        if not self.binding:
            return
        if self.held_input_expires_at and time.time() >= self.held_input_expires_at:
            await self.release_inputs("held input expiry")
        deadlines = [timestamp(value) for value in (self.deadline, self.run_deadline) if value is not None]
        if deadlines and time.time() >= min(deadlines) and self.lifecycle not in ("ended", "unbound"):
            await self.stop(Stop(**self.binding.model_dump(include={"run_id", "player_id", "epoch"}), terminate=True, reason="persisted_deadline_expired"))
            return
        if self.human_control:
            reason = None
            if time.monotonic() - self.last_heartbeat > self.heartbeat_timeout:
                reason = "controller_heartbeat_lost"
            elif not self.game_running:
                reason = "game_process_exited"
            elif any(process.returncode is not None for process in self.setup_processes):
                reason = "desktop_session_exited"
            if reason:
                async with self.state_lock:
                    await self._finish_human_control(reason)
                    if reason != "desktop_session_exited":
                        self.infrastructure_error = reason
                    self._event("human_control_lost", {"reason": reason})
        if self.lifecycle == "running":
            reason = None
            if time.monotonic() - self.last_heartbeat > self.heartbeat_timeout:
                reason = "controller_heartbeat_lost"
            elif (self.capture_error and (self.launched_monotonic is None or time.monotonic()-self.launched_monotonic > 10)) or (self.last_capture_at and time.time() - self.last_capture_at > self.observation_max_age):
                reason = "capture_unhealthy"
            elif self.game and not self.game_running:
                reason = "game_process_exited"
            if reason:
                self.infrastructure_error = reason
                self.interrupt.set()
                self.lifecycle = "paused"
                await self.release_inputs(reason)
                self._event("infrastructure_failure", {"reason": reason, "game_exit_code": self.game.returncode if self.game else None})

    def status(self) -> dict:
        disk = shutil.disk_usage(self.root)
        return {"contract_version": CONTRACT_VERSION, "worker_id": self.worker_id, "binding": self.binding.model_dump() if self.binding else None,
            "independent_desktop": True, "desktop_backend": "private-container-xvfb-xdotool", "build": self.build_record,
            "lifecycle": self.lifecycle, "game_running": self.game_running, "game_pid": self.game.pid if self.game_running else None,
            "human_control": self.human_control,
            "world_may_continue": self.game_running, "held_keys": sorted(self.held_keys), "held_buttons": sorted(self.held_buttons),
            "capture": {"healthy": self._capture_allowed() and self.last_capture_at is not None and time.time() - self.last_capture_at < self.observation_max_age and not self.capture_error,
                "last_at": self.last_capture_at, "error": self.capture_error, "video_running": bool(self.video_process and self.video_process.returncode is None)},
            "infrastructure_error": self.infrastructure_error, "investigation_deadline": self.deadline, "run_deadline": self.run_deadline,
            "controller_age_seconds": round(time.monotonic() - self.last_heartbeat, 3), "steam": self.steam_status(),
            "termination": self.termination_result, "disk": {"free_bytes": disk.free, "total_bytes": disk.total},
            "teardown_confirmed": bool(self.termination_result and self.termination_result.get("confirmed")),
            "resources": self.resources(), "game_exit": self._game_exit(),
            "width": self.desktop.width, "height": self.desktop.height, "timestamp": utc_now()}

    def _game_exit(self) -> dict | None:
        if self.game is None or self.game_running:
            return None
        rc = self.game.returncode
        name = None
        if os.name != "nt" and isinstance(rc, int) and rc < 0:
            try:
                name = signal.Signals(-rc).name
            except ValueError:
                pass  # An unknown signal number keeps the raw exit code only.
        return {"exit_code": rc, "signal": name, "log_tail": self._game_log_tail()}

    def _game_log_tail(self) -> str | None:
        try:
            with open(self.run_dir / "game.log", "rb") as handle:
                size = handle.seek(0, os.SEEK_END)
                handle.seek(max(0, size - 4096))
                data = handle.read()
        except (OSError, HTTPException):
            return None
        return data.decode("utf-8", errors="replace")

    def resources(self) -> dict:
        now = time.monotonic()
        if now - self.resource_sample_at < 5:
            return self.resource_sample
        result = {"cpu_percent_one_core": None, "memory_bytes": None, "memory_limit_bytes": None,
            "game_rss_bytes": None, "gpu_memory_bytes": None, "graphics": "Mesa software rendering" if os.environ.get("LIBGL_ALWAYS_SOFTWARE") == "1" else "configured runtime renderer",
            "evidence_bytes": None, "sampled_at": utc_now()}
        with contextlib.suppress(OSError, ValueError, KeyError):
            result["memory_bytes"] = int(Path("/sys/fs/cgroup/memory.current").read_text())
            limit = Path("/sys/fs/cgroup/memory.max").read_text().strip()
            result["memory_limit_bytes"] = int(limit) if limit != "max" else None
            stat = dict(line.split() for line in Path("/sys/fs/cgroup/cpu.stat").read_text().splitlines())
            usage = int(stat["usage_usec"])
            if self.cpu_sample:
                result["cpu_percent_one_core"] = round((usage-self.cpu_sample[1]) / ((now-self.cpu_sample[0])*10000), 1)
            self.cpu_sample = now, usage
        if self.game_running:
            with contextlib.suppress(OSError, ValueError):
                for line in Path(f"/proc/{self.game.pid}/status").read_text().splitlines():
                    if line.startswith("VmRSS:"):
                        result["game_rss_bytes"] = int(line.split()[1]) * 1024
        if self.binding:
            with contextlib.suppress(OSError):
                result["evidence_bytes"] = sum(path.stat().st_size for path in self.run_dir.rglob("*") if path.is_file())
        self.resource_sample, self.resource_sample_at = result, now
        return result

    def _run_root(self, run_id: str | None) -> Path:
        try:
            return self.root / "runs" / safe_id(run_id) if run_id else self.run_dir
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    def evidence_path(self, path: str, run_id: str | None = None) -> Path:
        root = self._run_root(run_id)
        candidate = (root / path).resolve()
        if not candidate.is_relative_to(root.resolve()) or not candidate.is_file():
            raise HTTPException(404, "Evidence file not found")
        if candidate.parts[len(root.resolve().parts)] not in EVIDENCE_ROOTS:
            raise HTTPException(403, "Path is outside evidence exports")
        return candidate

    def evidence_index(self, run_id: str | None = None) -> dict:
        root = self._run_root(run_id)
        if not root.is_dir():
            raise HTTPException(404, "Run evidence is unavailable on this worker")
        files = []
        for path in root.rglob("*"):
            relative = path.relative_to(root)
            if path.is_file() and not path.is_symlink() and relative.parts[0] in EVIDENCE_ROOTS and path.suffix != ".tmp":
                stat = path.stat()
                files.append({"path": str(relative).replace("\\", "/"), "bytes": stat.st_size, "modified_at": stat.st_mtime,
                    "pinned": relative.parts[0] == "pinned"})
        return {"worker_id": self.worker_id, "run_id": root.name, "files": files, "total_bytes": sum(file["bytes"] for file in files),
            "finalized": not (self.binding and self.binding.run_id == root.name and self.game_running), "captured_at": utc_now()}

    def export_fixture(self, manifest: dict) -> dict:
        target_run = self._run_root(manifest.get("source_run_id") or (self.binding.run_id if self.binding else ""))
        run_id = target_run.name
        if not target_run.is_dir():
            raise HTTPException(404, "Requested run save tree is unavailable")
        if self.game_running and self.binding and self.binding.run_id == run_id:
            raise HTTPException(409, "Stop the game before exporting a consistent save package")
        binding_path = target_run / "binding.json"
        if binding_path.is_file():
            binding = Binding.model_validate_json(binding_path.read_text())
        else:
            binding = None
            events = target_run / "events.jsonl"
            if events.is_file():
                for line in events.read_text().splitlines():
                    with contextlib.suppress(ValueError, TypeError):
                        record = json.loads(line)
                        if record.get("kind") == "bound":
                            binding = Binding.model_validate(record)
                            break
            if binding is None:
                raise HTTPException(409, "Historical run identity manifest is missing; refusing guessed character export")
        required = {"world_id", "build_revision", "save_version", "starting_milestones", "identity_mapping"}
        if not required.issubset(manifest):
            raise HTTPException(422, f"Fixture manifest is missing {sorted(required - set(manifest))}")
        if not isinstance(manifest["identity_mapping"], dict):
            raise HTTPException(422, "Fixture identity_mapping must be an object")
        if str(manifest["identity_mapping"].get(binding.player_id)) != binding.identity or manifest.get("player_id", binding.player_id) != binding.player_id:
            raise HTTPException(422, "Fixture identity mapping does not contain the bound player identity")
        manifest = {**manifest, "transport": binding.transport, "source_run_id": run_id, "player_id": binding.player_id}
        game_dir = target_run / "game"
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in game_dir.rglob("*"):
                if path.is_file() and not path.is_symlink() and path.name != "ashad-fixture.json":
                    archive.write(path, str(path.relative_to(game_dir)))
            archive.writestr("ashad-fixture.json", json.dumps(manifest))
        content = stream.getvalue()
        return {"manifest": manifest, "sha256": hashlib.sha256(content).hexdigest(), "archive_base64": base64.b64encode(content).decode()}

    def _unpack_archive(self, archive_base64: str, check=None) -> bytes:
        if len(archive_base64) > 140_000_000:
            raise HTTPException(413, "Fixture archive exceeds 100 MiB")
        try:
            blob = base64.b64decode(archive_base64, validate=True)
            with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                entries = archive.infolist()
                if sum(entry.file_size for entry in entries) > 1024 * 1024 * 1024:
                    raise ValueError("Unpacked fixture exceeds 1 GiB")
                for entry in entries:
                    path = (self.game_dir / entry.filename).resolve()
                    if not path.is_relative_to(self.game_dir.resolve()) or "\\" in entry.filename or (entry.external_attr >> 16) & 0o170000 == 0o120000:
                        raise ValueError("Fixture contains an unsafe path or symlink")
                if check:
                    check(archive)
                archive.extractall(self.game_dir)
        except (ValueError, zipfile.BadZipFile, KeyError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return blob

    async def capture_save(self, request: SaveCapture) -> dict:
        async with self.state_lock:
            self.check_binding(request)
            running = self.game_running
            epoch = self.game_control_epoch
            game_dir = self.game_dir
        status, detail = "on_disk", "Game is not running"
        if running:
            try:
                result = await self.game_bridge.request("save_game", {"epoch": epoch, "save_id": request.save_id})
                if result.get("status") == "saved":
                    status, detail = "confirmed", ""
                else:
                    detail = str(result.get("reason") or result.get("error") or "Game did not confirm the save")[:300]
            except (OSError, ValueError, asyncio.TimeoutError) as exc:
                detail = type(exc).__name__
        game_dir.mkdir(parents=True, exist_ok=True)
        content = await asyncio.to_thread(_zip_game_dir, game_dir)
        if content == b"":
            status = "none"
        if len(content) > MAX_SAVE_BYTES:
            raise HTTPException(413, "Save archive exceeds 100 MiB")
        sha = hashlib.sha256(content).hexdigest() if content else None
        self._event("save_captured", {"save_id": request.save_id, "game_save": status, "sha256": sha, "bytes": len(content)})
        return {"save_id": request.save_id, "game_save": status, "detail": detail, "sha256": sha, "bytes": len(content),
            "archive_base64": base64.b64encode(content).decode()}

    def restore_save(self, request: SaveRestore) -> dict:
        self.check_binding(request)
        if self.lifecycle != "bound" or self.game_running:
            raise HTTPException(409, "A save can only be restored before the game starts")
        if request.identity != self.binding.identity or request.transport != self.binding.transport:
            raise HTTPException(422, "Save identity or transport does not match this player")
        self.game_dir.mkdir(parents=True, exist_ok=True)
        if any(self.game_dir.iterdir()):
            raise HTTPException(409, "Run save directory is not empty; source fixtures and existing saves are never overwritten")
        try:
            blob = base64.b64decode(request.archive_base64, validate=True)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if hashlib.sha256(blob).hexdigest() != request.sha256:
            raise HTTPException(422, "Save archive checksum does not match")
        self._unpack_archive(request.archive_base64)
        self._event("save_restored", {"sha256": request.sha256})
        return {"restored": True, "sha256": request.sha256}

    def import_fixture(self, request: FixtureImport) -> dict:
        self.check_binding(request)
        if self.lifecycle != "bound" or self.game_running:
            raise HTTPException(409, "A fixture can only be restored before the game starts")
        manifest = request.manifest
        required = {"world_id", "build_revision", "save_version", "starting_milestones", "identity_mapping", "transport"}
        if not required.issubset(manifest):
            raise HTTPException(422, f"Fixture manifest is missing {sorted(required - set(manifest))}")
        if not isinstance(manifest["identity_mapping"], dict):
            raise HTTPException(422, "Fixture identity_mapping must be an object")
        original_identity = str(manifest["identity_mapping"].get(self.binding.player_id, ""))
        target_identity = request.identity_mapping.get(original_identity, original_identity)
        if target_identity != self.binding.identity:
            raise HTTPException(422, "Fixture character identity does not match this player and no explicit mapping was supplied")
        # Changing save identity requires a game-version-specific validated converter. Never infer it by copying files.
        if manifest["transport"] != self.binding.transport or original_identity != target_identity:
            raise HTTPException(422, "Cross-transport or changed-account fixtures require a separately validated converted package")
        if any(self.game_dir.iterdir()):
            raise HTTPException(409, "Run save directory is not empty; source fixtures and existing saves are never overwritten")
        def check(archive: zipfile.ZipFile) -> None:
            embedded = json.loads(archive.read("ashad-fixture.json")) if "ashad-fixture.json" in archive.namelist() else None
            if embedded is not None and any(embedded.get(key) != manifest.get(key) for key in required):
                raise ValueError("Embedded fixture manifest does not match the approved manifest")

        blob = self._unpack_archive(request.archive_base64, check)
        atomic_json(self.run_dir / "fixture-manifest.json", manifest)
        self._event("fixture_restored", {"sha256": hashlib.sha256(blob).hexdigest(), "manifest": manifest})
        return {"restored": True, "manifest": manifest, "sha256": hashlib.sha256(blob).hexdigest()}

    async def _start_steam(self) -> None:
        if self.steam and self.steam.returncode is None:
            return
        # Steam output is private setup state and is deliberately not retained with evidence.
        executable = shutil.which("steam") or "/usr/games/steam"
        self.steam = await asyncio.create_subprocess_exec(executable, "-silent", env={**os.environ, "DISPLAY": self.desktop.display},
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)

    async def _start_desktop_viewer(self) -> None:
        password = Path(os.environ.get("ASHAD_VNC_PASSWORD_FILE", "/run/secrets/vnc_password"))
        secret = password.read_text().strip() if password.is_file() else ""
        if not secret:
            raise HTTPException(503, "A private VNC password file must be provisioned first")
        # Never on argv (visible in ps). A -rfbauth file is only obscured with a fixed key, so plaintext 0600 is equivalent.
        stored_password = self.root / "private" / "vnc-password"
        stored_password.parent.mkdir(exist_ok=True)
        stored_password.unlink(missing_ok=True)
        with os.fdopen(os.open(stored_password, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
            stream.write(secret + "\n")
        self.desktop_session = secrets.token_hex(16)
        env = {**os.environ, "DISPLAY": self.desktop.display, "ASHAD_DESKTOP_SESSION": self.desktop_session}
        try:
            for command in (
                ["x11vnc", "-display", self.desktop.display, "-localhost", "-passwdfile", str(stored_password),
                 "-forever", "-shared", "-clear_all", "-rfbport", "5900"],
                ["websockify", "--web=/usr/share/novnc", "6080", "127.0.0.1:5900"],
            ):
                self.setup_processes.append(await asyncio.create_subprocess_exec(*command, env=env,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=(os.name != "nt")))
                self._persist()
            async with asyncio.timeout(5):
                for port in (5900, 6080):
                    while True:
                        if any(process.returncode is not None for process in self.setup_processes):
                            raise RuntimeError("Desktop viewer process exited during startup")
                        try:
                            _, writer = await asyncio.open_connection("127.0.0.1", port)
                        except OSError:
                            await asyncio.sleep(0.05)
                            continue
                        writer.close()
                        await writer.wait_closed()
                        break
        except BaseException:
            await self._stop_desktop_viewer()
            raise

    async def _stop_desktop_viewer(self) -> None:
        try:
            async with contextlib.AsyncExitStack() as cleanup:
                for process in self.setup_processes:
                    cleanup.push_async_callback(self._terminate_process, process)
        finally:
            self.setup_processes = [process for process in self.setup_processes if process.returncode is None]
            if not self.setup_processes:
                self.desktop_session = None
            self._persist()

    async def start_desktop(self, request: BoundCommand) -> dict:
        async with self.state_lock:
            self.check_binding(request)
            if self.lifecycle != "paused" or not self.game_running or self.steam_setup:
                raise HTTPException(409, "Take over requires a paused run with a live game")
            if not self.human_control:
                self.interrupt.set()
                await self.release_inputs("human take over")
                try:
                    held = await self.game_bridge.request("hold", {"reason": "human take over"})
                except (OSError, ValueError, asyncio.TimeoutError) as exc:
                    raise HTTPException(503, "Take over needs confirmation that the game's bot inputs have stopped") from exc
                if held.get("status") != "held":
                    raise HTTPException(503, "The game did not confirm that bot inputs have stopped")
                await self.desktop.focus(self.game.pid)
                self.human_control = True
                self._persist()
                try:
                    await self._start_desktop_viewer()
                except BaseException as exc:
                    await self._finish_human_control("desktop startup failed")
                    if isinstance(exc, (OSError, RuntimeError, asyncio.TimeoutError)):
                        raise HTTPException(503, "The desktop viewer could not start") from exc
                    raise
                self._event("human_control_started", {"command_id": request.command_id})
            return {**self.status(), "desktop_path": "/vnc.html", "capture_disabled": False}

    async def _finish_human_control(self, reason: str) -> None:
        if not self.human_control:
            return
        await self._stop_desktop_viewer()
        # VNC input does not pass through held_keys. x11vnc clears every key on exit;
        # also release the game keyboard if its process needed a forced shutdown.
        await self.desktop.input("keyup", "--delay", "0", *"abcdefghijklmnopqrstuvwxyz0123456789",
            "Up", "Down", "Left", "Right", "space", "Return", "Escape", "Tab", "BackSpace", "Delete",
            "Home", "End", "Prior", "Next", "Insert", "minus", "equal", "comma", "period", "slash",
            "semicolon", "apostrophe", "bracketleft", "bracketright", "backslash", "grave",
            *(f"F{number}" for number in range(1, 13)))
        self.held_buttons.update(range(1, 10))
        await self.release_inputs(reason)
        self.observations.clear()
        self.game_observations.clear()
        self.human_control = False
        self._persist()
        self._event("human_control_finished", {"reason": reason})

    async def finish_desktop(self, request: BoundCommand) -> dict:
        async with self.state_lock:
            self.check_binding(request)
            await self._finish_human_control("human returned control")
            return self.status()

    async def start_steam_setup(self) -> dict:
        async with self.state_lock:
            if self.game_running or self.human_control or self.lifecycle not in ("unbound", "ended"):
                raise HTTPException(409, "Steam setup needs an unleased desktop with no game process")
            if self.steam_setup:
                return self.steam_status()
            await self._stop_video()
            self.observations.clear()
            try:
                await self._start_steam()
                await self._start_desktop_viewer()
            except (OSError, RuntimeError, asyncio.TimeoutError) as exc:
                raise HTTPException(503, "Steam setup could not start") from exc
            self.steam_setup = True
            return {**self.steam_status(), "desktop_path": "/vnc.html", "capture_disabled": True,
                "instructions": "Sign in using the official Steam client and complete Steam Guard. Finish setup before reserving this desktop. Never send passwords to the application."}

    async def finish_steam_setup(self) -> dict:
        async with self.state_lock:
            if self.human_control:
                raise HTTPException(409, "Use the run's Return to bot control before finishing desktop setup")
            await self._stop_desktop_viewer()
            self.steam_setup = False
            return self.steam_status()

    async def verify_steam(self, request: SteamVerify) -> dict:
        """Read-only authentication probe from the real game main menu, with capture disabled."""
        async with self.state_lock:
            if self.game_running or self.lifecycle not in ("unbound", "ended"):
                raise HTTPException(409, "Account verification requires an unleased desktop")
            if request.app_id <= 0 or request.app_id == 480 or not request.steam_id.isdigit():
                raise HTTPException(422, "A real game App ID and expected Steam ID are required")
            if not request.command or not Path(request.command[0]).is_file():
                raise HTTPException(422, "Verification game executable is missing")
            if not executable_allowed(Path(request.command[0])):
                raise HTTPException(422, "Verification executable is outside allowed build roots")
            await self._start_steam()
            await self._stop_video()
            self.observations.clear()
            probe_id = f"steam-probe-{time.time_ns()}"
            directory = self.root / "private" / probe_id
            directory.mkdir(parents=True)
            telemetry = directory / "telemetry.json"
            env = {**os.environ, "DISPLAY": self.desktop.display, "ASHAD_RUN_ID": probe_id, "ASHAD_PLAYER_ID": "account-probe",
                "ASHAD_TRANSPORT": "steam", "ASHAD_IDENTITY": request.steam_id, "ASHAD_STEAM_APP_ID": str(request.app_id),
                "SteamAppId": str(request.app_id), "SteamGameId": str(request.app_id), "ASHAD_TELEMETRY_PATH": str(telemetry),
                "ASHAD_DATA_DIR": str(directory / "game"), "XDG_DATA_HOME": str(directory / "xdg-data"), "XDG_CONFIG_HOME": str(directory / "xdg-config")}
            env = game_environment(env)
            process = await asyncio.create_subprocess_exec(*request.command, cwd=str(Path(request.command[0]).parent), env=env,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=(os.name != "nt"))
            result = {"verified": False, "app_id": request.app_id, "steam_id": request.steam_id, "checked_at": utc_now(),
                "reason": "No authenticated game telemetry received; sign in and ensure this account has game access", "capture_disabled": True}
            try:
                for _ in range(40):
                    if telemetry.is_file():
                        with contextlib.suppress(OSError, ValueError):
                            observed = json.loads(telemetry.read_text())
                            if observed.get("run_id") == probe_id:
                                valid = bool(observed.get("authenticated") and str(observed.get("steam_id")) == request.steam_id and observed.get("app_id") == request.app_id and observed.get("app_subscribed") is True and not observed.get("offline_fallback"))
                                result.update(verified=valid, actual_steam_id=observed.get("steam_id"), actual_app_id=observed.get("app_id"),
                                    license_verified=observed.get("app_subscribed") is True,
                                    reason="Authenticated account and selected real App ID verified by game initialization" if valid else "Game reported invalid authentication, wrong account, App ID mismatch, or offline fallback")
                                break
                    if process.returncode is not None:
                        result["reason"] = "Game probe exited before authentication telemetry; check Steam login and game entitlement"
                        break
                    await asyncio.sleep(0.5)
            finally:
                await self._terminate_process(process)
            atomic_json(self.root / "private" / "steam-verification.json", result)
            return result
