"""No Docker, X11 server, game, or input is used: native calls are read-only doubles."""
import asyncio
import ctypes as C
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from worker.app import create_app
from worker.desktop import Desktop
from worker.models import Binding
from worker.prepare_probe import _XImage, probe_desktop
from worker.runtime import Runtime, sha256_file


TOKEN = "prepare-test-token-" + "x" * 30


class NativeDouble:
    def __init__(self, *, input_available=True, image_available=True, bad_buffer=False, width=16, x_error=False):
        self.calls, self.handlers = [], {}
        self.x_error = x_error
        self.pixels = C.create_string_buffer(b"\x14\x28\x3c\x00" * 16 * 8)
        self.image = _XImage(width=16, height=8, data=C.addressof(self.pixels), bytes_per_line=0 if bad_buffer else 64, bits_per_pixel=32)
        self.width = width
        self.input_available = input_available
        self.image_available = image_available
        self.x11, self.xtst = Mock(), Mock()
        for library, names in ((self.x11, ("XOpenDisplay", "XCloseDisplay", "XDefaultRootWindow", "XGetGeometry", "XGetImage", "XDestroyImage", "XSetErrorHandler")),
                               (self.xtst, ("XTestQueryExtension", "XTestFakeKeyEvent", "XTestFakeButtonEvent", "XTestFakeMotionEvent"))):
            for name in names:
                setattr(library, name, Mock(side_effect=lambda *args, name=name: self.call(name, args)))

    def call(self, name, args):
        self.calls.append(name)
        if name.startswith("XTestFake"):
            raise AssertionError("Input injection is forbidden")
        if name == "XOpenDisplay":
            return 42
        if name == "XDefaultRootWindow":
            return 7
        if name == "XGetGeometry":
            for pointer, value in zip(args[5:], (self.width, 8, 0, 24)):
                C.cast(pointer, C.POINTER(C.c_uint))[0] = value
            return 1
        if name == "XSetErrorHandler":
            self.handlers[name] = args[0].value
            return None
        if name == "XGetImage":
            if self.x_error:  # Xlib reports the async protocol error through the installed handler.
                C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_void_p)(self.handlers["XSetErrorHandler"])(None, None)
            return C.pointer(self.image) if self.image_available else None
        if name == "XTestQueryExtension":
            C.cast(args[3], C.POINTER(C.c_int))[0] = 2
            C.cast(args[4], C.POINTER(C.c_int))[0] = 2
            return int(self.input_available)
        return 1

    def load(self, name):
        return {"libX11.so.6": self.x11, "libXtst.so.6": self.xtst}[name]

    def probe(self):
        with patch("worker.prepare_probe.sys.platform", "linux"), patch("worker.prepare_probe.Path") as path, \
             patch("worker.prepare_probe.C.CDLL", side_effect=self.load), patch("worker.prepare_probe.shutil.which", return_value="/usr/bin/xdotool"), \
             patch("asyncio.create_subprocess_exec", side_effect=AssertionError("No spawn")):
            path.return_value.is_file.return_value = True
            return probe_desktop(Desktop(":99", 16, 8))


