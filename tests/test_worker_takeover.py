"""Human control owns the paused desktop, never the bot's action channel."""
import asyncio
import json
import signal
import time
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import HTTPException

from test_workers import DesktopDouble, ProcessDouble
from worker.app import create_app
from worker.models import Binding, BoundCommand, GameRequest, Heartbeat, Stop
from worker.runtime import Runtime


class ViewerProcess:
    def __init__(self, pid, returncode=None):
        self.pid, self.returncode = pid, returncode


async def paused_worker(tmp_path):
    worker = Runtime(tmp_path, DesktopDouble(), "worker")
    command = BoundCommand(run_id="run", player_id="player", epoch="epoch")
    await worker.bind(Binding(**command.model_dump(exclude={"command_id"}), identity="identity"))
    worker.game = ProcessDouble()
    worker.game_bridge.request = AsyncMock(return_value={"status": "held"})
    worker.lifecycle = "running"
    await worker.pause(command)

    async def start_viewer():
        worker.desktop_session = "private-session-marker"
        worker.setup_processes = [ViewerProcess(123), ViewerProcess(456)]
        worker._persist()

    async def terminate(process, hard=False):
        process.returncode = -9 if hard else -15

    worker._start_desktop_viewer = AsyncMock(side_effect=start_viewer)
    worker._terminate_process = AsyncMock(side_effect=terminate)
    return worker, command


@pytest.mark.asyncio
async def test_takeover_requires_auth_current_owner_pause_and_live_game(tmp_path):
    worker, command = await paused_worker(tmp_path)
    token = "worker-private-token-" * 3
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(worker, token)), base_url="http://worker") as client:
        assert (await client.post("/desktop/start", json=command.model_dump())).status_code == 401
        client.headers["Authorization"] = "Bearer " + token
        assert (await client.post("/desktop/start", json={**command.model_dump(), "epoch": "stale"})).status_code == 409
        worker.lifecycle = "running"
        assert (await client.post("/desktop/start", json=command.model_dump())).status_code == 409
        worker.lifecycle = "paused"
        worker.game.returncode = 0
        assert (await client.post("/desktop/start", json=command.model_dump())).status_code == 409
        worker.game.returncode = None
        response = await client.post("/desktop/start", json=command.model_dump())
        assert response.status_code == 200 and response.json()["human_control"] is True
        assert token not in response.text and "private-session-marker" not in response.text
        assert (await client.post("/desktop/finish", json={**command.model_dump(), "epoch": "stale"})).status_code == 409
        assert worker.human_control
        assert (await client.post("/desktop/finish", json=command.model_dump())).json()["human_control"] is False


@pytest.mark.asyncio
async def test_capture_continues_bots_blocked_and_handback_releases_human_input(tmp_path):
    worker, command = await paused_worker(tmp_path)
    await worker.start_desktop(command)
    await worker.start_desktop(command)
    worker._start_desktop_viewer.assert_awaited_once()
    assert worker._capture_allowed() and not worker.steam_setup
    worker.lifecycle = "running"  # The independent human-control guard still blocks bot inputs.
    worker.interrupt.clear()
    with pytest.raises(HTTPException, match="Game inputs are held"):
        await worker.game_request(GameRequest(**command.model_dump(), operation="perform_action", arguments={
            "action_id": "action", "observation_id": "old", "expires_at": time.time() + 5, "verb": "input"}))
    await worker.heartbeat(Heartbeat(**command.model_dump()))
    assert worker.game_bridge.request.call_args.args[1]["enabled"] is False
    worker.lifecycle = "paused"
    worker.observations["old"] = {}; worker.game_observations["old"] = {}
    result = await worker.finish_desktop(command)
    assert result["human_control"] is False and result["lifecycle"] == "paused"
    assert not worker.setup_processes and not worker.observations and not worker.game_observations
    assert any(call[0] == "keyup" and "w" in call and "Escape" in call for call in worker.desktop.commands)
    assert worker.desktop.releases[-1][1] == set(range(1, 10))
    assert not worker.held_keys and not worker.held_buttons
    await worker.finish_desktop(command)
    assert worker._terminate_process.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["pause", "resume", "stop", "disconnect", "close"])
