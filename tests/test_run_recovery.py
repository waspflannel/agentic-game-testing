"""Run recovery contracts use temporary stores and doubles, never live workers."""
import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from ashad.app import create_app
from ashad.worker_client import WorkerError
from test_controller import environment, new_run  # noqa: F401
from test_acceptance_edges import ready_run, game_context_worker


@pytest.mark.asyncio
async def test_pause_cancels_inference_and_resume_uses_optional_instructions(environment, monkeypatch):
    store, coord, _, desktops = environment
    run = await ready_run(environment)
    game_context_worker(monkeypatch, desktops["worker-1"], run)
    started = asyncio.Event()

    async def waiting_decision(*args):
        started.set()
        await asyncio.Event().wait()

    coord.ai.player_turn = waiting_decision
    task = asyncio.create_task(coord.player_turn(run["id"], "player-1"))
    coord.tasks[(run["id"], "player-1")] = task
    await started.wait()
    paused = await coord.control(run["id"], "pause", command_id="pause")
    assert task.cancelled()
    assert paused["pause"]["reason"] == "You paused the bots."
    assert paused["players"]["player-1"]["agent_status"]["phase"] == "paused"
    await coord.control(run["id"], "resume", "Try the keyboard control", "resume")
    coord.ai.player_turn = AsyncMock(return_value={"status": "selected"})
    await coord.player_turn(run["id"], "player-1")
    assert coord.ai.player_turn.call_args.args[0]["operator_instruction"] == {"text": "Try the keyboard control", "after": "resume"}
    assert "recovery_instruction" not in coord.ai.player_turn.call_args.args[0]
    assert coord.ai.player_turn.call_args.args[0]["transport"]["mode"] == "enet"
    assert store.require("runs", run["id"])["pause"] is None
    assert any(e["kind"] == "agent_task_cancelled" for e in store.events(run["id"]))
    await coord.control(run["id"], "pause", command_id="pause-again")
    resumed = await coord.control(run["id"], "resume", command_id="resume-blank")
    assert resumed["recovery_instruction"] == ""
    assert resumed["recovery_after"] == ""
    coord.ai.player_turn = AsyncMock(return_value={"status": "selected"})
    await coord.player_turn(run["id"], "player-1")
    assert "operator_instruction" not in coord.ai.player_turn.call_args.args[0]


@pytest.mark.asyncio
async def test_overlong_instruction_is_rejected_before_any_side_effect(environment):
    store, coord, _, _ = environment
    run = await ready_run(environment)
    await coord.control(run["id"], "pause", command_id="pause")
    before = len(store.require("runs", run["id"])["interventions"])
    with pytest.raises(ValueError, match="under 4000"):
        await coord.control(run["id"], "resume", "x" * 4001, "too-long")
    assert len(store.require("runs", run["id"])["interventions"]) == before
    assert not store.command(run["id"], "too-long")
    assert store.require("runs", run["id"])["state"] == "Paused"
    resumed = await coord.control(run["id"], "resume", "x" * 4000, "ok-length")
    assert resumed["state"] == "Running"


@pytest.mark.asyncio
async def test_resume_failure_holds_every_worker_and_keeps_deadline(environment, monkeypatch):
    store, coord, _, desktops = environment
    run = await ready_run(environment, count=2)
    await coord.investigate(run["id"], "provider_failure", "Provider temporarily unavailable", ["player-1"])
    deadline = store.require("runs", run["id"])["investigation_deadline"]
    original = desktops["worker-2"].request

    async def fail_resume(method, path, **kwargs):
        if path == "/resume":
            raise OSError("Desktop unreachable")
        return await original(method, path, **kwargs)

    monkeypatch.setattr(desktops["worker-2"], "request", fail_resume)
    with pytest.raises(ValueError, match="bots remain paused"):
        await coord.control(run["id"], "resume", command_id="retry")
    actual = store.require("runs", run["id"])
    assert actual["state"] == "Investigating" and actual["investigation_deadline"] == deadline
    assert "Desktop unreachable" in actual["pause"]["reason"]
    assert desktops["worker-1"].calls[-2][0] == "/pause"
    assert store.leases(run["id"])


