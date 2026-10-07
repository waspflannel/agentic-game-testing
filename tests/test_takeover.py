"""Human takeover contracts with temporary records and explicit desktop/AI doubles."""
import asyncio
import json
import time
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import pytest

from ashad.app import create_app
from test_acceptance_edges import ready_run
from test_controller import environment, FakeCodexBackend  # noqa: F401


def desktop_controls(env):
    """Exercise coordinator ordering while replacing only remote desktop IO."""
    controls = {"start": {"human_control": True}, "finish": {"human_control": False}, "pause_failure": None, "pause_result": {"lifecycle": "paused"}}
    for desktop in env[3].values():
        original = desktop.request

        async def request(method, path, *, data=None, _desktop=desktop, _original=original, **kwargs):
            if path in {"/desktop/start", "/desktop/finish"}:
                _desktop.calls.append((path, data))
                result = controls["start" if path.endswith("start") else "finish"]
                if isinstance(result, Exception):
                    raise result
                if type(result.get("human_control")) is bool:
                    _desktop.human_control = result["human_control"]
                return result
            if path == "/pause" and _desktop.worker["id"] == controls["pause_failure"]:
                _desktop.calls.append((path, data))
                raise OSError("pause acknowledgement lost")
            if path == "/status":
                _desktop.calls.append((path, data))
                human = getattr(_desktop, "human_control", False)
                return {"human_control": human, "lifecycle": "paused" if human else "running"}
            if path == "/pause":
                result = await _original(method, path, data=data, **kwargs)
                return {**result, **controls["pause_result"]}
            return await _original(method, path, data=data, **kwargs)

        desktop.request = request
        desktop.close = AsyncMock()
        desktop.calls.clear()
    return controls


async def takeover(coord, run, command_id="takeover"):
    return await coord.control(run["id"], "takeover", command_id=command_id,
                               player_id="player-1", desktop_url="http://127.0.0.1:6081/vnc.html")


@pytest.mark.asyncio
async def test_takeover_pauses_every_bot_cancels_inference_and_keeps_other_runs_idle(environment):
    store, coord, _, desktops = environment
    run = await ready_run(environment, count=2)
    desktop_controls(environment)
    tasks = [asyncio.create_task(asyncio.Event().wait()) for _ in range(3)]
    coord.tasks.update({(run["id"], "player-1"): tasks[0], (run["id"], "player-2"): tasks[1], ("other-run", "player-1"): tasks[2]})
    await asyncio.sleep(0)
    try:
        actual = await takeover(coord, run)
        assert actual["state"] == "Paused" and actual["resume_state"] == "Running"
        assert tasks[0].cancelled() and tasks[1].cancelled() and not tasks[2].done()
        assert actual["pause"]["category"] == "human_control"
        assert actual["pause"]["players"] == ["player-1", "player-2"]
        assert actual["human_control"]["player_id"] == "player-1"
        assert actual["players"]["player-1"]["agent_status"]["phase"] == "human_control"
        for player in run["players"].values():
            assert actual["players"][player["id"]]["generation"] > player["generation"]
            paths = [path for path, _ in desktops[player["worker_id"]].calls]
            assert "/pause" in paths and "/resume" not in paths
        host_paths = [path for path, _ in desktops["worker-1"].calls]
        assert host_paths.index("/pause") < host_paths.index("/desktop/start")
        assert not any(path == "/desktop/start" for path, _ in desktops["worker-2"].calls)
        assert not coord.ai.backend.requests
        assert store.require("runs", run["id"])["human_control"] == actual["human_control"]
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation,player", [("Queued", "player-1"), ("Stopping", "player-1"), ("Ended", "player-1"), (None, "missing"), ("unbound", "player-1"), ("unlaunched", "player-1")])
async def test_takeover_rejects_invalid_state_or_player_without_worker_input(environment, mutation, player):
    store, coord, _, desktops = environment
    run = await ready_run(environment)
    desktop_controls(environment)
    if mutation in {"unbound", "unlaunched"}:
        run["players"]["player-1"]["bound" if mutation == "unbound" else "launched"] = False
    elif mutation:
        run["state"] = mutation
    store.put("runs", run)
    with pytest.raises(ValueError):
        await coord.control(run["id"], "takeover", command_id="invalid", player_id=player)
    assert store.require("runs", run["id"])["interventions"] == run["interventions"]
    assert all(not desktop.calls for desktop in desktops.values())


