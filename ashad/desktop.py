"""Owned-loopback transport for the desktop shell, not a browser-mode bypass."""
import asyncio
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
from urllib.parse import urlparse

from starlette.responses import JSONResponse

PROTOCOL = "ashad-desktop/1"


def bind_loopback():
    """Keep the ephemeral port reserved until the server takes this same socket."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        listener.setblocking(False)
        return listener
    except BaseException:
        listener.close()
        raise


class DesktopBoundary:
    """Secrets stay in main-process memory; main injects headers only for this origin.

    The main-only key is separate from renderer requests. A shutdown callback must
    drain admission and confirm all ownership release before returning true.
    """
    def __init__(self, app, *, port, session_key, owner_key, instance_id, shutdown, status=None):
        for key in (session_key, owner_key):
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", key):
                raise ValueError("Desktop keys require at least 256 bits of random material")
        if session_key == owner_key:
            raise ValueError("Desktop session and owner keys must differ")
        self.app = app
        self.authority = f"127.0.0.1:{port}"
        self.session_key, self.owner_key = session_key, owner_key
        self.instance_id, self.shutdown, self.status = instance_id, shutdown, status

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] != "http":
            await send({"type": "websocket.close", "code": 1008})
            return
        headers = {}
        for name, value in scope.get("headers", []):
            # Reject duplicate boundary headers rather than disagree with a proxy/parser.
            if name in headers and name in {b"host", b"origin", b"x-ashad-desktop", b"x-ashad-owner"}:
                return await self.reply(scope, receive, send, 403, "Ambiguous desktop headers")
            headers[name] = value.decode("latin-1")
        if headers.get(b"host") != self.authority or headers.get(b"origin", f"http://{self.authority}") != f"http://{self.authority}":
            return await self.reply(scope, receive, send, 403, "Unexpected desktop origin")
        administrative = scope["path"].startswith("/_desktop/")
        supplied = headers.get(b"x-ashad-owner" if administrative else b"x-ashad-desktop", "")
        expected = self.owner_key if administrative else self.session_key
        if not secrets.compare_digest(supplied.encode("latin-1"), expected.encode("ascii")):
            return await self.reply(scope, receive, send, 401, "Owned desktop authentication required")
        if not administrative:
            return await self.app(scope, receive, send)
        if scope["path"] == "/_desktop/ready" and scope["method"] == "GET":
            response = JSONResponse({"protocol": PROTOCOL, "instance_id": self.instance_id})
        elif scope["path"] == "/_desktop/status" and scope["method"] == "GET" and self.status:
            response = JSONResponse(self.status())
        elif scope["path"] == "/_desktop/shutdown" and scope["method"] == "POST":
            raw = b""
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                raw += message.get("body", b"")
                if len(raw) > 1024:
                    return await self.reply(scope, receive, send, 413, "Desktop command too large")
                if not message.get("more_body"):
                    break
            try:
                options = json.loads(raw or b"{}")
                if not isinstance(options, dict) or set(options) - {"stop_runs", "cancel_operations"} or any(type(v) is not bool for v in options.values()):
                    raise ValueError()
            except (ValueError, UnicodeError):
                return await self.reply(scope, receive, send, 422, "Use explicit boolean shutdown choices")
            result = await self.shutdown(**options)
            response = JSONResponse(result, status_code=200 if result["drained"] else 409)
        else:
            response = JSONResponse({"detail": "Unknown desktop operation"}, status_code=404)
        response.headers["Cache-Control"] = "no-store"
        await response(scope, receive, send)

    @staticmethod
    async def reply(scope, receive, send, status, message):
        await JSONResponse({"detail": message}, status_code=status,
                           headers={"Cache-Control": "no-store"})(scope, receive, send)


class DesktopLifecycle:
    """Quit uses the normal runtime stop/reconciliation paths, never process-kill."""
    def __init__(self, app, server):
        self.app, self.server = app, server
        self.lock = asyncio.Lock()

    def status(self):
        state = self.app.state
        backend = state.ai.backend
        login = getattr(backend, "login_wait", None)
        operations = list(state.operations.values())
        if login and not login.done():
            operations.append({"kind": "sign_in"})
        viewers = []
        for worker in state.store.list("workers"):
            url = worker.get("desktop_url", "")
            parsed = urlparse(url)
            if parsed.scheme in {"http", "https"} and parsed.hostname and not any((parsed.username, parsed.password, parsed.query, parsed.fragment)):
                viewers.append(url)
        return {"owners": state.coordinator.occupied_instances(), "operations": operations,
                "draining": state.coordinator.draining, "viewer_urls": viewers}

    async def shutdown(self, *, stop_runs=False, cancel_operations=False):
        async with self.lock:
            coord = self.app.state.coordinator
            async with coord.lock:
                current = self.status()
                if current["owners"] and not stop_runs:
                    return {"drained": False, "detail": "Confirm Stop and quit for every owned instance.", **current}
                if current["operations"] and not cancel_operations:
                    return {"drained": False, "detail": "Wait for operations or explicitly cancel tuning/sign-in.", **current}
                if any(op["kind"] not in {"tuning", "sign_in"} for op in current["operations"]):
                    return {"drained": False, "detail": "Wait for desktop/setup/lifecycle mutations to finish.", **current}
                coord.draining = True
            try:
                task = self.app.state.tuning_task
                if task and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                backend = self.app.state.ai.backend
                login = getattr(backend, "login_wait", None)
                if login and not login.done():
                    await backend.login_cancel()
                # Legacy data can contain multiple owners; stop/reconcile every one.
                for owner in coord.occupied_instances():
                    if owner["state"] == "Unknown":
                        continue  # Never fabricate release of an orphaned lease.
                    if owner["state"] == "Ended":
                        await coord.reconcile(owner["run_id"])
                    else:
                        await coord.control(owner["run_id"], "stop", command_id="desktop_quit_" + secrets.token_hex(16))
                remaining = coord.occupied_instances()
                if remaining:
                    return {"drained": False, "detail": "Teardown remains unconfirmed; reconcile ownership before quitting.", "owners": remaining}
                self.server.should_exit = True
                return {"drained": True}
            except Exception as exc:
                # Keep the service/UI and independent watchdog available for recovery.
                return {"drained": False, "detail": f"Shutdown failed ({type(exc).__name__}); inspect instance recovery.",
                        "owners": coord.occupied_instances()}
            finally:
                if not self.server.should_exit:
                    coord.draining = False


def read_launch_config(stream):
    """One bounded stdin handshake; credentials never enter argv/env/logs."""
    raw = stream.readline(4097)
    if len(raw) > 4096:
        raise ValueError("Desktop launch configuration too large")
    config = json.loads(raw)
    required = {"data_root", "session_key", "owner_key", "instance_id"}
    if not isinstance(config, dict) or set(config) != required:
        raise ValueError("Invalid desktop launch configuration")
    if not isinstance(config["data_root"], str) or not Path(config["data_root"]).is_absolute():
        raise ValueError("Desktop requires an absolute writable user-data root")
    if not isinstance(config["instance_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", config["instance_id"]):
        raise ValueError("Desktop requires a launch identity")
    for key in (config["session_key"], config["owner_key"]):
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", key):
            raise ValueError("Invalid desktop launch keys")
    if config["session_key"] == config["owner_key"]:
        raise ValueError("Desktop launch keys must differ")
    return config


async def serve_desktop(config, controller_lock=None):
    import uvicorn
    from .app import create_app

    listener = bind_loopback()
    port = listener.getsockname()[1]
    # Desktop starts with its own store only. Importing checkout data/credentials is
    # an explicit migration, never initialize_local's automatic side effect.
    app = create_app(config["data_root"], initialize=False, controller_lock=controller_lock)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False,
                                         timeout_graceful_shutdown=5))
    lifecycle = DesktopLifecycle(app, server)
    server.config.app = DesktopBoundary(app, port=port, session_key=config["session_key"],
        owner_key=config["owner_key"], instance_id=config["instance_id"],
        shutdown=lifecycle.shutdown, status=lifecycle.status)
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        while not server.started:
            if task.done():
                await task
                raise RuntimeError("Controller exited before readiness")
            await asyncio.sleep(.01)
        print(json.dumps({"protocol": PROTOCOL, "port": port, "instance_id": config["instance_id"]}), flush=True)
        await task
    finally:
        listener.close()
        if not task.done():
            server.should_exit = True
            await task


def main():
    from contextlib import closing
    from .process_lock import acquire
    from .store import Store
    from .coordinator import Coordinator

    config = read_launch_config(sys.stdin)
    root = Path(config["data_root"]).resolve()
    os.environ["ASHAD_DATA"] = str(root)
    with acquire(root / "controller.lock") as controller_lock:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        watchdog = subprocess.Popen([sys.executable, "-I", "-B", "-m", "ashad.watchdog"],
            cwd=Path(__file__).resolve().parents[1], creationflags=flags)
        try:
            asyncio.run(serve_desktop(config, controller_lock=controller_lock))
        finally:
            # Forced loss is not successful drain. Retain independent authority
            # while any active/queued/unresolved ownership persists.
            with closing(Store(root)) as store:
                owners = Coordinator(store, None, None).occupied_instances()
            if not owners:
                watchdog.terminate()
                watchdog.wait(timeout=10)
