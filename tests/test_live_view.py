"""Live-view transport and cleanup checks; no game, desktop capture, or provider call."""
import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from ashad.worker_client import WorkerClient
from worker.app import create_app
from worker.models import Binding, BoundCommand
from worker.runtime import Runtime
from test_workers import DesktopDouble, ProcessDouble
from test_ui_http import app_at


class EncoderDouble:
    def __init__(self):
        self.returncode = None
        self.stdout = type("Pipe", (), {})()
        self.stdout.read = AsyncMock(side_effect=[b"--frame\r\nfirst", b"second", b""])
        self.wait = AsyncMock()

    def kill(self):
        self.returncode = -9


async def live_worker(tmp_path):
    worker = Runtime(tmp_path, DesktopDouble(), "worker")
    binding = Binding(run_id="run", player_id="player", epoch="epoch", identity="identity")
    await worker.bind(binding)
    worker.game = ProcessDouble()
    worker.lifecycle = "running"
    encoder = EncoderDouble()
    worker.desktop.live_video = AsyncMock(return_value=encoder)
    return worker, encoder, BoundCommand(run_id="run", player_id="player", epoch="epoch")


@pytest.mark.asyncio
async def test_live_frames_continue_when_paused_but_stop_before_private_capture(tmp_path):
    worker, encoder, command = await live_worker(tmp_path)
    frames = worker.live_frames(command)
    assert await anext(frames) == b"--frame\r\nfirst"
    worker.lifecycle = "paused"  # Human viewing continues while inputs are held.
    assert await anext(frames) == b"second"
    async def private_read(size):
        worker.steam_setup = True
        return b"PRIVATE LOGIN MUST NOT LEAVE WORKER"
    encoder.stdout.read.side_effect = private_read
    with pytest.raises(StopAsyncIteration):
        await anext(frames)
    assert encoder.returncode == -9 and worker.live_viewers == 0
    encoder.wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_disconnect_cancels_encoder_and_old_epoch_cannot_open_camera(tmp_path):
    worker, encoder, command = await live_worker(tmp_path)
    with pytest.raises(HTTPException):
        await anext(worker.live_frames(command.model_copy(update={"epoch": "old"})))
    worker.desktop.live_video.assert_not_called()
    entered = asyncio.Event()
    async def blocked_read(size):
        entered.set()
        await asyncio.Event().wait()
    encoder.stdout.read.side_effect = blocked_read
    request = asyncio.create_task(anext(worker.live_frames(command)))
    await entered.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert encoder.returncode == -9 and worker.live_viewers == 0
    encoder.wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_live_http_requires_auth_binding_and_open_capture(tmp_path):
    worker, encoder, command = await live_worker(tmp_path)
    transport = httpx.ASGITransport(app=create_app(worker, token="live-view-secret-" * 3))
    async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
        params = command.model_dump(exclude_none=True)
        assert (await client.get('/live.mjpeg', params=params)).status_code == 401
        client.headers['Authorization'] = 'Bearer ' + 'live-view-secret-' * 3
        assert (await client.get('/live.mjpeg', params={**params, 'epoch':'old'})).status_code == 409
        worker.live_viewers = 4
        assert (await client.get('/live.mjpeg', params=params)).status_code == 429
        worker.live_viewers = 0
        response = await client.get('/live.mjpeg', params=params)
        assert response.content == b'--frame\r\nfirstsecond'
        assert response.headers['content-type'] == 'multipart/x-mixed-replace; boundary=frame'
        worker.steam_setup = True
        assert (await client.get('/live.mjpeg', params=params)).status_code == 409


def test_controller_stream_is_bound_and_closes_worker_connection(tmp_path):
    class Frames(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            yield b'--frame\r\n'
            yield b'camera data'
        async def aclose(self):
            self.closed = True
    stream = Frames()
    received = []
    def upstream(request):
        received.append(request)
        return httpx.Response(200, stream=stream)
    app = app_at(tmp_path)
    worker = WorkerClient('http://worker', 'private-service-token', transport=httpx.MockTransport(upstream))
    app.state.coordinator.client_factory = lambda record: worker
    store = app.state.store
    store.put('workers', {'id':'worker'})
    run = {'id':'run', 'state':'Running', 'players':{'player':{'launched':True,'worker_id':'worker','epoch':'epoch'}}}
    store.put('runs', run)
    with TestClient(app) as client:
        url = '/api/runs/run/players/player/live'
        assert client.get(url+'?epoch=old').status_code == 409
        assert not received
        response = client.get(url+'?epoch=epoch')
        assert response.content == b'--frame\r\ncamera data' and stream.closed
        assert dict(received[0].url.params) == {'run_id':'run','player_id':'player','epoch':'epoch'}
        assert 'private-service-token' not in str(response.headers) + response.text
        run['state'] = 'Ended';store.put('runs', run)
        assert client.get(url+'?epoch=epoch').status_code == 503