@pytest.mark.asyncio
async def test_duplicate_takeover_is_safe_and_switching_players_requires_handback(environment):
    _, coord, _, desktops = environment
    run = await ready_run(environment, count=2)
    desktop_controls(environment)
    first = await takeover(coord, run)
    calls = len(desktops["worker-1"].calls)
    again = await takeover(coord, run)
    assert again["human_control"] == first["human_control"]
    assert len(desktops["worker-1"].calls) == calls
    retry = await takeover(coord, run, "new-http-retry")
    assert retry["human_control"] == first["human_control"]
    assert sum(path == "/desktop/start" for path, _ in desktops["worker-1"].calls) == 1
    assert sum(path == "/pause" for path, _ in desktops["worker-1"].calls) == 1
    with pytest.raises(ValueError, match="Hand back"):
        await coord.control(run["id"], "takeover", command_id="switch", player_id="player-2")
    assert not any(path == "/desktop/start" for path, _ in desktops["worker-2"].calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unconfirmed", "start_error", "pause_error", "pause_unconfirmed"])
async def test_failed_takeover_remains_paused_and_same_command_can_retry(environment, failure):
    store, coord, _, desktops = environment
    run = await ready_run(environment, count=2)
    control = desktop_controls(environment)
    if failure == "unconfirmed":
        control["start"] = {}
    elif failure == "start_error":
        control["start"] = OSError("desktop start unavailable")
    elif failure == "pause_error":
        control["pause_failure"] = "worker-2"
    else:
        control["pause_result"] = {"lifecycle": "running"}
    with pytest.raises(ValueError, match="bots remain paused"):
        await takeover(coord, run)
    actual = store.require("runs", run["id"])
    assert actual["state"] == "Paused" and not actual.get("human_control")
    assert actual["pause"]["category"] == "infrastructure_failure"
    assert not any(path == "/resume" for desktop in desktops.values() for path, _ in desktop.calls)
    if failure in {"pause_error", "pause_unconfirmed"}:
        assert not any(path == "/desktop/start" for path, _ in desktops["worker-1"].calls)
    control.update(start={"human_control": True}, pause_failure=None, pause_result={"lifecycle": "paused"})
    assert (await takeover(coord, run))["human_control"]["player_id"] == "player-1"


@pytest.mark.asyncio
async def test_ambiguous_start_and_cleanup_keep_ownership_for_recovery(environment):
    store, coord, _, _ = environment
    run = await ready_run(environment)
    control = desktop_controls(environment)
    control.update(start=TimeoutError("acknowledgement lost"), finish=OSError("close unavailable"))
    with pytest.raises(ValueError):
        await takeover(coord, run)
    actual = store.require("runs", run["id"])
    assert actual["state"] == "Paused" and actual["human_control"]["player_id"] == "player-1"
    control["finish"] = {"human_control": False}
    closed = await coord.control(run["id"], "takeover_finish", command_id="recover", resume=False)
    assert closed["state"] == "Paused" and not closed.get("human_control")


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["takeover_finish", "resume"])
@pytest.mark.parametrize("failure", [OSError("desktop close unavailable"), {"human_control": True}, {}])
async def test_handback_never_resumes_until_viewer_closure_is_confirmed(environment, command, failure):
    store, coord, _, desktops = environment
    run = await ready_run(environment, count=2)
    control = desktop_controls(environment)
    await takeover(coord, run)
    control["finish"] = failure
    for desktop in desktops.values():
        desktop.calls.clear()
    with pytest.raises(ValueError, match="bots remain paused"):
        await coord.control(run["id"], command, command_id="handback")
    actual = store.require("runs", run["id"])
    assert actual["state"] == "Paused" and actual.get("human_control")
    assert not any(path == "/resume" for desktop in desktops.values() for path, _ in desktop.calls)
    control["finish"] = {"human_control": False}
    actual = await coord.control(run["id"], command, command_id="handback")
    assert actual["state"] == "Running" and not actual.get("human_control")
    assert actual["recovery_instruction"] == "" and actual["pause"] is None
    assert all(player["last_observation"] is None for player in actual["players"].values())
    host_paths = [path for path, _ in desktops["worker-1"].calls]
    assert host_paths[-3:] == ["/desktop/finish", "/resume", "/heartbeat"]
    calls = sum(len(desktop.calls) for desktop in desktops.values())
    await coord.control(run["id"], "takeover_finish", command_id="http-handback-retry")
    assert sum(len(desktop.calls) for desktop in desktops.values()) == calls
    assert not coord.ai.backend.requests


