"""Contract tests use explicit desktop doubles; real-client checks are recorded separately."""
import asyncio
import base64
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock
import zipfile

import httpx
from fastapi import HTTPException

from ashad.worker_client import WorkerClient, WorkerError
from worker.app import create_app
from worker.models import Binding, BoundCommand, FixtureImport, Heartbeat, Pin, Stop
from worker.runtime import Runtime, atomic_json


class DesktopDouble:
    display = ":99"
    width, height = 1280, 720

    def __init__(self):
        self.commands = []
        self.releases = []
        self.fail_input = False

    async def input(self, *args):
        self.commands.append(args)
        if self.fail_input:
            raise RuntimeError("injected xdotool failure")

    async def focus(self, pid):
        self.commands.append(("focus", pid))

    async def capture(self, path):
        path.write_bytes(b"test frame; not a real gameplay observation")

    async def command(self, *args, **kwargs):
        if args[0] == "ffmpeg" and args[-1].endswith(".mp4"):
            Path(args[-1]).write_bytes(b"test mux double; not real video")
        return b""

    async def release_all(self, keys, buttons):
        self.releases.append((set(keys), set(buttons)))


class ProcessDouble:
    pid = 54321
    returncode = None


class WorkerContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.desktop = DesktopDouble()
        self.worker = Runtime(Path(self.tmp.name), desktop=self.desktop, worker_id="worker-A")
        self.binding = Binding(run_id="run-A", player_id="player-A", epoch="epoch-A", identity="identity-A")
        await self.worker.bind(self.binding)
        self.worker.game_bridge.request = AsyncMock(return_value={"status": "completed"})
        self.worker.game = ProcessDouble()
        self.worker.lifecycle = "running"
        self.observation = await self.worker.capture()
        self.desktop.commands.clear()

    async def asyncTearDown(self):
        self.worker.game = None
        self.tmp.cleanup()


    def command(self):
        return BoundCommand(run_id="run-A", player_id="player-A", epoch="epoch-A")




    async def test_controller_loss_and_held_input_expiry_release(self):
        self.worker.held_keys.add("W")
        self.worker.held_input_expires_at = time.time() - 1
        await self.worker.watchdog_tick()
        self.assertFalse(self.worker.held_keys)
        self.worker.last_heartbeat -= 100
        await self.worker.watchdog_tick()
        self.assertEqual(self.worker.lifecycle, "paused")
        self.assertEqual(self.worker.infrastructure_error, "controller_heartbeat_lost")

    async def test_capture_failure_does_not_claim_game_freeze(self):
        self.worker.capture_error = "ffmpeg failed"
        await self.worker.watchdog_tick()
        self.assertEqual(self.worker.infrastructure_error, "capture_unhealthy")
        self.assertEqual(self.worker.lifecycle, "paused")
        self.assertTrue(self.worker.game_running)



    async def test_shutdown_drains_tasks_and_attempts_all_cleanup_after_release_failure(self):
        pending = asyncio.create_task(asyncio.Event().wait())
        self.worker.tasks = [pending]
        self.worker.release_inputs = AsyncMock(side_effect=RuntimeError("release failed"))
        self.worker._terminate_process = AsyncMock()
        self.worker._stop_video = AsyncMock()
        self.worker.finish_steam_setup = AsyncMock()
        with self.assertRaisesRegex(RuntimeError, "release failed"):
            await self.worker.close()
        self.assertTrue(pending.done())
        self.worker._terminate_process.assert_awaited_once_with(self.worker.game)
        self.worker._stop_video.assert_awaited_once()
        self.worker.finish_steam_setup.assert_awaited_once()


    async def test_deadline_persisted_and_termination_is_not_extended_by_heartbeat(self):
        deadline = time.time() - 1
        await self.worker.heartbeat(Heartbeat(**self.command().model_dump(), investigation_deadline=deadline))
        state = json.loads((self.worker.root / "worker-state.json").read_text())
        self.assertEqual(state["investigation_deadline"], deadline)
        called = []

        async def terminate(process, hard=False):
            called.append(hard)
            process.returncode = -9

        self.worker._terminate_process = terminate
        await self.worker.watchdog_tick()
        self.assertEqual(called, [True])
        self.assertEqual(self.worker.lifecycle, "ended")
        self.assertTrue(self.worker.termination_result["confirmed"])

    async def test_steam_actual_app_and_identity_are_required(self):
        self.worker.binding = self.binding.model_copy(update={"transport": "steam", "app_id": 3753020, "identity": "76561190000000001"})
        self.assertFalse(self.worker._capture_allowed())
        telem = {"run_id": "run-A", "player_id": "player-A", "authenticated": True, "app_subscribed": True, "steam_id": "76561190000000001", "app_id": 480, "transport": "steam"}
        atomic_json(self.worker.run_dir / "telemetry.json", telem)
        self.assertFalse(self.worker.steam_status()["app_id_verified"])
        telem["app_id"] = 3753020
        atomic_json(self.worker.run_dir / "telemetry.json", telem)
        self.assertTrue(self.worker._capture_allowed())
        telem["transport"] = "none"
        atomic_json(self.worker.run_dir / "telemetry.json", telem)
        self.assertTrue(self.worker._capture_allowed())
        self.assertFalse(self.worker.steam_status()["transport_verified"])
        telem["transport"] = "enet"
        atomic_json(self.worker.run_dir / "telemetry.json", telem)
        self.assertFalse(self.worker._capture_allowed())

    async def test_fixture_roundtrip_and_traversal_rejection(self):
        self.worker.game = None
        self.worker.lifecycle = "bound"
        manifest = dict(world_id="world-A", build_revision="abc", save_version="1", starting_milestones=[],
            identity_mapping={"player-A": "identity-A"}, transport="enet")
        (self.worker.game_dir / "world.save").write_text("immutable fixture contents")
        exported = self.worker.export_fixture(manifest)
        (self.worker.game_dir / "world.save").unlink()
        imported = self.worker.import_fixture(FixtureImport(**self.command().model_dump(), manifest=exported["manifest"], archive_base64=exported["archive_base64"]))
        self.assertEqual(imported["sha256"], exported["sha256"])
        self.assertEqual((self.worker.game_dir / "world.save").read_text(), "immutable fixture contents")
        for path in self.worker.game_dir.iterdir():
            path.unlink()
        blob = io.BytesIO()
        with zipfile.ZipFile(blob, "w") as archive:
            archive.writestr("../escaped.txt", "invalid")
        with self.assertRaises(HTTPException):
            self.worker.import_fixture(FixtureImport(**self.command().model_dump(), manifest=manifest, archive_base64=base64.b64encode(blob.getvalue()).decode()))
        self.assertFalse((self.worker.run_dir / "escaped.txt").exists())

    async def test_fixture_identity_conversion_is_explicitly_rejected(self):
        self.worker.game = None
        self.worker.lifecycle = "bound"
        manifest = dict(world_id="world-A", build_revision="abc", save_version="1", starting_milestones=[],
            identity_mapping={"player-A": "steam-account"}, transport="steam")
        with self.assertRaises(HTTPException) as caught:
            self.worker.import_fixture(FixtureImport(**self.command().model_dump(), manifest=manifest, archive_base64="",
                identity_mapping={"steam-account": "identity-A"}))
        self.assertIn("validated converted", caught.exception.detail)

    async def test_pin_preserves_segments_and_marks_missing_post_window(self):
        rolling = self.worker.run_dir / "rolling"
        rolling.mkdir()
        (rolling / "segment-1.ts").write_bytes(b"completed video segment")
        (rolling / "segment-2.ts").write_bytes(b"active video segment")
        pin = await self.worker.pin(Pin(**self.command().model_dump(), incident_id="incident-A", before_seconds=30, after_seconds=30))
        self.assertEqual(len(pin["segments"]), 1)
        await self.worker._finish_pin(pin, final=True)
        self.assertEqual(len(pin["segments"]), 2)
        self.assertIn("Post-event capture ended", pin["limitations"][0])

    async def test_old_run_cannot_pin_reused_worker_and_status_is_explicit(self):
        with self.assertRaises(HTTPException):
            await self.worker.pin(Pin(run_id="old-run", player_id="player-A", epoch="old-epoch", incident_id="old-incident"))
        self.assertFalse(self.worker.status()["teardown_confirmed"])


    async def test_reconnect_retains_binding_data_and_observations_cannot_cross_pause(self):
        self.worker.game.returncode = 0
        (self.worker.game_dir / "character.save").write_text("same identity")
        await self.worker.stop(Stop(**self.command().model_dump(), terminate=True), disconnect=True)
        self.assertEqual(self.worker.lifecycle, "disconnected")
        same = await self.worker.bind(self.binding)
        self.assertEqual(same["lifecycle"], "disconnected")
        self.assertEqual((self.worker.game_dir / "character.save").read_text(), "same identity")
        self.assertEqual(self.worker.binding.identity, self.binding.identity)

    async def test_http_authentication_and_client_binding(self):
        token = "test-token-" + "x" * 30
        app = create_app(self.worker, token=token)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as unauthorized:
            self.assertEqual((await unauthorized.get("/health")).status_code, 401)
        async with WorkerClient("http://test", token, transport=transport) as client:
            with self.assertRaises(WorkerError):
                await client.request("POST", "/action", data={})
            health = await client.health()
            self.assertEqual(health["worker_id"], "worker-A")
            client.set_binding("run-A", "player-A", "epoch-A")
            self.assertEqual((await client.pause())["lifecycle"], "paused")
            await client.resume()
            observation = await client.observation(fresh=True)
            self.assertTrue(await client.screenshot(observation["id"]))
            with self.assertRaises(WorkerError):
                await client.evidence("../../private/steam-password")

    async def test_startup_bridge_refusal_retries_until_ready_but_other_failures_surface(self):
        app = create_app(self.worker, token="startup-test-token-" + "x" * 24)
        self.worker.launched_monotonic = time.monotonic()
        self.worker.game_bridge.request = AsyncMock(side_effect=ConnectionRefusedError("listener not ready"))
        async with WorkerClient("http://test", "startup-test-token-" + "x" * 24,
                                transport=httpx.ASGITransport(app=app)) as client:
            client.set_binding("run-A", "player-A", "epoch-A")
            deadline = time.time() + 100
            heartbeat = await client.heartbeat(investigation_deadline=deadline)
            self.assertEqual(heartbeat["game_bridge"], "starting")
            self.worker.game_bridge.request.assert_awaited_once()
            persisted = json.loads((self.worker.root / "worker-state.json").read_text())
            self.assertEqual(persisted["investigation_deadline"], deadline)
            with self.assertRaises(WorkerError) as starting:
                await client.request("POST", "/game", data={**self.command().model_dump(), "operation": "observe"})
            self.assertEqual(starting.exception.code, "game_bridge_starting")
            self.worker.game_bridge.request = AsyncMock(return_value={"ok": True})
            self.assertEqual((await client.heartbeat())["game_bridge"], "ready")
            self.assertEqual(self.worker.lifecycle, "running")
            self.worker.game_bridge.request = AsyncMock(side_effect=ValueError("incompatible response"))
            with self.assertRaises(WorkerError) as protocol:
                await client.heartbeat()
            self.assertEqual(protocol.exception.code, "game_bridge_unavailable")
            self.assertIn("POST /heartbeat (HTTP 503)", str(protocol.exception))
            self.assertIn("incompatible response", str(protocol.exception))
            self.worker.launched_monotonic -= 16
            self.worker.game_bridge.request = AsyncMock(side_effect=ConnectionRefusedError("listener absent"))
            with self.assertRaises(WorkerError) as expired:
                await client.heartbeat()
            self.assertEqual(expired.exception.code, "game_bridge_unavailable")

    async def test_worker_errors_identify_operation_without_retrying_or_exposing_token(self):
        token = "private-controller-service-token"
        for status, payload in ((500, b"Internal Server Error"), (200, b"not JSON")):
            calls = []
            def failed(request):
                calls.append(request)
                return httpx.Response(status, content=payload)
            async with WorkerClient("http://test", token, transport=httpx.MockTransport(failed)) as client:
                with self.assertRaises(WorkerError) as failure:
                    await client.request("POST", "/game", data={"operation": "perform_action"})
                self.assertEqual(len(calls), 1)
                self.assertEqual(failure.exception.status_code, status)
                self.assertTrue(failure.exception.ambiguous)
                self.assertIn(f"POST /game (HTTP {status})", str(failure.exception))
                self.assertNotIn(token, str(failure.exception))

    async def test_missing_partial_and_nonobject_telemetry_are_unavailable_not_http_500(self):
        self.assertEqual(self.worker.telemetry()["reason"], "telemetry_not_yet_written")
        path = self.worker.run_dir / "telemetry.json"
        for raw in ('{"run_id":', '[]'):
            path.write_text(raw)
            self.assertFalse(self.worker.telemetry()["available"])

    async def test_recovery_marks_unacknowledged_execution_unknown(self):
        self.worker._persist()
        recovered = Runtime(self.worker.root, DesktopDouble(), "worker-A")
        self.assertEqual(recovered.lifecycle, "ended")
        self.assertIn("worker_restarted", recovered.infrastructure_error)
        self.assertFalse(recovered.game_observations)

    async def test_historical_fixture_export_and_evidence_exclude_private_data(self):
        (self.worker.game_dir / "world.save").write_text("historical world")
        self.worker.game = None
        self.worker.lifecycle = "ended"
        await self.worker.bind(Binding(run_id="run-B", player_id="player-B", epoch="epoch-B", identity="identity-B"))
        self.worker.game = ProcessDouble()
        self.worker.lifecycle = "running"
        manifest = dict(source_run_id="run-A", player_id="player-A", world_id="world-A", build_revision="abc", save_version="1",
            starting_milestones=[], identity_mapping={"player-A": "identity-A"})
        exported = self.worker.export_fixture(manifest)
        self.assertEqual(exported["manifest"]["source_run_id"], "run-A")
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(exported["archive_base64"]))) as archive:
            self.assertEqual(archive.read("world.save"), b"historical world")
        private = self.worker.root / "private"
        private.mkdir()
        (private / "secret.txt").write_text("never exported")
        index = self.worker.evidence_index("run-A")
        self.assertTrue(index["finalized"])
        self.assertTrue(any(item["path"] == "binding.json" for item in index["files"]))
        self.assertFalse(any("secret" in item["path"] or "world.save" in item["path"] for item in index["files"]))
        with self.assertRaises(HTTPException):
            self.worker.export_fixture({**manifest, "player_id": "player-B"})


if __name__ == "__main__":
    unittest.main()


def test_game_launch_boundary_helpers(tmp_path, monkeypatch):
    from worker.runtime import executable_allowed, game_environment
    root = tmp_path / "game"
    root.mkdir()
    executable = root / "game.exe"
    monkeypatch.setenv("ASHAD_EXECUTABLE_ROOTS", str(root))
    assert executable_allowed(executable)
    assert not executable_allowed(root / ".." / "outside.exe")
    assert not executable_allowed(tmp_path / "game-other" / "game.exe")
    secrets = {key: "private" for key in (
        "ASHAD_WORKER_TOKEN", "ASHAD_WORKER_TOKEN_FILE", "ASHAD_VNC_PASSWORD",
        "ASHAD_VNC_PASSWORD_FILE", "OPENAI_API_KEY")}
    env = {**secrets, "DISPLAY": ":99", "ASHAD_RUN_ID": "run", "SteamAppId": "3753020"}
    assert game_environment(env) == {"DISPLAY": ":99", "ASHAD_RUN_ID": "run", "SteamAppId": "3753020"}
    assert all(key in env for key in secrets)
