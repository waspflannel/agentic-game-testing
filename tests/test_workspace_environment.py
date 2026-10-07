"""Fixture orchestration only: no real keyring, Docker, provisioning, or game."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ashad.repository import sha256_file
from ashad.store import Store
from ashad.workspace_environment import CONTRACT, IMAGE, inspect_environment, inspect_fresh_environment, prepare_environment


class MemoryCredentials:
    def __init__(self):
        self.values, self.saves = {}, []

    def get(self, key):
        return self.values.get(key)

    def save(self, key, value):
        self.values[key] = value
        self.saves.append(key)


def probe(build, worker_id="fixture-1"):
    return {"ready": True, "contract_version": CONTRACT, "worker_id": worker_id, "independent_desktop": True,
            "build": {key: build[key] for key in ("artifact_sha256", "engine_sha256", "manifest_sha256", "engine_version", "source_revision", "command", "structured_version", "integration_version") if key in build},
            "desktop": {"rendered": True, "capture_ready": True, "input_ready": True},
            "lifecycle": "unbound", "binding": None, "game_running": False, "world_may_continue": False,
            "human_control": False, "held_keys": [], "held_buttons": [], "infrastructure_error": None,
            "steam": {"setup_active": False, "requires_human_setup": True}}


class FakeWorker:
    def __init__(self, owner, url, token):
        assert token == owner.secret
        self.owner = owner
        self.url = url

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def health(self):
        self.owner.calls.append("health")
        if self.owner.unreachable:
            raise RuntimeError(self.owner.secret)
        return deepcopy(self.owner.snapshot)

    async def status(self):
        self.owner.calls.append("status")
        return deepcopy(self.owner.snapshot)

    async def request(self, method, path, *, data):
        assert (method, path) == ("POST", "/prepare/probe")
        assert data["build"]["artifact_sha256"] == self.owner.build["artifact_sha256"]
        self.owner.calls.append("probe")
        return deepcopy(self.owner.snapshot)


class FakeDocker:
    def __init__(self, build):
        self.build = build
        self.argv, self.options, self.calls = [], [], []
        self.secret = "fixture-secret-" * 4
        self.snapshot = probe(build)
        self.unreachable = False
        self.existing = None
        self.collision = False
        self.fail_script = False
        self.linux = True
        self.scripts = 0
        self.recovery = None
        self.image_available = True
        self.builds = []
        self.fail_build = False

    def client(self, url, token):
        return FakeWorker(self, url, token)

    def run(self, argv, **options):
        self.argv.append(argv)
        self.options.append(options)
        result = subprocess.CompletedProcess(argv, 0, "", "")
        if "-File" in argv:
            self.scripts += 1
            def argument(name):
                return argv[argv.index(name) + 1]
            worker_id = argument("-WorkerPrefix") + "-1"
            self.snapshot["worker_id"] = worker_id
            secrets = Path(argument("-SecretsRoot")) / worker_id
            secrets.mkdir(parents=True, exist_ok=True)
            (secrets / "worker_token").write_text(self.secret)
            (secrets / "vnc_password").write_text("human-only-password")
            api = int(argument("-ApiBasePort")) + 1
            desktop = int(argument("-DesktopBasePort")) + 1
            entry = {"id": worker_id, "container": "ashad-" + worker_id, "url": f"http://127.0.0.1:{api}",
                     "desktop_url": f"http://127.0.0.1:{desktop}/vnc.html", "token_file": str(secrets / "worker_token"),
                     "vnc_password_file": str(secrets / "vnc_password")}
            Path(argument("-RegistrationFile")).write_text(json.dumps([entry]), encoding="utf-8")
            self.existing = {"Config": {"Image": argument("-Image"), "Labels": {"ashad.managed": "true", "ashad.environment-owner": argument("-ManagedOwner")}},
                             "State": {"Running": True}, "Mounts": [
                                 {"Destination": "/game", "Source": argument("-BuildPath"), "RW": False},
                                 {"Destination": "/run/secrets", "Source": str(secrets), "RW": False},
                                 {"Destination": "/data", "Name": "ashad-" + worker_id + "-data"},
                                 {"Destination": "/home/player", "Name": "ashad-" + worker_id + "-home"}]}
            if self.fail_script:
                result.returncode, result.stderr = 1, self.secret
        elif argv[1:3] == ["info", "--format"]:
            result.stdout = "linux" if self.linux else "windows"
        elif argv[1:3] == ["image", "ls"]:
            if self.image_available:
                result.stdout = argv[argv.index("--filter") + 1].removeprefix("reference=")
        elif argv[1] == "build":
            self.builds.append(argv)
            self.image_available = not self.fail_build
            if self.fail_build:
                result.returncode, result.stderr = 1, self.secret
        elif argv[1:3] == ["image", "inspect"]:
            result.stdout = "[{}]"
        elif argv[1:3] == ["container", "ls"]:
            if self.existing or self.collision:
                result.stdout = argv[argv.index("--filter") + 1].removeprefix("name=^/").removesuffix("$")
        elif argv[1:3] == ["container", "inspect"]:
            result.stdout = json.dumps([self.existing])
        elif argv[1] == "cp":
            if self.recovery is None:
                result.returncode = 1
            else:
                Path(argv[-1]).write_text(json.dumps(self.recovery))
        else:
            raise AssertionError("Unexpected fake subprocess operation")
        return result


class EnvironmentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ashad environment ")
        base = Path(self.temporary.name)
        self.resources = base / "private Python site 空间" / "site-packages"
        (self.resources / "scripts").mkdir(parents=True)
        (self.resources / "worker").mkdir()
        (self.resources / "scripts/provision-workers.ps1").write_text("fixture resource; never executed")
        (self.resources / "worker/Dockerfile").write_text("fixture image resource")
        self.build_root = base / "game build 日本語"
        (self.build_root / "runtime").mkdir(parents=True)
        (self.build_root / "runtime/game").write_bytes(b"fixture executable")
        (self.build_root / "game.pck").write_bytes(b"fixture pinned pack")
        manifest = self.build_root / "manifest.json"
        self.build = {"id": "build-1", "project_id": "project-1", "manifest_path": str(manifest),
                      "source_revision": "pinned-fixture", "structured_version": 1, "integration_version": 2,
                      "engine_version": "GodotSteam 4.5.1 / 4.17 / Steamworks 1.63",
                      "command": ["/game/runtime/game", "--main-pack", "/game/game.pck"],
                      "artifact_sha256": sha256_file(self.build_root / "game.pck"),
                      "engine_sha256": sha256_file(self.build_root / "runtime/game"), "steam_app_id": 3753020}
        manifest.write_text(json.dumps(self.build), encoding="utf-8")
        self.build["manifest_sha256"] = sha256_file(manifest)
        self.store = Store(base / "writable user data 用户")
        self.credentials = MemoryCredentials()
        self.fake = FakeDocker(self.build)
        self.tools = patch("ashad.workspace_environment._tools", return_value=(Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"), Path(r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"), {"PATH": r"C:\Windows\System32;C:\Windows"}))
        self.tools.start()

    def tearDown(self):
        self.tools.stop()
        self.store.db.close()
        self.temporary.cleanup()

    async def prepare(self, **changes):
        kwargs = dict(project_id="project-1", build=self.build, request_id="request-1", confirmed=True,
                      target_drained=True, resource_root=self.resources, runner=self.fake.run, client_factory=self.fake.client)
        kwargs.update(changes)
        return await prepare_environment(self.store, self.credentials, **kwargs)

    def test_positive_probes_and_actual_idle_required(self):
        snapshot = probe(self.build)
        worker = {"id": "fixture-1"}
        self.assertTrue(inspect_environment(self.build, worker, snapshot, snapshot)["ready"])
        for field, value in (("binding", {"run_id": "old"}), ("game_running", True), ("human_control", True), ("held_keys", ["W"]), ("world_may_continue", True), ("lifecycle", "ended")):
            with self.subTest(field=field):
                broken = {**snapshot, field: value}
                self.assertFalse(inspect_environment(self.build, worker, snapshot, broken)["ready"])
        ended = {**snapshot, "lifecycle": "ended", "teardown_confirmed": True, "binding": {"run_id": "past"}}
        self.assertTrue(inspect_environment(self.build, worker, ended, ended)["ready"])
        self.assertFalse(inspect_environment(self.build, worker, snapshot, snapshot, occupied=True)["ready"])

    def test_missing_hashes_unsupported_contract_or_desktop_never_ready(self):
        snapshot = probe(self.build)
        worker = {"id": "fixture-1", "last_probe": snapshot}
        cases = [None, {}, {**snapshot, "build": None}, {**snapshot, "build": {**snapshot["build"], "artifact_sha256": "0" * 64}},
                 {**snapshot, "contract_version": "future"}, {**snapshot, "worker_id": "unrelated"},
                 {**snapshot, "independent_desktop": False}, {**snapshot, "desktop": {"rendered": True}},
                 {**snapshot, "steam": {"setup_active": True}}]
        for case in cases:
            with self.subTest(case=case):
                self.assertFalse(inspect_environment(self.build, worker, case, case)["ready"])
        self.assertFalse(inspect_environment({**self.build, "artifact_sha256": None}, worker, snapshot, snapshot)["ready"])

    def test_steam_requires_live_license_identity_real_build_app_not_stale_account(self):
        snapshot = probe(self.build)
        worker = {"id": "fixture-1"}
        account = {"worker_id": "fixture-1", "steam_id": "76561190000000001", "app_id": 3753020, "readiness": "ready"}
        self.assertFalse(inspect_environment(self.build, worker, snapshot, snapshot, account=account)["steam"]["ready"])
        snapshot["steam"].update(authenticated=True, identity_verified=True, app_id_verified=True, license_verified=True,
                                 steam_id=account["steam_id"], app_id=3753020, requires_human_setup=False)
        self.assertTrue(inspect_environment(self.build, worker, snapshot, snapshot, account=account)["steam"]["ready"])
        for field, value in (("license_verified", False), ("steam_id", "another"), ("app_id", 480), ("setup_active", True)):
            broken = deepcopy(snapshot)
            broken["steam"][field] = value
            self.assertFalse(inspect_environment(self.build, worker, broken, broken, account=account)["steam"]["ready"])
        self.assertFalse(inspect_environment({**self.build, "steam_app_id": 480}, worker, snapshot, snapshot, account=account)["steam"]["ready"])

    async def test_confirmed_drain_and_stable_payload_before_operations(self):
        for changes in ({"confirmed": False}, {"confirmed": 1}, {"target_drained": False}, {"request_id": "../escape"}, {"project_id": "different"}):
            with self.assertRaises(ValueError):
                await self.prepare(**changes)
        self.assertEqual(self.fake.argv, [])
        self.assertEqual(self.credentials.saves, [])
        self.assertEqual(self.store.list("environment_preparations"), [])

    async def test_orchestration_packaged_paths_argv_and_backend_only_secrets(self):
        result = await self.prepare()
        self.assertTrue(result["environment"]["ready"])
        self.assertEqual(result["status"], "completed")
        argv = next(argv for argv in self.fake.argv if "-File" in argv)
        self.assertEqual(argv[argv.index("-Count") + 1], "1")
        self.assertIn("-SkipBuild", argv)
        self.assertNotIn("-Recreate", argv)
        self.assertEqual(argv[argv.index("-BuildPath") + 1], str(self.build_root))
        self.assertEqual(argv[argv.index("-File") + 1], str(self.resources / "scripts/provision-workers.ps1"))
        self.assertTrue(result["worker_id"].startswith("workspace-"))
        for option in self.fake.options:
            self.assertFalse(option["shell"])
            self.assertNotIn("Python", option["env"]["PATH"])
            self.assertTrue(Path(option["cwd"]).is_relative_to(self.store.root))
        public_and_store = json.dumps(result) + json.dumps(self.store.list("workers")) + json.dumps(self.store.list("environment_preparations"))
        self.assertNotIn(self.fake.secret, public_and_store)
        self.assertNotIn("human-only-password", public_and_store)
        self.assertNotIn("token_file", public_and_store)
        self.assertEqual(self.fake.calls, ["health", "status", "probe"])

    async def test_completed_retry_has_no_resources_credentials_or_worker_calls(self):
        first = await self.prepare()
        counts = len(self.fake.argv), len(self.credentials.saves), len(self.fake.calls)
        # Completed reconciliation does not depend on installation/build still existing.
        (self.build_root / "game.pck").write_bytes(b"changed after completion")
        self.assertEqual(await self.prepare(), first)
        self.assertEqual(counts, (len(self.fake.argv), len(self.credentials.saves), len(self.fake.calls)))
        for field, value in (("artifact_sha256", "0" * 64), ("manifest_path", str(self.build_root / "other.json"))):
            with self.assertRaisesRegex(ValueError, "rebound"):
                await self.prepare(build={**self.build, field: value})

    async def test_script_or_docker_success_without_probes_is_not_ready(self):
        self.fake.snapshot["build"] = None
        self.fake.snapshot.pop("desktop")
        result = await self.prepare()
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["environment"]["ready"])
        self.assertFalse(result["environment"]["checks"]["artifact_sha256"]["ready"])

    async def test_unreachable_probe_is_truthful_and_secret_safe(self):
        self.fake.unreachable = True
        result = await self.prepare()
        self.assertFalse(result["environment"]["ready"])
        self.assertNotIn(self.fake.secret, json.dumps(result))
        self.assertFalse(result["environment"]["checks"]["reachable"]["ready"])

    async def test_failure_metadata_retry_only_own_matching_idle_container(self):
        self.fake.fail_script = True
        with self.assertRaises(ValueError) as raised:
            await self.prepare()
        self.assertNotIn(self.fake.secret, str(raised.exception))
        record = self.store.require("environment_preparations", "request-1")
        self.assertEqual(record["status"], "error")
        self.fake.fail_script = False
        result = await self.prepare()
        self.assertEqual(result["worker_id"], record["worker_id"])
        self.assertEqual(self.fake.scripts, 2)

    async def test_busy_or_unrelated_matching_name_is_not_started_adopted_or_recreated(self):
        self.fake.collision = True
        with self.assertRaises(ValueError):
            await self.prepare()
        self.assertEqual(self.fake.scripts, 0)
        self.fake.collision = False
        self.fake.fail_script = True
        with self.assertRaises(ValueError):
            await self.prepare()
        self.fake.fail_script = False
        self.fake.snapshot["human_control"] = True
        with self.assertRaises(ValueError):
            await self.prepare()
        self.assertEqual(self.fake.scripts, 1)
        self.fake.snapshot["human_control"] = False
        self.fake.existing["Config"]["Labels"]["ashad.environment-owner"] = "unrelated"
        with self.assertRaises(ValueError):
            await self.prepare()
        self.assertEqual(self.fake.scripts, 1)

    async def test_stopped_own_unbound_resource_can_retry_but_unknown_or_bound_cannot(self):
        self.fake.fail_script = True
        with self.assertRaises(ValueError):
            await self.prepare()
        self.fake.fail_script = False
        self.fake.existing["State"]["Running"] = False
        with self.assertRaises(ValueError):
            await self.prepare()
        record = self.store.require("environment_preparations", "request-1")
        self.fake.recovery = {"contract_version": CONTRACT, "worker_id": record["worker_id"], "lifecycle": "unbound", "binding": {"run_id": "unresolved"},
                              "human_control": False, "desktop_pids": [], "held_keys": [], "held_buttons": []}
        with self.assertRaises(ValueError):
            await self.prepare()
        self.fake.recovery["binding"] = None
        self.assertEqual((await self.prepare())["worker_id"], record["worker_id"])

    async def test_reuse_known_compatible_idle_environment_without_provisioning(self):
        worker = {"id": "fixture-1", "url": "http://127.0.0.1:9999", "enabled": True}
        self.store.put("workers", worker)
        self.credentials.save("worker:fixture-1", self.fake.secret)
        self.assertEqual((await self.prepare())["worker_id"], "fixture-1")
        self.assertEqual(self.fake.argv, [])
        self.assertEqual(self.fake.calls, ["health", "status", "probe"])
        self.assertEqual(self.credentials.saves, ["worker:fixture-1"])

    async def test_changed_artifact_missing_resource_and_installation_writes_refused(self):
        with self.assertRaises(ValueError):
            await self.prepare(resource_root=self.resources.parent / "missing")
        with self.assertRaises(ValueError):
            await self.prepare(resource_root=self.store.root.parent)
        (self.build_root / "game.pck").write_bytes(b"tampered")
        with self.assertRaises(ValueError):
            await self.prepare()
        self.assertEqual(self.fake.argv, [])

    async def test_supported_producer_prerequisites_fail_before_side_effects(self):
        for key in ("engine_sha256", "artifact_sha256", "manifest_sha256", "engine_version", "source_revision"):
            build = {field: value for field, value in self.build.items() if field != key}
            with self.assertRaisesRegex(ValueError, "supported"):
                await self.prepare(build=build)
            self.assertFalse(inspect_environment(build, {"id": "fixture-1"}, self.fake.snapshot, self.fake.snapshot)["ready"])
        self.assertEqual(self.fake.argv, [])
        self.assertEqual(self.credentials.saves, [])
        self.assertEqual(self.store.list("environment_preparations"), [])
        self.assertFalse((self.store.root / "environments").exists())

    async def test_missing_image_builds_only_confirmed_content_addressed_bundled_context(self):
        self.fake.image_available = False
        result = await self.prepare()
        self.assertTrue(result["environment"]["ready"])
        self.assertEqual(len(self.fake.builds), 1)
        argv = self.fake.builds[0]
        self.assertEqual(argv[-1], str(self.resources / "worker"))
        tag = argv[argv.index("--tag") + 1]
        self.assertRegex(tag, r"^ashad-workspace-worker:[a-f0-9]{64}$")
        self.assertNotEqual(tag, "ashad-worker:local")
        script = next(argv for argv in self.fake.argv if "-File" in argv)
        self.assertEqual(script[script.index("-Image") + 1], tag)
        self.assertEqual(self.store.require("environment_preparations", "request-1")["image"], tag)

    async def test_existing_compatible_image_no_build_and_failed_build_is_recoverable(self):
        await self.prepare()
        self.assertEqual(self.fake.builds, [])
        self.fake.image_available = False
        self.fake.fail_build = True
        self.fake.snapshot["desktop"]["rendered"] = False
        with self.assertRaisesRegex(ValueError, "image failed") as raised:
            await self.prepare(request_id="request-2")
        self.assertNotIn(self.fake.secret, str(raised.exception))
        self.assertEqual(self.store.require("environment_preparations", "request-2")["status"], "error")

    async def test_resource_version_change_does_not_rebind_incomplete_request(self):
        self.fake.fail_script = True
        with self.assertRaises(ValueError):
            await self.prepare()
        counts = len(self.fake.argv), len(self.credentials.saves)
        (self.resources / "worker/Dockerfile").write_text("changed supported context")
        with self.assertRaisesRegex(ValueError, "version changed"):
            await self.prepare()
        self.assertEqual(counts, (len(self.fake.argv), len(self.credentials.saves)))

    async def test_prepared_worker_registration_is_used_by_actual_coordinator_client(self):
        from ashad.coordinator import Coordinator
        result = await self.prepare()
        worker = self.store.require("workers", result["worker_id"])
        self.assertEqual(worker["credential_ref"], "worker:" + result["worker_id"])
        self.assertNotIn("token_ref", worker)
        coordinator = Coordinator(self.store, self.credentials, object())
        client = coordinator.client(worker["id"])
        self.assertEqual(client._client.headers["Authorization"], "Bearer " + self.fake.secret)
        self.assertEqual(client.base_url, worker["url"])
        await client.close()

    async def test_existing_worker_credential_ref_takes_precedence_over_legacy_alias(self):
        worker = {"id": "fixture-1", "url": "http://127.0.0.1:9999", "credential_ref": "legacy-worker-service", "token_ref": "obsolete"}
        self.store.put("workers", worker)
        self.credentials.save("legacy-worker-service", self.fake.secret)
        self.assertEqual((await self.prepare())["worker_id"], "fixture-1")
        self.assertEqual(self.fake.argv, [])
        self.assertEqual(self.fake.calls, ["health", "status", "probe"])

    async def test_malformed_remote_snapshots_never_become_ready(self):
        for malformed in ([], "secret-shaped-response", {"build": [], "desktop": [], "steam": []}):
            self.assertFalse(inspect_environment(self.build, {"id": "fixture-1"}, malformed, malformed)["ready"])

    async def test_private_registration_cannot_read_or_persist_external_credentials(self):
        external = self.store.root.parent / "unrelated-token"
        external.write_text("never-read-this")
        def poisoned(argv, **options):
            result = self.fake.run(argv, **options)
            if "-File" in argv:
                registration = Path(argv[argv.index("-RegistrationFile") + 1])
                entry = json.loads(registration.read_text())[0]
                entry["vnc_password_file"] = str(external)
                registration.write_text(json.dumps([entry]))
            return result
        with self.assertRaises(ValueError):
            await self.prepare(runner=poisoned)
        self.assertEqual(self.credentials.saves, [])
        self.assertEqual(self.store.list("workers"), [])
        self.assertEqual(external.read_text(), "never-read-this")
        self.assertNotIn("never-read-this", json.dumps(self.store.list("environment_preparations")))

    async def test_native_tools_and_path_are_not_taken_from_renderer_or_global_runtime(self):
        self.tools.stop()
        try:
            from ashad.workspace_environment import _tools
            with patch.dict("os.environ", {"SystemRoot": r"C:\Windows", "ProgramFiles": r"C:\Program Files", "PATH": r"C:\global Python;C:\global Node"}), patch("pathlib.Path.is_file", return_value=True), patch("ashad.workspace_environment._safe_path", side_effect=Path):
                powershell, docker, env = _tools()
            self.assertEqual(powershell, Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"))
            self.assertEqual(docker, Path(r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"))
            self.assertNotIn("global Python", env["PATH"])
            self.assertNotIn("global Node", env["PATH"])
        finally:
            self.tools.start()

    async def test_authenticated_worker_client_uses_actual_passive_probe_endpoint(self):
        import httpx
        from types import SimpleNamespace
        from ashad.worker_client import WorkerClient
        from worker.app import create_app
        service = SimpleNamespace(closed=False, binding=None, lifecycle="unbound", game_running=False,
            termination_result=None, human_control=False, desktop_session=None, steam_setup=False,
            setup_processes=[], held_keys=set(), held_buttons=set(), infrastructure_error=None,
            state_lock=asyncio.Lock(), desktop=object(), status=lambda: deepcopy(self.fake.snapshot))
        transport = httpx.ASGITransport(app=create_app(service, token=self.fake.secret))
        def client(url, token):
            return WorkerClient(url, token, transport=transport)
        with patch("worker.prepare_probe.measure_build", return_value=probe(self.build)["build"]), patch("worker.prepare_probe.probe_desktop", return_value={**probe(self.build)["desktop"], "checks": {}}):
            result = await self.prepare(client_factory=client)
        self.assertTrue(result["environment"]["ready"])
        # Legacy /health alone cannot prove the newly required passive endpoint.
        self.fake.snapshot.pop("desktop")
        self.fake.snapshot["build"] = None
        self.assertFalse(inspect_environment(self.build, {"id": result["worker_id"]}, self.fake.snapshot, self.fake.snapshot)["ready"])

    async def test_cancellation_waits_child_settlement_and_retains_unknown_metadata(self):
        import threading
        entered, released = threading.Event(), threading.Event()
        def slow(argv, **options):
            if "-File" in argv:
                entered.set()
                released.wait(5)
            return self.fake.run(argv, **options)
        task = asyncio.create_task(self.prepare(runner=slow))
        await asyncio.to_thread(entered.wait, 5)
        self.assertTrue(entered.is_set())
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        released.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.store.require("environment_preparations", "request-1")["status"], "unknown")
        self.assertEqual(self.fake.scripts, 1)


class ProvisionScriptTests(unittest.TestCase):
    def test_windows_powershell_legacy_defaults_and_private_output_options(self):
        script = (Path(__file__).resolve().parents[1] / "scripts/provision-workers.ps1").read_text()
        for expected in ("$Count = 4", "$WorkerPrefix = 'worker'", "$DockerExecutable = 'docker'", "$NetworkName = 'ashad-private'",
                         "$SecretsRoot = ''", "$RegistrationFile = ''", "RandomNumberGenerator]::Create()", "$rng.GetBytes", "ashad.environment-owner", "volume create --label"):
            self.assertIn(expected, script)
        self.assertNotIn("RandomNumberGenerator]::Fill", script)
        self.assertNotIn("& docker", script)