class NativeProbeTests(unittest.TestCase):
    def test_actual_pixel_read_and_xtest_query_not_input_events(self):
        native = NativeDouble()
        result = native.probe()
        self.assertTrue(all(result[key] for key in ("rendered", "capture_ready", "input_ready")))
        self.assertEqual((result["width"], result["height"], result["capture_bytes"]), (16, 8, 512))
        self.assertNotIn("pixels", result)
        self.assertIn("XGetImage", native.calls)
        self.assertIn("XTestQueryExtension", native.calls)
        self.assertEqual(native.calls[-2:], ["XDestroyImage", "XCloseDisplay"])
        self.assertFalse(any(name.startswith("XTestFake") for name in native.calls))

    def test_x_protocol_error_fails_probe_instead_of_exiting_worker(self):
        native = NativeDouble(x_error=True)
        result = native.probe()
        self.assertIn("XSetErrorHandler", native.calls)
        self.assertFalse(result["rendered"] or result["capture_ready"] or result["input_ready"])
        self.assertIn("X11 protocol error", result["checks"]["rendered"])
        self.assertEqual(native.calls[-2:], ["XDestroyImage", "XCloseDisplay"])
        self.assertTrue(NativeDouble().probe()["rendered"])  # Errors are scoped to one probe.

    def test_missing_extension_does_not_fabricate_input_ready(self):
        result = NativeDouble(input_available=False).probe()
        self.assertTrue(result["capture_ready"])
        self.assertFalse(result["input_ready"])
        self.assertIn("XTEST extension", result["checks"]["input_ready"])

    def test_missing_image_invalid_buffer_and_wrong_geometry_are_not_rendered(self):
        for options in ({"image_available": False}, {"bad_buffer": True}, {"width": 32}):
            with self.subTest(options=options):
                native = NativeDouble(**options)
                result = native.probe()
                self.assertFalse(result["rendered"])
                self.assertFalse(result["capture_ready"])
                self.assertFalse(result["input_ready"])
                self.assertNotIn("XTestQueryExtension", native.calls)
                self.assertEqual(native.calls[-1], "XCloseDisplay")

    def test_missing_native_library_and_nonlinux_are_unknown(self):
        with patch("worker.prepare_probe.sys.platform", "linux"), patch("worker.prepare_probe.Path") as path, patch("worker.prepare_probe.C.CDLL", side_effect=OSError("X11 missing")):
            path.return_value.is_file.return_value = True
            result = probe_desktop(Desktop())
            self.assertFalse(result["capture_ready"])
            self.assertIn("X11 missing", result["checks"]["rendered"])
        with patch("worker.prepare_probe.sys.platform", "win32"), patch("worker.prepare_probe.C.CDLL") as load:
            self.assertFalse(probe_desktop(Desktop())["rendered"])
            load.assert_not_called()

    def test_missing_control_tool_not_ready_despite_xtest(self):
        native = NativeDouble()
        with patch("worker.prepare_probe.sys.platform", "linux"), patch("worker.prepare_probe.Path") as path, patch("worker.prepare_probe.C.CDLL", side_effect=native.load), patch("worker.prepare_probe.shutil.which", return_value=None):
            path.return_value.is_file.return_value = True
            result = probe_desktop(Desktop(":99", 16, 8))
            self.assertTrue(result["rendered"])
            self.assertFalse(result["input_ready"])


class PrepareEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        game = self.root / "game"
        game.mkdir()
        engine, pack = game / "engine", game / "game.pck"
        engine.write_bytes(b"fixture pinned engine, never executed")
        engine.chmod(0o755)
        pack.write_bytes(b"fixture pinned supported game pack")
        self.manifest_path = game / "manifest.json"
        manifest = {"command": [str(engine), "--main-pack", str(pack)], "structured_version": 1, "integration_version": 2,
                    "engine_sha256": sha256_file(engine), "artifact_sha256": sha256_file(pack),
                    "engine_version": "GodotSteam 4.5.1 / 4.17 / Steamworks 1.63", "source_revision": "fixture-pinned-source"}
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.build = {**manifest, "manifest_sha256": sha256_file(self.manifest_path), "id": "fixture-build"}
        self.runtime = Runtime(self.root / "worker", desktop=Desktop(":99", 16, 8), worker_id="fixture-worker")
        for name in ("bind", "launch", "release_inputs", "capture", "verify_steam", "start_steam_setup", "_start_steam"):
            setattr(self.runtime, name, AsyncMock(side_effect=AssertionError("No mutation: " + name)))
        for name in ("input", "command", "focus", "capture", "release_all"):
            setattr(self.runtime.desktop, name, AsyncMock(side_effect=AssertionError("No desktop mutation: " + name)))
        self.transport = httpx.ASGITransport(app=create_app(self.runtime, token=TOKEN))
        self.client = httpx.AsyncClient(transport=self.transport, base_url="http://fixture", headers={"Authorization": "Bearer " + TOKEN})
        self.addAsyncCleanup(self.client.aclose)
        self.roots = patch.dict(os.environ, {"ASHAD_EXECUTABLE_ROOTS": str(game)})
        self.roots.start()
        self.addCleanup(self.roots.stop)
        self.no_spawn = patch("asyncio.create_subprocess_exec", side_effect=AssertionError("No spawn"))
        self.spawn = self.no_spawn.start()
        self.addCleanup(self.no_spawn.stop)

    async def probe(self, build=None):
        return await self.client.post("/prepare/probe", json={"build": self.build if build is None else build})

    async def test_auth_and_request_validation(self):
        response = await self.client.post("/prepare/probe", json={"build": self.build}, headers={"Authorization": "Bearer wrong"})
        self.assertEqual(response.status_code, 401)
        for body in ({}, {"build": []}, {"build": self.build, "command": ["not-allowed"]}):
            self.assertEqual((await self.client.post("/prepare/probe", json=body)).status_code, 422)
        self.spawn.assert_not_called()

    async def test_positive_endpoint_measured_pins_native_pixels_and_steam_unknown(self):
        native = NativeDouble()
        before = list(self.runtime.root.rglob("*"))
        with patch("worker.prepare_probe.probe_desktop", side_effect=lambda desktop: native.probe()):
            response = await self.probe()
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertTrue(result["ready"])
        self.assertTrue(result["independent_desktop"])
        self.assertEqual(result["contract_version"], "ashad-worker/1")
        for key in ("engine_sha256", "artifact_sha256", "structured_version", "integration_version", "engine_version", "source_revision"):
            self.assertEqual(result["build"][key], self.build[key])
        self.assertIsNone(result["binding"])
        self.assertFalse(result["game_running"])
        self.assertEqual(result["held_keys"], [])
        self.assertEqual(result["desktop"]["capture_bytes"], 512)
        self.assertFalse(result["steam"]["license_verified"])
        self.assertFalse(result["steam"]["verified"])
        self.assertIn("unknown", result["steam"]["reason"])
        self.assertIsNone(self.runtime.build_record)
        self.assertEqual(before, list(self.runtime.root.rglob("*")))
        self.spawn.assert_not_called()

    async def test_busy_states_rejected_before_build_or_desktop_reads(self):
        cases = [("binding", Binding(run_id="fixture", player_id="fixture", epoch="fixture", identity="fixture")),
                 ("game", Mock(returncode=None)), ("lifecycle", "paused"), ("lifecycle", "ended"),
                 ("human_control", True), ("steam_setup", True), ("desktop_session", "human"),
                 ("held_keys", {"W"}), ("held_buttons", {1}), ("closed", True),
                 ("setup_processes", [Mock(returncode=None)]), ("infrastructure_error", "unconfirmed teardown")]
        for key, value in cases:
            with self.subTest(key=key, value=value), patch.object(self.runtime, key, value), patch("worker.prepare_probe.measure_build") as measure, patch("worker.prepare_probe.probe_desktop") as desktop:
                response = await self.probe()
                self.assertEqual(response.status_code, 409)
                self.assertTrue(response.json()["detail"])
                measure.assert_not_called()
                desktop.assert_not_called()

    async def test_confirmed_ended_historical_binding_is_read_only_reusable(self):
        self.runtime.binding = Binding(run_id="old", player_id="old", epoch="old", identity="old")
        self.runtime.lifecycle = "ended"
        self.runtime.termination_result = {"confirmed": True}
        with patch("worker.prepare_probe.probe_desktop", side_effect=lambda desktop: NativeDouble().probe()):
            response = await self.probe()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ready"])
        self.assertEqual(response.json()["binding"]["run_id"], "old")
        self.assertEqual(self.runtime.binding.run_id, "old")
        self.runtime.termination_result = {"confirmed": False}
        self.assertEqual((await self.probe()).status_code, 409)

    async def test_missing_and_malformed_manifest_are_not_ready(self):
        self.manifest_path.write_text("not-json")
        self.build["manifest_sha256"] = sha256_file(self.manifest_path)
        self.assertFalse((await self.probe()).json()["ready"])
        manifest = {key: value for key, value in self.build.items() if key not in {"manifest_sha256", "id"}}
        manifest["engine_sha256"] = 7
        self.manifest_path.write_text(json.dumps(manifest))
        self.build["manifest_sha256"] = sha256_file(self.manifest_path)
        self.assertIn("engine_sha256", (await self.probe()).json()["detail"])
        # Delete only the explicit temporary fixture file created by this test.
        self.manifest_path.unlink()
        self.assertIn("manifest is missing", (await self.probe()).json()["detail"])

    async def test_unsupported_missing_and_changed_builds_never_capture(self):
        cases = [{**self.build, "structured_version": 2}, {**self.build, "integration_version": 1},
                 {**self.build, "engine_sha256": None}, {**self.build, "artifact_sha256": "0" * 64},
                 {**self.build, "manifest_sha256": None}, {**self.build, "engine_version": "different"},
                 {**self.build, "command": [str(self.root / "outside"), "--main-pack", self.build["command"][2]]},
                 {**self.build, "command": [self.build["command"][0], "--main-pack"]}]
        with patch("worker.prepare_probe.probe_desktop") as desktop:
            for build in cases:
                with self.subTest(build=build):
                    response = await self.probe(build)
                    self.assertEqual(response.status_code, 200)
                    self.assertFalse(response.json()["ready"])
                    self.assertFalse(response.json()["independent_desktop"])
                    self.assertIsNone(response.json()["build"])
                    self.assertFalse(response.json()["checks"]["build"]["ready"])
                    self.assertTrue(response.json()["detail"])
            desktop.assert_not_called()
        Path(self.build["command"][2]).write_bytes(b"changed fixture")
        self.assertIn("artifact_sha256", (await self.probe()).json()["detail"])

    async def test_mounted_manifest_versions_are_not_request_assertions(self):
        manifest = json.loads(self.manifest_path.read_text())
        manifest["integration_version"] = 1
        self.manifest_path.write_text(json.dumps(manifest))
        self.build["manifest_sha256"] = sha256_file(self.manifest_path)
        with patch("worker.prepare_probe.probe_desktop") as desktop:
            result = (await self.probe()).json()
        self.assertFalse(result["ready"])
        self.assertIn("integration_version", result["detail"])
        desktop.assert_not_called()

    async def test_desktop_failure_and_partial_positive_are_truthful(self):
        for native in (NativeDouble(image_available=False), NativeDouble(input_available=False)):
            with patch("worker.prepare_probe.probe_desktop", side_effect=lambda desktop: native.probe()):
                result = (await self.probe()).json()
            self.assertFalse(result["ready"])
            self.assertIsNotNone(result["build"])
            self.assertFalse(result["desktop"]["input_ready"])
            self.assertTrue(result["detail"])

    async def test_stored_steam_record_cannot_attest_fresh_license(self):
        private = self.runtime.root / "private"
        private.mkdir()
        (private / "steam-verification.json").write_text(json.dumps({"verified": True, "license_verified": True, "app_id": 480, "steam_id": "76561190000000001"}))
        with patch("worker.prepare_probe.probe_desktop", side_effect=lambda desktop: NativeDouble().probe()):
            result = (await self.probe()).json()
        self.assertTrue(result["ready"])
        self.assertFalse(result["steam"]["authenticated"])
        self.assertFalse(result["steam"]["license_verified"])
        self.assertNotIn("last_verification", result["steam"])
        self.assertNotIn("app_id", result["steam"])

    async def test_repeated_probes_do_not_rehash_unchanged_build(self):
        with patch("worker.prepare_probe.sha256_file", wraps=sha256_file) as hashed, patch("worker.prepare_probe.probe_desktop", side_effect=lambda desktop: NativeDouble().probe()):
            for _ in range(3):
                self.assertTrue((await self.probe()).json()["ready"])
            first = hashed.call_count
            self.assertLessEqual(first, 3)  # manifest, engine and pack at most once each
            Path(self.build["command"][2]).write_bytes(b"changed fixture with another size")
            self.assertFalse((await self.probe()).json()["ready"])
            self.assertEqual(hashed.call_count, first + 1)

    async def test_hashing_does_not_hold_lifecycle_lock(self):
        from worker.prepare_probe import measure_build
        held = []
        def observed(build):
            held.append(self.runtime.state_lock.locked())
            return measure_build(build)
        with patch("worker.prepare_probe.measure_build", side_effect=observed), patch("worker.prepare_probe.probe_desktop", side_effect=lambda desktop: NativeDouble().probe()):
            self.assertTrue((await self.probe()).json()["ready"])
        self.assertEqual(held, [False])

    async def test_cancelled_probe_keeps_lifecycle_lock_until_reads_finish(self):
        from worker.prepare_probe import prepare_probe
        import threading
        entered, release = threading.Event(), threading.Event()
        def blocked(desktop):
            entered.set()
            release.wait(5)
            return NativeDouble().probe()
        with patch("worker.prepare_probe.probe_desktop", side_effect=blocked):
            task = asyncio.create_task(prepare_probe(self.runtime, self.build))
            await asyncio.to_thread(entered.wait, 5)
            self.assertTrue(entered.is_set())
            task.cancel()
            try:
                self.assertTrue(self.runtime.state_lock.locked())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(self.runtime.state_lock.locked())


if __name__ == "__main__":
    unittest.main()
