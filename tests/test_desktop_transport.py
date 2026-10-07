"""Real loopback transport proof; fixture data, no AI, worker, or keyring access."""
import asyncio
from contextlib import asynccontextmanager
import secrets
import socket

import httpx
import pytest
import uvicorn
from starlette.responses import StreamingResponse

from ashad.app import create_app
from ashad.desktop import DesktopBoundary, PROTOCOL, bind_loopback


class NoCredentials:
    def get(self, reference="openai"):
        raise AssertionError("Transport proof must not access credentials")


@asynccontextmanager
async def desktop_server(tmp_path):
    app = create_app(tmp_path / "desktop transport data", credentials=NoCredentials(),
                     initialize=False, start_services=False)
    session, owner = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    instance = secrets.token_hex(16)
    listener = bind_loopback()
    port = listener.getsockname()[1]
    state = {"can_drain": False}

    @app.get("/fixture-camera")
    async def camera():
        async def frames():
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\nfixture-not-gameplay\r\n"
        return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame")

    async def shutdown():
        if not state["can_drain"]:
            return False
        server.should_exit = True
        return True

    wrapped = DesktopBoundary(app, port=port, session_key=session, owner_key=owner,
                              instance_id=instance, shutdown=shutdown)
    server = uvicorn.Server(uvicorn.Config(wrapped, log_level="error", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                    raise AssertionError("Server exited before readiness")
                await asyncio.sleep(.01)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
            yield client, state, session, owner, instance, port
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        listener.close()


@pytest.mark.asyncio
async def test_owned_readiness_assets_camera_and_confirmed_shutdown(tmp_path):
    async with desktop_server(tmp_path) as (client, state, session, owner, instance, port):
        # The selected port stays bound, so another process cannot take it between
        # discovery and server startup; a mere /api/health reply proves nothing.
        with socket.socket() as competitor:
            with pytest.raises(OSError):
                competitor.bind(("127.0.0.1", port))
        for path in ("/", "/static/app.js", "/api/health", "/fixture-camera", "/_desktop/ready"):
            assert (await client.get(path)).status_code == 401
        headers = {"X-ASHAD-Desktop": session}
        assert (await client.get("/_desktop/ready", headers=headers)).status_code == 401
        admin = {"X-ASHAD-Owner": owner}
        ready = await client.get("/_desktop/ready", headers=admin)
        assert ready.json() == {"protocol": PROTOCOL, "instance_id": instance}
        assert ready.headers["cache-control"] == "no-store"
        assert (await client.get("/api/health", headers=admin)).status_code == 401
        index = await client.get("/", headers=headers)
        assert index.status_code == 200 and "/static/app.js" in index.text
        assert "script-src 'self'" in index.headers["content-security-policy"]
        assert session not in index.text and owner not in index.text
        assert (await client.get("/static/app.js", headers=headers)).status_code == 200
        assert (await client.get("/api/bootstrap", headers=headers)).json()["runs"] == []
        feed = await client.get("/fixture-camera", headers=headers)
        assert feed.status_code == 200 and b"fixture-not-gameplay" in feed.content
        assert feed.headers["content-type"].startswith("multipart/x-mixed-replace")
        # Existing mutation-intent requirement remains, even with a valid session.
        assert (await client.post("/api/settings", headers=headers, json={})).status_code == 403
        for forged in ({"Origin": "https://127.0.0.1:" + str(port)}, {"Host": "attacker.invalid"}):
            assert (await client.get("/api/health", headers={**headers, **forged})).status_code == 403
        assert (await client.post("/_desktop/shutdown", headers=headers)).status_code == 401
        assert (await client.post("/_desktop/shutdown", headers=admin)).status_code == 409
        assert (await client.get("/api/health", headers=headers)).status_code == 200
        state["can_drain"] = True
        assert (await client.post("/_desktop/shutdown", headers=admin)).json() == {"drained": True}
    # Lifespan closed the real SQLite handle; no controller process was killed.
    with socket.socket() as released:
        released.bind(("127.0.0.1", port))


def test_transport_rejects_weak_or_reused_keys():
    with pytest.raises(ValueError):
        DesktopBoundary(None, port=1, session_key="short", owner_key="short", instance_id="test", shutdown=None)
    key = secrets.token_urlsafe(32)
    with pytest.raises(ValueError):
        DesktopBoundary(None, port=1, session_key=key, owner_key=key, instance_id="test", shutdown=None)