@pytest.mark.asyncio
async def test_terminate_http_needs_no_reason_and_retains_unconfirmed_lease(environment):
    store, coord, _, desktops = environment
    run = await new_run(environment)
    await coord.admit(run)
    desktops["worker-1"].fail_stop = True
    app = create_app(store.root, coord.credentials, start_services=False)
    app.state.coordinator.client_factory = coord.client_factory
    with TestClient(app) as client:
        result = client.post(f"/api/runs/{run['id']}/control", headers={"X-ASHAD-Request": "1"},
                             json={"command": "terminate", "command_id": "terminate"})
        assert result.status_code == 200
        assert result.json()["state"] == "Ended"
        assert result.json()["interventions"][-1]["reason"] == ""
        assert result.json()["players"]["player-1"]["teardown_confirmed"] is False
    assert store.leases(run["id"])


@pytest.mark.asyncio
async def test_unexpected_agent_exit_is_durable_and_actionable(environment, monkeypatch):
    store, coord, _, _ = environment
    run = await ready_run(environment)
    monkeypatch.setattr(coord.structured, "turn", AsyncMock(side_effect=RuntimeError("broken turn")))
    await coord.player_turn(run["id"], "player-1")
    actual = store.require("runs", run["id"])
    assert actual["state"] == "Investigating"
    assert "broken turn" in actual["pause"]["reason"]
    assert actual["pause"]["players"] == ["player-1"] and actual["pause"]["recovery"]
    assert "broken turn" in actual["players"]["player-1"]["agent_status"]["last_error"]
    assert any(e["kind"] == "agent_task_failed" for e in store.events(run["id"]))


@pytest.mark.asyncio
async def test_expired_pending_action_pauses_instead_of_waiting_forever(environment, monkeypatch):
    store, coord, _, desktops = environment
    run = await ready_run(environment)
    game_context_worker(monkeypatch, desktops["worker-1"], run)
    original = desktops["worker-1"].request

    async def stuck_action(method, path, *, data=None, **kwargs):
        if path == "/game" and data["operation"] == "get_action_status":
            return {"status": "running", "expires_at": time.time() - 60}
        return await original(method, path, data=data, **kwargs)

    monkeypatch.setattr(desktops["worker-1"], "request", stuck_action)
    key = (run["id"], "player-1")
    coord.structured.generations[key] = run["players"]["player-1"]["generation"]
    coord.structured.pending[key] = "unfinished-action"
    await coord.player_turn(*key)
    actual = store.require("runs", run["id"])
    assert actual["state"] == "Investigating"
    assert actual["pause"]["category"] == "unknown_execution"
    assert "execution deadline" in actual["pause"]["reason"]


@pytest.mark.asyncio
async def test_pause_during_reconciliation_cannot_deliver_stale_input(environment, monkeypatch):
    store, coord, _, desktops = environment
    run = await ready_run(environment)
    observation = game_context_worker(monkeypatch, desktops["worker-1"], run)
    original = desktops["worker-1"].request

    async def pause_while_reconciling(method, path, *, data=None, **kwargs):
        if path == "/game" and data["operation"] == "get_action_status":
            await coord.control(run["id"], "pause", command_id="pause-during-action")
            return {"status": "completed"}
        return await original(method, path, data=data, **kwargs)

    monkeypatch.setattr(desktops["worker-1"], "request", pause_while_reconciling)
    coord.structured.pending[(run["id"], "player-1")] = "previous-action"
    result = await coord.structured.dispatch(run["id"], "player-1", "perform_action",
        {"verb": "input", "parameters": {"name": "ui_accept"}}, observation, run["players"]["player-1"]["generation"])
    assert result["status"] == "cancelled"
    assert not any(path == "/game" and body["operation"] == "perform_action" for path, body in desktops["worker-1"].calls)


@pytest.mark.asyncio
async def test_waiting_agent_reports_reason_and_wakeup_time(environment, monkeypatch):
    store, coord, _, desktops = environment
    run = await ready_run(environment)
    observation = game_context_worker(monkeypatch, desktops["worker-1"], run)
    key = (run["id"], "player-1")
    generation = run["players"]["player-1"]["generation"]
    coord.structured.generations[key] = generation
    await coord.structured.dispatch(*key, "wait_for_change", {"seconds": 20, "reason": "Waiting for crops"}, observation, generation)
    coord.ai.player_turn = AsyncMock()
    await coord.player_turn(*key)
    status = store.require("runs", run["id"])["players"]["player-1"]["agent_status"]
    assert status["phase"] == "waiting" and status["detail"] == "Waiting for crops"
    assert status["wait_until"] > time.time()
    coord.ai.player_turn.assert_not_called()