async def test_run_lifecycle_revokes_viewer_before_bots_or_teardown(tmp_path, operation):
    worker, command = await paused_worker(tmp_path)
    await worker.start_desktop(command)
    viewers = list(worker.setup_processes)
    if operation in {"stop", "disconnect"}:
        await worker.stop(Stop(**command.model_dump(), terminate=True), disconnect=operation == "disconnect")
    elif operation == "close":
        await worker.close()
    else:
        await getattr(worker, operation)(command)
    assert not worker.human_control and all(process.returncode is not None for process in viewers)
    if operation == "resume":
        assert worker.lifecycle == "running" and not worker.interrupt.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["heartbeat", "game", "viewer"])
async def test_manual_watchdog_revokes_access_and_stays_paused(tmp_path, failure):
    worker, command = await paused_worker(tmp_path)
    await worker.start_desktop(command)
    if failure == "heartbeat":
        worker.last_heartbeat -= 100
    elif failure == "game":
        worker.game.returncode = 1
    else:
        worker.setup_processes[0].returncode = 1
    await worker.watchdog_tick()
    assert not worker.human_control and not worker.setup_processes
    assert worker.lifecycle == "paused" and worker.interrupt.is_set()


@pytest.mark.asyncio
async def test_start_failure_stays_paused_and_cleans_partial_viewer(tmp_path):
    worker, command = await paused_worker(tmp_path)
    partial = ViewerProcess(123)
    async def fail():
        worker.setup_processes = [partial]
        raise RuntimeError("viewer failed")
    worker._start_desktop_viewer.side_effect = fail
    with pytest.raises(HTTPException, match="desktop viewer could not start"):
        await worker.start_desktop(command)
    assert not worker.human_control and partial.returncode is not None
    assert worker.lifecycle == "paused" and worker.interrupt.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [ConnectionRefusedError("unavailable"), {"status": "error"}])
async def test_takeover_does_not_open_viewer_without_confirmed_game_hold(tmp_path, answer):
    worker, command = await paused_worker(tmp_path)
    if isinstance(answer, Exception):
        worker.game_bridge.request.side_effect = answer
    else:
        worker.game_bridge.request.return_value = answer
    with pytest.raises(HTTPException, match="bot inputs have stopped"):
        await worker.start_desktop(command)
    worker._start_desktop_viewer.assert_not_awaited()
    assert not worker.human_control and worker.lifecycle == "paused"


@pytest.mark.asyncio
async def test_viewer_uses_authentication_clears_keys_and_persists_owned_processes(tmp_path, monkeypatch):
    worker, command = await paused_worker(tmp_path)
    password = tmp_path / "password"
    password.write_text("do-not-expose-password")
    monkeypatch.setenv("ASHAD_VNC_PASSWORD_FILE", str(password))
    stored = ViewerProcess(1, returncode=0)
    stored.wait = AsyncMock(return_value=0)
    processes = [stored, ViewerProcess(123), ViewerProcess(456)]
    spawn = AsyncMock(side_effect=processes)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    writer = Mock(wait_closed=AsyncMock())
    monkeypatch.setattr(asyncio, "open_connection", AsyncMock(return_value=(None, writer)))
    worker._start_desktop_viewer = Runtime._start_desktop_viewer.__get__(worker)
    result = await worker.start_desktop(command)
    assert "do-not-expose-password" not in json.dumps(result)
    vnc_args = spawn.call_args_list[1].args
    assert "-rfbauth" in vnc_args and "-localhost" in vnc_args and "-clear_all" in vnc_args
    state = json.loads((tmp_path / "worker-state.json").read_text())
    assert state["human_control"] is True and state["desktop_pids"] == [123, 456]
    assert spawn.call_args_list[1].kwargs["env"]["ASHAD_DESKTOP_SESSION"] == state["desktop_session"]
    await worker.finish_desktop(command)
    state = json.loads((tmp_path / "worker-state.json").read_text())
    assert state["human_control"] is False and not state["desktop_pids"] and state["desktop_session"] is None


def test_independent_recovery_closes_only_matching_session_process_groups(monkeypatch):
    from worker import independent_watchdog
    from pathlib import Path
    monkeypatch.setattr(independent_watchdog, "os", Mock(name="posix", killpg=Mock()))
    independent_watchdog.os.name = "posix"
    def environment(path):
        return b"ASHAD_DESKTOP_SESSION=current\0" if str(path).replace("\\", "/") == "/proc/123/environ" else b"ASHAD_DESKTOP_SESSION=different\0"
    monkeypatch.setattr(Path, "read_bytes", environment)
    independent_watchdog.revoke_desktop({"desktop_session": "current", "desktop_pids": [123, 456, "bad", -1]})
    independent_watchdog.os.killpg.assert_called_once_with(123, signal.SIGTERM)