@pytest.mark.asyncio
async def test_human_recovery_ends_investigation_but_preserves_run_duration_and_can_leave_paused(environment):
    store, coord, plan, desktops = environment
    plan["limits"]["duration_seconds"] = 600
    run = await ready_run(environment, plan)
    desktop_controls(environment)
    await coord.investigate(run["id"], "progression_blocker", "needs a human")
    assert store.require("runs", run["id"])["investigation_deadline"] is not None
    human = await takeover(coord, run)
    assert human["state"] == "Paused" and human["investigation_deadline"] is None
    assert human["plan"]["limits"]["duration_seconds"] == 600
    coord.ai.backend.ready = False
    actual = await coord.control(run["id"], "takeover_finish", command_id="leave-paused", resume=False)
    assert actual["state"] == "Paused" and actual["investigation_deadline"] is None
    assert not actual.get("human_control")
    assert not any(path == "/resume" for path, _ in desktops["worker-1"].calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("close_fails", [False, True])
async def test_restart_closes_human_access_or_retains_unknown_ownership_without_resuming(environment, close_fails):
    store, coord, _, desktops = environment
    run = await ready_run(environment)
    control = desktop_controls(environment)
    await takeover(coord, run)
    if close_fails:
        control["finish"] = OSError("desktop unavailable during restart")
    await coord.recover()
    actual = store.require("runs", run["id"])
    assert actual["state"] == "Paused"
    if close_fails:
        assert actual["human_control"]["status"] == "unknown"
        assert any("Could not close desktop access" in entry for entry in actual["limitations"])
    else:
        assert not actual.get("human_control")
        assert actual["pause"]["category"] == "coordinator_restarted"
    assert not any(path == "/resume" for path, _ in desktops["worker-1"].calls)


@pytest.mark.asyncio
async def test_new_infrastructure_investigation_closes_human_access_and_sets_deadline(environment):
    store, coord, _, desktops = environment
    run = await ready_run(environment)
    desktop_controls(environment)
    await takeover(coord, run)
    await coord.investigate(run["id"], "infrastructure_failure", "capture stopped")
    actual = store.require("runs", run["id"])
    assert actual["state"] == "Investigating" and actual["investigation_deadline"] > time.time()
    assert not actual.get("human_control")
    assert any(path == "/desktop/finish" for path, _ in desktops["worker-1"].calls)


@pytest.mark.asyncio
async def test_run_duration_limit_still_ends_an_instance_under_human_control(environment):
    store, coord, plan, _ = environment
    plan["limits"]["duration_seconds"] = 600
    run = await ready_run(environment, plan)
    desktop_controls(environment)
    actual = await takeover(coord, run)
    actual["started_at"] = time.time() - 601
    store.put("runs", actual)
    assert await coord.enforce_limits(actual)
    ended = store.require("runs", run["id"])
    assert ended["state"] == "Ended" and ended["ending_reason"] == "limit_reached"
    assert not ended.get("human_control")


def takeover_app(environment):
    store, coord, _, desktops = environment
    run = asyncio.run(ready_run(environment))
    desktop_controls(environment)
    worker = store.require("workers", "worker-1")
    worker["desktop_url"] = "http://127.0.0.1:6081/vnc.html"
    store.put("workers", worker)
    coord.credentials.save("desktop:worker-1", "desktop-only-test-secret")
    app = create_app(store.root, credentials=coord.credentials, ai_backend=FakeCodexBackend(), initialize=False, start_services=False)
    app.state.coordinator.client_factory = lambda worker: desktops[worker["id"]]
    return app, run


def test_desktop_password_is_returned_only_by_explicit_takeover_response(environment):
    app, run = takeover_app(environment)
    secret = "desktop-only-test-secret"
    start = f"/api/runs/{run['id']}/players/player-1/takeover"
    finish = f"/api/runs/{run['id']}/takeover/finish"
    headers = {"X-ASHAD-Request": "1"}
    with TestClient(app) as client:
        assert client.post(start, json={}).status_code == 403
        assert client.post(start, json={}, headers={**headers, "Origin": "https://external.invalid"}).status_code == 403
        response = client.post(start, json={}, headers=headers)
        assert response.status_code == 200 and response.json()["password"] == secret
        assert secret not in response.json()["desktop_url"]
        assert secret not in json.dumps(response.json()["run"])
        assert response.headers["cache-control"] == "no-store"
        for path in ("/api/bootstrap", f"/api/runs/{run['id']}", f"/api/runs/{run['id']}/events", start):
            assert secret not in client.get(path).text
        generic = client.post(f"/api/runs/{run['id']}/control", json={"command": "takeover", "command_id": "generic-retry", "player_id": "player-1"}, headers=headers)
        assert generic.status_code == 422 and secret not in generic.text
        assert client.post(finish, json={}).status_code == 403
        handback = client.post(finish, json={"reason": "", "resume": True}, headers=headers)
        assert handback.status_code == 200 and handback.json()["state"] == "Running"
        assert secret not in handback.text
        assert not app.state.ai.backend.requests
    assert all(secret.encode() not in path.read_bytes() for path in environment[0].root.rglob("*") if path.is_file())


@pytest.mark.parametrize("failure", ["password", "url", "resume_type"])
def test_takeover_api_rejects_missing_setup_and_invalid_handback_without_control(environment, failure):
    app, run = takeover_app(environment)
    store, coord, _, desktops = environment
    if failure == "password":
        coord.credentials.remove("desktop:worker-1")
    elif failure == "url":
        worker = store.require("workers", "worker-1")
        worker["desktop_url"] += "?password=must-not-use-url-secrets"
        store.put("workers", worker)
    with TestClient(app) as client:
        path = f"/api/runs/{run['id']}/players/player-1/takeover" if failure != "resume_type" else f"/api/runs/{run['id']}/takeover/finish"
        response = client.post(path, json={"resume": "true"} if failure == "resume_type" else {}, headers={"X-ASHAD-Request": "1"})
        assert response.status_code == 422
        assert all(not desktop.calls for desktop in desktops.values())
        assert store.require("runs", run["id"])["state"] == "Running"