@pytest.mark.asyncio
async def test_structured_observation_can_corroborate_run_completion(environment, monkeypatch):
    store, coord, _, desktops = environment
    run = await ready_run(environment, completed=["demo_wrapup_moar"])
    run["players"]["player-1"]["last_observation"] = None
    store.put("runs", run)
    game_context_worker(monkeypatch, desktops["worker-1"], run)
    await coord.structured.observe(run["id"], "player-1")
    await coord.monitor(store.require("runs", run["id"]))
    actual = store.require("runs", run["id"])
    assert actual["ending_reason"] == "completed" and actual["demo_complete"]


@pytest.mark.asyncio
async def test_only_explicit_bridge_startup_error_is_retried(environment, monkeypatch):
    store, coord, _, _ = environment
    run = await ready_run(environment)
    monkeypatch.setattr(coord.structured, "observe", AsyncMock(side_effect=WorkerError(
        "Listener starting", 503, method="POST", path="/game", code="game_bridge_starting")))
    await coord.player_turn(run["id"], "player-1")
    actual = store.require("runs", run["id"])
    assert actual["state"] == "Running"
    assert actual["players"]["player-1"]["agent_status"]["phase"] == "startup_retry"
    coord.structured.observe.side_effect = WorkerError("Internal Server Error", 500, method="POST", path="/game")
    await coord.player_turn(run["id"], "player-1")
    actual = store.require("runs", run["id"])
    assert actual["state"] == "Investigating"
    assert "POST /game (HTTP 500)" in actual["pause"]["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,args,phase", [("wait_for_change", {"seconds": 20, "reason": "Wait for crops"}, "waiting"),
                                           ("perform_action", {"verb": "input", "parameters": {"name": "ui_accept"}}, "acting")])
async def test_completed_decision_timestamp_preserves_action_or_wait_status(environment, monkeypatch, tool, args, phase):
    store, coord, _, desktops = environment
    run = await ready_run(environment)
    game_context_worker(monkeypatch, desktops["worker-1"], run)

    async def decision(context, dispatch, *unused):
        return await dispatch(tool, args)

    coord.ai.player_turn = decision
    await coord.player_turn(run["id"], "player-1")
    status = store.require("runs", run["id"])["players"]["player-1"]["agent_status"]
    assert status["phase"] == phase and status["completed_at"] <= time.time()


@pytest.mark.asyncio
async def test_unknown_action_retains_diagnostic_and_pending_until_confirmed(environment, monkeypatch):
    store, coord, _, desktops = environment
    run = await ready_run(environment)
    observation = game_context_worker(monkeypatch, desktops["worker-1"], run)
    original = desktops["worker-1"].request

    async def lost_ack(method, path, *, data=None, **kwargs):
        if path == "/game" and data["operation"] == "perform_action":
            raise WorkerError("Bridge unavailable", 503, method="POST", path="/game", code="game_bridge_unavailable")
        if path == "/game" and data["operation"] == "get_action_status":
            return {"status": "completed"}
        return await original(method, path, data=data, **kwargs)

    monkeypatch.setattr(desktops["worker-1"], "request", lost_ack)
    key = (run["id"], "player-1")
    generation = run["players"]["player-1"]["generation"]
    coord.structured.generations[key] = generation
    result = await coord.structured.dispatch(*key, "perform_action", {"verb": "input", "parameters": {"name": "ui_accept"}}, observation, generation)
    assert result["status"] == "unknown"
    assert "POST /game (HTTP 503)" in result["error"] and result["error_code"] == "game_bridge_unavailable"
    assert store.require("runs", run["id"])["players"]["player-1"]["pending_action"]
    coord.ai.player_turn = AsyncMock(return_value=None)
    await coord.player_turn(*key)
    assert store.require("runs", run["id"])["players"]["player-1"]["pending_action"] is None
    assert key not in coord.structured.pending
