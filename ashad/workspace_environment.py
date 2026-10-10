"""Passive environment checks and confirmed, request-owned Docker preparation.

Caller owns the controller process lock, coord.lock and global setup/provider/
instance drain throughout preparation (including cancellation settlement). No
worker binding, game launch, teardown, inference or Steam verification is done.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
from secrets import token_bytes
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time

from .repository import sha256_file
from .worker_client import WorkerClient

CONTRACT = "ashad-worker/1"
IMAGE = "ashad-workspace-worker"
ENGINE = "GodotSteam 4.5.1 / 4.17 / Steamworks 1.63"
STARTUP_SECONDS = 30


class PreparationError(ValueError):
    """Only backend-authored, credential-free messages may use this type."""


def _object(value):
    return value if isinstance(value, dict) else {}


def _result(checks):
    failures = [value["reason"] for value in checks.values() if not value["ready"]]
    return {"ready": not failures, "reason": "; ".join(failures) if failures else "Verified by fresh environment probes", "checks": checks}


def inspect_environment(build, worker, health, status, *, occupied=False, account=None):
    """Consume a fresh authenticated /prepare/probe result, /status and validated build.

    The health argument is the flattened passive probe (Runtime.status plus
    measured build/desktop), NOT registration metadata or legacy /health alone.
    Unknown/missing probes fail. Never use last_probe or account.readiness.
    """
    checks = {}

    def check(key, ready, reason):
        checks[key] = {"ready": bool(ready), "reason": "verified" if ready else reason}

    reachable = isinstance(health, dict) and isinstance(status, dict)
    health, status = _object(health), _object(status)
    snapshots = (health, status)
    measured = (health,)
    check("reachable", reachable, "Authenticated passive probe/status unavailable")
    check("probe", health.get("ready") is True, "Worker passive compatibility probe failed")
    check("enabled", worker.get("enabled", True) is True, "Desktop is disabled")
    check("identity", all(probe.get("worker_id") == worker.get("id") and worker.get("id") for probe in snapshots), "Worker identity is unknown or mismatched")
    check("controller", all(probe.get("contract_version") == CONTRACT for probe in snapshots), "Worker controller contract is unknown or unsupported")
    check("independent", all(probe.get("independent_desktop") is True for probe in snapshots), "Independent desktop is not positively verified")
    pins = [(key, build.get(key)) for key in ("artifact_sha256", "engine_sha256", "manifest_sha256")]
    for key, expected in pins:
        check(key, bool(isinstance(expected, str) and re.fullmatch(r"[a-fA-F0-9]{64}", expected) and
              all(_object(probe.get("build")).get(key) == expected.lower() for probe in measured)), f"Exact measured {key} is missing or mismatched")
    for key, supported in (("structured_version", 1), ("integration_version", 2), ("engine_version", ENGINE)):
        check(key, type(build.get(key)) is type(supported) and build.get(key) == supported and all(type(_object(probe.get("build")).get(key)) is type(supported) and _object(probe.get("build")).get(key) == supported for probe in measured), f"{key} compatibility is unknown or unsupported")
    for key in ("source_revision", "command"):
        check(key, bool(build.get(key)) and _object(health.get("build")).get(key) == build.get(key), f"Mounted {key} is missing or mismatched")
    for key in ("rendered", "capture_ready", "input_ready"):
        check(key, all(_object(probe.get("desktop")).get(key) is True for probe in measured), f"Independent desktop {key} probe is not positive")
    check("idle", not occupied and all(_idle(probe) for probe in snapshots), "Desktop is busy, in human setup, or teardown/idle state is unknown")
    check("infrastructure", all("infrastructure_error" in probe and not probe["infrastructure_error"] for probe in snapshots), "Worker infrastructure state is unknown or unhealthy")

    steam_checks = {}
    def steam_check(key, ready, reason):
        steam_checks[key] = {"ready": bool(ready), "reason": "verified" if ready else reason}
    app_id = build.get("steam_app_id")
    steam_check("app_id", type(app_id) is int and app_id > 0 and app_id != 480, "Build requires a real licensed Steam App ID (never 480)")
    steam_check("account", bool(account and account.get("worker_id") == worker.get("id") and account.get("steam_id") and account.get("app_id") == app_id), "No matching desktop/account/build App ID")
    for key in ("authenticated", "identity_verified", "app_id_verified", "license_verified"):
        steam_check(key, all(_object(probe.get("steam")).get(key) is True for probe in snapshots), f"Fresh Steam {key} is not proven")
    steam_check("identity", bool(account and all(str(_object(probe.get("steam")).get("steam_id")) == str(account.get("steam_id")) for probe in snapshots)), "Actual Steam identity does not match the licensed account")
    steam_check("actual_app_id", type(app_id) is int and app_id != 480 and all(_object(probe.get("steam")).get("app_id") == app_id for probe in snapshots), "Actual Steam App ID does not match this build")
    steam_check("setup", all(_object(probe.get("steam")).get("setup_active") is False and _object(probe.get("steam")).get("requires_human_setup") is False for probe in snapshots), "Steam requires human setup or its setup state is unknown")
    steam_check("environment", all(value["ready"] for value in checks.values()), "Desktop environment is not ready")
    return {"worker_id": worker.get("id"), "checked_at": time.time(), **_result(checks), "steam": _result(steam_checks)}


def _idle(probe):
    return (probe.get("lifecycle") in {"unbound", "ended"} and
            ((probe.get("lifecycle") == "unbound" and "binding" in probe and probe["binding"] is None) or
             (probe.get("lifecycle") == "ended" and probe.get("teardown_confirmed") is True)) and
            all(probe.get(key) is False for key in ("game_running", "world_may_continue", "human_control")) and
            probe.get("held_keys") == [] and probe.get("held_buttons") == [] and
            _object(probe.get("steam")).get("setup_active") is False)


async def inspect_fresh_environment(build, worker, credentials, *, occupied=False, account=None, client_factory=WorkerClient):
    """Fresh health/status plus passive mounted-build/desktop probe, never game launch."""
    health = status = None
    try:
        token = await asyncio.to_thread(credentials.get, worker.get("credential_ref") or worker.get("token_ref") or "worker:" + worker["id"])
        if token:
            async with client_factory(worker["url"], token) as client:
                health, status = await client.health(), await client.status()
                if health.get("worker_id") == worker["id"] and health.get("contract_version") == CONTRACT and _idle(status):
                    health = await client.request("POST", "/prepare/probe", data={"build": build})
    except Exception:
        health = None
    return inspect_environment(build, worker, health, status, occupied=occupied, account=account)


def _safe_path(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise PreparationError("An absolute non-traversing path is required")
    for ancestor in (*reversed(path.parents), path):
        try:
            info = ancestor.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT or (stat.S_ISREG(info.st_mode) and info.st_nlink > 1):
            raise PreparationError("Linked resource/credential paths are not allowed")
    return path.resolve()


def validate_build(build):
    """Recheck exact local artifacts, even when the API already validated import."""
    if any(not isinstance(build.get(key), str) or not re.fullmatch(r"[a-fA-F0-9]{64}", build[key]) for key in ("engine_sha256", "artifact_sha256", "manifest_sha256")):
        raise PreparationError("Unsupported legacy build: re-register a supported build with exact engine, artifact and manifest SHA256 pins before preparing a desktop")
    if type(build.get("structured_version")) is not int or build["structured_version"] != 1 or type(build.get("integration_version")) is not int or build["integration_version"] != 2 or build.get("engine_version") != ENGINE or not build.get("source_revision"):
        raise PreparationError("Only the supported pinned GodotSteam structured integration (1/2) can be prepared; re-register a supported build")
    manifest = _safe_path(build["manifest_path"])
    root = manifest.parent
    if not manifest.is_file() or manifest.stat().st_size > 2_000_000 or sha256_file(manifest) != build["manifest_sha256"].lower():
        raise PreparationError("Pinned build manifest is missing or changed; re-register the supported build")
    try:
        mounted = json.loads(manifest.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise PreparationError("Pinned build manifest is unreadable or invalid") from None
    if not isinstance(mounted, dict) or any(type(mounted.get(key)) is not type(build.get(key)) or mounted.get(key) != build.get(key) for key in ("command", "source_revision", "structured_version", "integration_version", "engine_version")) or any(str(mounted.get(key)).lower() != build[key].lower() for key in ("engine_sha256", "artifact_sha256")):
        raise PreparationError("Pinned manifest contents do not match the validated supported build")
    command = build.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(value, str) for value in command) or command.count("--main-pack") != 1:
        raise PreparationError("Build requires the supported /game executable and main-pack command")
    try:
        pack = command[command.index("--main-pack") + 1]
    except IndexError:
        raise PreparationError("Build main-pack is missing") from None
    for runtime_path, key in ((command[0], "engine_sha256"), (pack, "artifact_sha256")):
        relative = PurePosixPath(runtime_path)
        if not relative.is_relative_to("/game") or ".." in relative.parts:
            raise PreparationError("Preparation only supports read-only /game build artifacts")
        path = _safe_path(root.joinpath(*relative.relative_to("/game").parts))
        expected = build.get(key)
        if not path.is_relative_to(root) or not path.is_file():
            raise PreparationError("Pinned build artifact is missing or changed")
        if key == "artifact_sha256" and path.parent != root:
            raise PreparationError("Supported game pack must share its mounted directory with the manifest")
        if sha256_file(path) != expected.lower():
            raise PreparationError("Pinned build artifact is missing or changed")
    for artifact in build.get("artifacts", []):
        path = _safe_path(artifact["path"])
        if not path.is_file() or not artifact.get("sha256") or sha256_file(path) != artifact["sha256"]:
            raise PreparationError("Pinned build artifact inventory is missing or changed")
    if any(char in str(root) for char in (",", "\n", "\r")):
        raise PreparationError("Build path cannot be represented safely as a Docker bind mount")
    return root


def _image(resources):
    """Content-address the approved bundled worker, never overwrite a legacy image."""
    context = resources / "worker"
    ignore = context / ".dockerignore"
    # Docker's root-anchored .dockerignore semantics (no "!" exceptions are used), so the tag hashes the actual build context.
    patterns = [line.strip().strip("/") for line in (ignore.read_text(encoding="utf-8").splitlines() if ignore.is_file() else ()) if line.strip() and not line.lstrip().startswith("#")]
    digest = hashlib.sha256()
    for path in sorted(context.rglob("*")):
        relative = path.relative_to(context)
        if any(PurePosixPath(*relative.parts[:depth]).full_match(pattern) for depth in range(1, len(relative.parts) + 1) for pattern in patterns):
            continue
        path = _safe_path(path)
        if path.is_file():
            digest.update(relative.as_posix().encode("utf-8") + b"\0" + bytes.fromhex(sha256_file(path)))
    return IMAGE + ":" + digest.hexdigest()


def _tools():
    if sys.platform == "linux":
        # Fixed install locations only: the inherited PATH must not choose which docker runs.
        docker = next((path.resolve() for path in (Path("/usr/bin/docker"), Path("/usr/local/bin/docker")) if path.is_file()), None)
        if docker is None:
            raise PreparationError("Docker Engine CLI is required at /usr/bin/docker or /usr/local/bin/docker")
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith("DOCKER_")}
        env["PATH"] = "/usr/bin:/bin"
        # No PowerShell on Linux: prepare_environment provisions in-process.
        return None, docker, env
    system = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    program = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    if not system.is_absolute() or not program.is_absolute():
        raise PreparationError("Known absolute Windows system/program directories are required")
    # Windows-serviced binaries may legitimately be hard-linked into WinSxS.
    powershell = (system / "System32/WindowsPowerShell/v1.0/powershell.exe").resolve()
    docker = (program / "Docker/Docker/resources/bin/docker.exe").resolve()
    if os.name != "nt" or not powershell.is_file() or not docker.is_file():
        raise PreparationError("Windows PowerShell 5.1 and official Docker Desktop CLI are required")
    # Native Windows tools only. No dependency on global Python/Node or shell lookup.
    # Ambient DOCKER_HOST/CONTEXT/CONFIG must not redirect owned resources to another engine.
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("DOCKER_")}
    env["PATH"] = os.pathsep.join(map(str, (system / "System32", system, powershell.parent, docker.parent)))
    return powershell, docker, env


def _execute(argv, *, cwd, env, shell=False, timeout):
    """No inherited pipes: Docker grandchildren cannot block a timed-out read; the whole tree (Windows) or process group (Linux) is killed."""
    with tempfile.TemporaryFile(dir=cwd) as output:
        process = subprocess.Popen(argv, cwd=cwd, env=env, shell=shell, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
                                   start_new_session=os.name != "nt")
        try:
            process.wait(timeout)
        except BaseException:
            if os.name == "nt":
                taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/taskkill.exe"
                subprocess.run([str(taskkill), "/T", "/F", "/PID", str(process.pid)], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False)
            else:
                # The child leads its own session, so its process group holds every descendant that didn't detach.
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            process.kill()
            process.wait()
            raise
        output.seek(0)
        return subprocess.CompletedProcess(argv, process.returncode, output.read().decode("utf-8", "replace"), "")


async def _run(runner, argv, root, env, *, timeout=120, check=True):
    def call():
        result = runner(list(map(str, argv)), cwd=str(root), env=env, shell=False, timeout=timeout)
        if result.returncode and not check:
            return None
        if result.returncode:
            # Script/CLI stderr can contain generated credentials. Never expose it.
            reason = "Docker preparation command failed; reconcile this request's retained resources"
            if len(argv) > 1 and str(argv[1]) == "build":
                reason = "Building the confirmed bundled worker image failed; check Docker connectivity and image-build prerequisites, then retry this request"
            elif list(map(str, argv[1:3])) == ["info", "--format"]:
                reason = ("Docker Desktop is unreachable; start Docker in Linux-container mode before retrying" if os.name == "nt"
                          else "Docker Engine is unreachable; start the Docker service and add this user to the docker group before retrying")
            raise PreparationError(reason)
        return result.stdout.strip()
    task = asyncio.create_task(asyncio.to_thread(call))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task  # Do not release the parent's drain while the child still runs.
        raise


def _ports():
    # ponytail: close reservations before Docker binds; collisions fail safely rather than replacing anything.
    with socket.socket() as api, socket.socket() as desktop:
        api.bind(("127.0.0.1", 0))
        desktop.bind(("127.0.0.1", 0))
        return api.getsockname()[1], desktop.getsockname()[1]


async def _import_worker(store, credentials, entry, record, directory):
    worker_id = record["worker_id"]
    if entry.get("id") != worker_id or entry.get("container") != record["container"] or entry.get("url") != record["url"] or entry.get("desktop_url") != record["desktop_url"]:
        raise PreparationError("Owned worker registration does not match the preparation request")
    previous = store.get("workers", worker_id)
    if previous and (previous.get("environment_owner") != record["owner"] or previous.get("build_id") != record["build_id"]):
        raise PreparationError("Worker registration is already bound to another environment")
    secrets = []
    for field, filename, reference in (("token_file", "worker_token", "worker:"), ("vnc_password_file", "vnc_password", "desktop:")):
        expected = _safe_path(directory / "secrets" / worker_id / filename)
        if _safe_path(entry[field]) != expected or not expected.is_file():
            raise PreparationError("Credential file is outside this request's private directory")
        secret = expected.read_text(encoding="utf-8-sig").strip()
        if not secret or (field == "token_file" and len(secret) < 24):
            raise PreparationError("Provisioned credential is missing or invalid")
        secrets.append((reference + worker_id, secret))
    def save():
        for reference, secret in secrets:
            if credentials.get(reference) != secret:
                credentials.save(reference, secret)
    await asyncio.to_thread(save)
    worker = {"id": worker_id, "name": "Project desktop", "url": record["url"], "desktop_url": record["desktop_url"],
              "credential_ref": "worker:" + worker_id,
              "host_address": record["container"], "container": record["container"], "enabled": True,
              "environment_owner": record["owner"], "project_id": record["project_id"], "build_id": record["build_id"]}
    store.put("workers", worker)
    return worker


async def _await_startup(worker, credentials, client_factory):
    """Bounded, cancellable wait for a just-started worker before the single readiness probe."""
    token = await asyncio.to_thread(credentials.get, worker["credential_ref"])
    deadline = time.monotonic() + STARTUP_SECONDS
    while time.monotonic() < deadline:
        try:
            async with client_factory(worker["url"], token) as client:
                if (await client.health()).get("worker_id") == worker["id"]:
                    return
        except Exception:
            pass
        await asyncio.sleep(0.5)


async def _provision(runner, docker, env, record, *, root, directory, owner, existing):
    """Linux in-process equivalent of provision-workers.ps1's request-owned path (-Count 1 -SkipBuild -ManagedOwner).

    prepare_environment has already verified an existing container's ownership, mounts and idle state.
    """
    worker_id, container = record["worker_id"], record["container"]
    secrets_root = directory / "secrets"
    secret_directory = secrets_root / worker_id
    secret_directory.mkdir(parents=True, exist_ok=True)
    # Only this user may read generated service credentials.
    secrets_root.chmod(0o700)
    secret_directory.chmod(0o700)
    token_file, vnc_file = secret_directory / "worker_token", secret_directory / "vnc_password"
    for path, size in ((token_file, 48), (vnc_file, 6)):
        if not path.exists():
            # Created owner-only so the secret never depends on the parent directory's mode.
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as secret:
                secret.write(base64.b64encode(token_bytes(size)).decode("ascii"))
    label = await _run(runner, [docker, "network", "inspect", "--format", '{{index .Labels "ashad.managed"}}', record["network"]], directory, env, check=False)
    if label is None:
        await _run(runner, [docker, "network", "create", "--label", "ashad.managed=true", record["network"]], directory, env)
    elif label != "true":
        raise PreparationError("Network collision; refusing an unrelated network")
    for volume in (f"ashad-{worker_id}-data", f"ashad-{worker_id}-home"):
        volume_owner = await _run(runner, [docker, "volume", "inspect", "--format", '{{index .Labels "ashad.environment-owner"}}', volume], directory, env, check=False)
        if volume_owner is None:
            await _run(runner, [docker, "volume", "create", "--label", "ashad.managed=true", "--label", "ashad.environment-owner=" + owner, volume], directory, env)
        elif volume_owner != owner:
            raise PreparationError("Persistent volume collision; refusing unrelated data")
    if existing:
        await _run(runner, [docker, "start", container], directory, env)
    else:
        await _run(runner, [docker, "run", "--pull", "never", "--label", "ashad.environment-owner=" + owner, "--detach", "--name", container,
                            "--label", "ashad.managed=true", "--network", record["network"], "--network-alias", container,
                            "--restart", "unless-stopped", "--init", "--shm-size", "1g", "--cpus", "3", "--memory", "4g",
                            "--publish", f"127.0.0.1:{record['api_port']}:8080", "--publish", f"127.0.0.1:{record['desktop_port']}:6080",
                            "--env", "ASHAD_WORKER_ID=" + worker_id, "--env", "ASHAD_EXECUTABLE_ROOTS=/game:/opt/runtime",
                            "--mount", f"type=bind,source={root},target=/game,readonly",
                            "--mount", f"type=bind,source={secret_directory},target=/run/secrets,readonly",
                            "--mount", f"type=volume,source=ashad-{worker_id}-data,target=/data",
                            "--mount", f"type=volume,source=ashad-{worker_id}-home,target=/home/player", record["image"]], directory, env)
    return {"id": worker_id, "container": container, "url": record["url"], "desktop_url": record["desktop_url"],
            "token_file": str(token_file), "vnc_password_file": str(vnc_file)}


async def prepare_environment(store, credentials, *, project_id, build, request_id,
                              confirmed=False, target_drained=False, resource_root=None,
                              runner=_execute, client_factory=WorkerClient):
    """Prepare one desktop, without starting a game; completed retries are read-only.

    Resources are owned by store/project/build, not request: a new request ID for the
    same build reconciles the same container, profile volumes and ports.

    target_drained MUST be a server assertion, never copied from renderer input.
    Status completed means provisioning finished, NOT that environment.ready is
    true. Error/unknown records retain resource identity for the same-ID retry.
    """
    if confirmed is not True or target_drained is not True:
        raise PreparationError("Explicit user confirmation and server-held target drain are required")
    for value in (project_id, request_id, build.get("id")):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
            raise PreparationError("A stable project/build/request ID is required")
    if build.get("project_id") != project_id:
        raise PreparationError("Build does not belong to this project")
    payload = {"project_id": project_id, "build_id": build["id"], "artifact_sha256": build.get("artifact_sha256"),
               "engine_sha256": build.get("engine_sha256"), "manifest_path": build.get("manifest_path"),
               "manifest_sha256": build.get("manifest_sha256"), "source_revision": build.get("source_revision"),
               "command": build.get("command"), "engine_version": build.get("engine_version"), "structured_version": build.get("structured_version"),
               "integration_version": build.get("integration_version"), "confirmed": True}
    previous = store.get("environment_preparations", request_id)
    if previous:
        if previous["request"] != payload:
            raise PreparationError("Preparation request ID cannot be rebound to another payload")
        if previous["status"] == "completed":
            return previous["result"]
    root = await asyncio.to_thread(validate_build, build)
    if not previous:
        for worker in store.list("workers"):
            if worker.get("enabled", True) is not True:
                continue
            environment = await inspect_fresh_environment(build, worker, credentials, client_factory=client_factory)
            if environment["ready"]:
                result = {"request_id": request_id, "project_id": project_id, "build_id": build["id"],
                          "status": "completed", "worker_id": worker["id"], "environment": environment}
                store.put("environment_preparations", {"id": request_id, "request": payload,
                          "status": "completed", "result": result, "reused": True, "updated_at": time.time()})
                return result
    resources = _safe_path(resource_root or Path(__file__).resolve().parents[1])
    script = _safe_path(resources / "scripts/provision-workers.ps1")
    if not script.is_file() or not (resources / "worker/Dockerfile").is_file():
        raise PreparationError("Packaged worker and provisioning script resources are missing")
    image = await asyncio.to_thread(_image, resources)
    if previous and previous.get("image") != image:
        raise PreparationError("Bundled worker version changed during an incomplete request; reconcile its retained resources before making a new preparation request")
    target = _safe_path(store.root)
    if any(char in str(target) for char in (",", "\n", "\r")):
        raise PreparationError("Writable data path cannot be represented safely as a Docker credential bind mount")
    if target.is_relative_to(resources):
        # Checkout developer data is allowed only outside the immutable resource tree too.
        raise PreparationError("Preparation data must be outside the installation/resource directory")
    powershell, docker, env = _tools()
    # Incomplete legacy (request-derived) owners keep reconciling their own resources.
    owner = previous["owner"] if previous else hashlib.sha256((str(target) + "\0" + project_id + "\0" + build["id"]).encode()).hexdigest()[:32]
    directory = _safe_path(target / "environments" / owner)
    directory.mkdir(parents=True, exist_ok=True)
    prior = previous or max((item for item in store.list("environment_preparations") if item.get("owner") == owner and "api_port" in item),
                            key=lambda item: item.get("updated_at", 0), default=None)
    if previous:
        record = dict(previous)
    else:
        api, desktop = _ports()
        prefix = "workspace-" + owner[:24]
        record = {"id": request_id, "request": payload, "project_id": project_id, "build_id": build["id"],
                  "owner": owner, "image": image, "worker_id": prefix + "-1", "container": "ashad-" + prefix + "-1",
                  "prefix": prefix, "network": "ashad-workspace-" + hashlib.sha256(str(target).encode()).hexdigest()[:16],
                  "api_port": api, "desktop_port": desktop, "url": f"http://127.0.0.1:{api}",
                  "desktop_url": f"http://127.0.0.1:{desktop}/vnc.html"}
    record.update(status="preparing", updated_at=time.time())
    store.put("environment_preparations", record)
    try:
        registered = store.get("workers", record["worker_id"])
        if registered and (registered.get("environment_owner") != owner or registered.get("build_id") != build["id"]):
            raise PreparationError("Worker ID collision; unrelated registrations cannot be rebound")
        if await _run(runner, [docker, "info", "--format", "{{.OSType}}"], directory, env) != "linux":
            raise PreparationError("Docker Desktop must be reachable in Linux-container mode")
        available = await _run(runner, [docker, "image", "ls", "--filter", "reference=" + image, "--format", "{{.Repository}}:{{.Tag}}"], directory, env)
        if available and available != image:
            raise PreparationError("Worker image identity is ambiguous")
        if not available:
            await _run(runner, [docker, "build", "--tag", image, resources / "worker"], directory, env, timeout=900)
        await _run(runner, [docker, "image", "inspect", image], directory, env)
        names = await _run(runner, [docker, "container", "ls", "--all", "--filter", "name=^/" + record["container"] + "$", "--format", "{{.Names}}"], directory, env)
        if names:
            if not prior or names != record["container"]:
                raise PreparationError("Container name collision; unrelated resources cannot be adopted")
            # The existing container publishes the ports of the latest attempt that created it.
            record.update({key: prior[key] for key in ("api_port", "desktop_port", "url", "desktop_url")})
            inspected = json.loads(await _run(runner, [docker, "container", "inspect", record["container"]], directory, env))[0]
            labels = inspected["Config"].get("Labels") or {}
            mounts = {item["Destination"]: item for item in inspected.get("Mounts", [])}
            if labels.get("ashad.managed") != "true" or labels.get("ashad.environment-owner") != owner or inspected["Config"]["Image"] != record["image"] or not all(
                    destination in mounts and mounts[destination].get("Name") == "ashad-" + record["worker_id"] + suffix for destination, suffix in (("/data", "-data"), ("/home/player", "-home"))) or not all(
                    destination in mounts and Path(mounts[destination].get("Source", "")).resolve() == source and mounts[destination].get("RW") is False for destination, source in (("/game", root), ("/run/secrets", directory / "secrets" / record["worker_id"]))):
                raise PreparationError("Existing container ownership, image or mounts do not match this request")
            if not inspected["State"].get("Running"):
                recovery = _safe_path(directory / "recovery-state.json")
                await _run(runner, [docker, "cp", record["container"] + ":/data/worker-state.json", recovery], directory, env)
                state = json.loads(recovery.read_text(encoding="utf-8-sig"))
                if state.get("worker_id") != record["worker_id"] or state.get("contract_version") != CONTRACT or state.get("lifecycle") != "unbound" or state.get("binding") is not None or state.get("game_pid") or state.get("human_control") is not False or state.get("desktop_session") or state.get("desktop_pids") != [] or state.get("held_keys") != [] or state.get("held_buttons") != []:
                    raise PreparationError("Stopped worker idle/unbound state is not proven; reconcile manually")
            else:
                token_path = _safe_path(directory / "secrets" / record["worker_id"] / "worker_token")
                token = token_path.read_text(encoding="utf-8-sig").strip()
                async with client_factory(record["url"], token) as client:
                    health, status = await client.health(), await client.status()
                if any(probe.get("worker_id") != record["worker_id"] or probe.get("contract_version") != CONTRACT or not _idle(probe) for probe in (health, status)):
                    raise PreparationError("Existing worker is busy or its idle state is unknown")
        if powershell is None:
            entry = await _provision(runner, docker, env, record, root=root, directory=directory, owner=owner, existing=bool(names))
        else:
            registration = _safe_path(directory / "workers.json")
            argv = [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", script,
                    "-Count", "1", "-BuildPath", root, "-Image", image, "-SkipBuild", "-WorkerPrefix", record["prefix"],
                    "-ApiBasePort", str(record["api_port"] - 1), "-DesktopBasePort", str(record["desktop_port"] - 1),
                    "-SecretsRoot", directory / "secrets", "-RegistrationFile", registration,
                    "-DockerExecutable", docker, "-NetworkName", record["network"], "-ManagedOwner", owner]
            await _run(runner, argv, directory, env, timeout=300)
            entries = json.loads(registration.read_text(encoding="utf-8-sig"))
            if not isinstance(entries, list) or len(entries) != 1:
                raise PreparationError("Provisioning registration must contain exactly this one desktop")
            entry = entries[0]
        worker = await _import_worker(store, credentials, entry, record, directory)
        await _await_startup(worker, credentials, client_factory)
        environment = await inspect_fresh_environment(build, worker, credentials, client_factory=client_factory)
        result = {"request_id": request_id, "project_id": project_id, "build_id": build["id"],
                  "status": "completed", "worker_id": worker["id"], "environment": environment}
        record.update(status="completed", result=result, updated_at=time.time())
        store.put("environment_preparations", record)
        return result
    except BaseException as exc:
        # Persist no subprocess/worker exception text: it can contain a service secret.
        reason = str(exc) if isinstance(exc, PreparationError) else "Preparation interrupted or failed; retry the same request to reconcile owned resources"
        record.update(status="unknown" if isinstance(exc, asyncio.CancelledError) else "error",
                      reason=reason + ". Persistent profiles were retained.", updated_at=time.time())
        store.put("environment_preparations", record)
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise PreparationError(record["reason"]) from None
