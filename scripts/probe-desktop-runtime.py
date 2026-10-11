"""Offline P0 resource probe; never starts a controller, watchdog loop, or provider.

Run with the candidate Python executable and --scratch-parent under your workspace.
The candidate must already resolve ASHAD and its dependencies (PYTHONPATH in dev,
private runtime search path when packaged). All mutable data is a fresh fixture.
"""
import argparse
import asyncio
from importlib import import_module, metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


class OfflineOnly:
    """Fail closed if fixture startup accidentally requests credentials/provider work."""

    def __getattr__(self, name):
        raise AssertionError(f"Offline probe attempted forbidden operation: {name}")

    async def close(self):
        pass


def child(command, root):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("ASHAD_", "CODEX_", "OPENAI_", "PYTHON"))}
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "CODEX_HOME"):
        env[key] = str(root)
    return subprocess.run(command, cwd=root, env=env, capture_output=True,
                          text=True, encoding="utf-8", timeout=30)


def check_lock(root, app_root):
    from ashad.process_lock import acquire

    # Explicit resource path, not checkout cwd or inherited PYTHONPATH.
    code = ("import sys; sys.path.insert(0, sys.argv[1]); "
            "import ashad.__main__, ashad.watchdog; from ashad.process_lock import acquire\n"
            "try:\n acquire(sys.argv[2]).close()\n"
            "except (RuntimeError, OSError) as exc:\n print(type(exc).__name__); sys.exit(17)\n")
    rejections = {}
    for name in ("controller.lock", "watchdog.lock"):
        path = root / name
        command = [sys.executable, "-I", "-B", "-c", code, str(app_root), str(path)]
        with acquire(path):
            conflict = child(command, root)
            assert conflict.returncode == 17, conflict.stderr
            rejections[name] = conflict.stdout.strip()
        released = child(command, root)
        assert released.returncode == 0, released.stderr
    return rejections


async def check_app(root):
    import httpx
    from ashad.app import create_app
    from ashad.watchdog import once

    app = create_app(data_root=root / "controller", credentials=OfflineOnly(),
                     start_services=False, ai_backend=OfflineOnly())
    async with app.router.lifespan_context(app):
        # Empty fixture only; any attempted worker operation raises immediately.
        await once(app.state.store, OfflineOnly())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            for url in ("/", "/static/app.js", "/static/app.css", "/api/health", "/api/bootstrap"):
                response = await client.get(url)
                assert response.status_code == 200 and response.content, (url, response.status_code)
            bootstrap = (await client.get("/api/bootstrap")).json()
            assert all(bootstrap[key] == [] for key in ("runs", "workers", "accounts", "configurations", "builds")) and bootstrap["usage_count"] == 0
    # Lifespan closed SQLite: data can be reopened after drain.
    from ashad.store import Store
    store = Store(root / "controller")
    try:
        assert store.list("runs") == []
    finally:
        store.close()


def probe(scratch_parent):
    import ashad
    from codex_cli_bin import bundled_codex_path, bundled_package_dir

    modules = ("ashad.__main__", "ashad.watchdog", "ashad.app", "ashad.credentials")
    for name in modules:
        import_module(name)  # Import only: no main(), no watchdog process, no keyring calls.
    app_root = Path(ashad.__file__).resolve().parent.parent
    static = app_root / "ashad" / "static"
    for name in ("index.html", "app.js", "core.js", "app.css"):
        assert (static / name).is_file(), f"Missing static resource: {name}"
    backend = "not checked: Windows-only probe"
    if os.name == "nt":
        from keyring.backends.Windows import WinVaultKeyring
        assert WinVaultKeyring.priority > 0
        backend = "keyring.backends.Windows.WinVaultKeyring (import/priority only; no credential access)"
    package = bundled_package_dir()
    manifest = json.loads((package / "codex-package.json").read_text(encoding="utf-8"))
    executable = bundled_codex_path()
    assert executable == package / manifest["entrypoint"]
    resources = sorted(str(path.relative_to(package)) for path in package.rglob("*") if path.is_file())
    if os.name == "nt":
        for name in ("bin/codex-code-mode-host.exe", "codex-path/rg.exe",
                     "codex-resources/codex-command-runner.exe", "codex-resources/codex-windows-sandbox-setup.exe"):
            assert (package / name).is_file(), f"Missing Codex resource: {name}"
    scratch_parent = Path(scratch_parent).resolve()
    scratch_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="desktop fixture ", dir=scratch_parent) as temporary:
        root = Path(temporary)
        lock_rejections = check_lock(root, app_root)
        asyncio.run(check_app(root))
        version = child([str(executable), "--version"], root)
        assert version.returncode == 0, version.stderr
        assert manifest["version"] in version.stdout, version.stdout
    return {
        "status": "ok", "python": sys.version.split()[0], "executable": sys.executable,
        "app_root": str(app_root), "cwd": str(Path.cwd()), "keyring": backend,
        "dependencies": {name: metadata.version(name) for name in
                         ("fastapi", "uvicorn", "keyring", "openai-codex", "openai-codex-cli-bin")},
        "codex_version": version.stdout.strip(), "codex_resources": resources,
        "lock_rejections": lock_rejections,
        "checks": ["static HTTP resources", "empty bootstrap", "fixture lifespan drain/reopen",
                   "controller/watchdog imports in independent interpreter",
                   "cross-process lock rejection/reacquisition", "empty watchdog pass", "Codex --version"],
        "not_verified": ["installed Electron application", "OS keyring read/write or login",
                         "live controller/watchdog startup", "authenticated readiness/shutdown",
                         "provider connection or inference", "Docker or gameplay", "non-admin installation"],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch-parent", type=Path, required=True,
                        help="Parent for disposable fixtures; never an existing controller data directory")
    args = parser.parse_args()
    print(json.dumps(probe(args.scratch_parent), indent=2))
