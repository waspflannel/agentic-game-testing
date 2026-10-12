"""Goal retirement/recalculation with local game and inference doubles, no live actions."""
import asyncio
import copy
import time
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from ashad import subgoals as goals
from ashad.instances import InstanceRequest, Instances
from test_controller import environment  # noqa: F401
from test_instances import profile  # noqa: F401
from test_subgoals import condition, context, decision, observe, proposed
from test_ui_http import app_at


def stale_setup():
    plan = goals.new_plan("Have at least 10 wheat in your inventory.", "p", "run")
    goals.reconcile(plan, observe(), 1)
    return goals.apply_decision(plan, decision(plan, "expand", subgoals=[
        proposed("name", "", "Enter the character name", condition(), True)]), context())


def test_retiring_obsolete_setup_refocuses_without_fabricating_completion():
    plan = stale_setup()
    original = copy.deepcopy(plan)
    response = decision(plan, "retire")
    response.update(reason="The player is in the world; character setup no longer applies.",
                    evidence=[{"source": "observation", "path": "/ready", "quote": "true"}])
    revised = goals.apply_decision(plan, response, context())
    assert plan == original
    assert revised["primary_goal"] == plan["primary_goal"]
    retired = revised["goals"][1]
    assert retired["retired"] and retired["status"] == "blocked"
    assert retired["completion"] == original["goals"][1]["completion"]
    assert not retired.get("completion_evidence")
    assert retired["retirement_evidence"]["observation"]["observation_id"]
    goals.reconcile(revised, observe(), 2)
    assert revised["active_goal_id"] == "root" and goals.needs_planning(revised)
    with pytest.raises(ValueError, match="Decompose the primary"):
        goals.apply_decision(revised, decision(revised, "retire"), context())
    response["evidence"][0]["quote"] = "false"
    with pytest.raises(ValueError):
        goals.apply_decision(plan, response, context())
    response["evidence"][0]["quote"] = "true"
    response["completion"] = condition("world_ready")
    with pytest.raises(ValueError, match="preserve"):
        goals.apply_decision(plan, response, context())


async def recovery_instance(environment, profile):
    store, coordinator, _, _ = environment
    run = await Instances(coordinator).start(InstanceRequest(
        configuration_id=profile["id"], primary_goal="Have at least 10 wheat in your inventory.", request_id="recover"))
    await coordinator.admit(run)
    run = store.require("runs", run["id"])
    plan = stale_setup()
    plan.update(player_id="player-1", run_id=run["id"])
    goals.block(plan, "The character-name screen has gone; setup is obsolete.")
    run.update(state="Investigating", resume_state="Running", investigation_deadline=time.time() + 120,
               subgoal_plan=plan)
    store.put("runs", run)
    calls = []

    async def request(worker_id, method, path, body=None):
        calls.append((path, body))
        if path in {"/pause", "/heartbeat"}:
            return {"lifecycle": "paused"}
        if path == "/stop":
            return {"teardown_confirmed": True}
        assert path == "/game"
        if body["operation"] == "get_action_status":
            return {"status": "cancelled", "action_id": body["arguments"]["action_id"]}
        assert body["operation"] == "observe", "Recalculation cannot execute gameplay"
        return observe(run_id=run["id"], player_id="player-1")

    async def plan_subgoals(supplied, run_id):
        assert run_id == run["id"]
        assert supplied["goal"] == run["plan"]["primary_goal"]
        assert supplied["current_goal"]["id"] == "root"
        assert supplied["game_controls"] == run["game_controls"]["controls"]
        assert not coordinator.run_lock(run_id).locked(), "Monitor heartbeats must continue during inference"
        assert store.require("runs", run_id)["state"] == "Paused"
        ref = [{"source": "observation", "path": "/entities/0/state/recipes", "quote": "wheat"}]
        return {"goal_id": "root", "mode": "expand", "completion": supplied["current_goal"]["completion"],
                "evidence": ref, "reason": "The world is ready; pursue wheat rather than old setup.",
                "subgoals": [{"key": "wheat", "parent_key": "", "objective": "Obtain 10 wheat",
                              "completion": condition("inventory_quantity", "wheat", 10),
                              "evidence": ref, "actionable": True}]}

    coordinator.request = AsyncMock(side_effect=request)
    coordinator.ai.plan_subgoals = AsyncMock(side_effect=plan_subgoals)
    return store, coordinator, run, calls


@pytest.mark.asyncio
async def test_manual_recalculation_archives_old_plan_reobserves_and_never_resumes(environment, profile):
    store, coordinator, run, calls = await recovery_instance(environment, profile)
    ps = run["players"]["player-1"]
    ps["pending_action"] = {"action_id": "old-action", "expires_at": time.time() - 1}
    store.put("runs", run)
    revised = await coordinator.recalculate_goals(run["id"], "recalculate-1")
    assert revised["state"] == "Paused" and revised["investigation_deadline"] is None
    assert revised["goal_recalculation"]["status"] == "completed"
    assert revised["players"]["player-1"]["pending_action"] is None
    assert goals.active_goal(revised["subgoal_plan"])["objective"] == "Obtain 10 wheat"
    for field in ("plan", "approved_plan", "configuration", "model", "build"):
        assert revised[field] == run[field]
    assert revised["subgoal_plan"]["revision"] > run["subgoal_plan"]["revision"]
    assert [body["operation"] for path, body in calls if path == "/game"] == ["get_action_status", "observe", "observe"]
    archived = next(e["payload"] for e in store.events(run["id"], limit=1000) if e["kind"] == "subgoal_plan_recalculation_prepared")
    assert archived["previous_plan"] == run["subgoal_plan"]
    assert archived["retired_goal_ids"] == [run["subgoal_plan"]["active_goal_id"]]
    assert not archived["previous_plan"]["goals"][1].get("completion_evidence")
    assert (await coordinator.recalculate_goals(run["id"], "recalculate-1"))["subgoal_plan"] == revised["subgoal_plan"]
    assert coordinator.ai.plan_subgoals.await_count == 1
    assert not any(path in {"/resume", "/launch"} for path, _ in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["pause", "stale", "identity", "world", "screen", "model", "unknown_action"])
async def test_recalculation_failures_keep_original_plan_and_inputs_held(environment, profile, failure):
    store, coordinator, run, calls = await recovery_instance(environment, profile)
    real_request = coordinator.request.side_effect
    observed = 0

    async def request(worker, method, path, body=None):
        nonlocal observed
        result = await real_request(worker, method, path, body)
        if path == "/pause" and failure == "pause":
            return {"lifecycle": "running"}
        if path == "/game" and body["operation"] == "get_action_status":
            return {"status": "unknown"}
        if path == "/game" and body["operation"] == "observe":
            observed += 1
            if failure == "stale":
                result["timestamp"] -= 60
            if failure == "identity":
                result["player_id"] = "other-player"
            if failure == "world" and observed == 2:
                result["world_id"] = "other-world"
            if failure == "screen" and observed == 2:
                result["entities"].append({"id": "new-menu", "kind": "ui", "actions": ["ui_text"]})
        return result

    coordinator.request.side_effect = request
    if failure == "model":
        coordinator.ai.plan_subgoals.side_effect = ValueError("Invalid planner output")
    if failure == "unknown_action":
        run["players"]["player-1"]["pending_action"] = {"action_id": "unconfirmed"}
        store.put("runs", run)
    with pytest.raises(ValueError):
        await coordinator.recalculate_goals(run["id"], "recalculate-fail")
    latest = store.require("runs", run["id"])
    assert latest["subgoal_plan"] == run["subgoal_plan"]
    assert latest["state"] == "Paused" and latest["goal_recalculation"]["status"] == "failed"
    assert store.command("recalculate:" + run["id"], "recalculate-fail") is None
    assert not any(path == "/resume" for path, _ in calls)
    if failure == "unknown_action":
        assert latest["players"]["player-1"]["pending_action"]["action_id"] == "unconfirmed"
        assert coordinator.ai.plan_subgoals.await_count == 0
    # Same command id may safely retry a failed calculation; it never replays gameplay.
    coordinator.request.side_effect = real_request
    if failure != "model":
        assert (await coordinator.recalculate_goals(run["id"], "recalculate-fail"))["goal_recalculation"]["status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["human", "external", "ended", "unlaunched", "no_plan"])
async def test_recalculation_rejects_unavailable_owners_before_pause_or_inference(environment, profile, owner):
    store, coordinator, run, calls = await recovery_instance(environment, profile)
    if owner == "human":
        run["human_control"] = {"player_id": "player-1"}
    elif owner == "external":
        coordinator.structured.control_owners[(run["id"], "player-1")] = "mcp-owner"
    elif owner == "ended":
        run["state"] = "Ended"
    elif owner == "unlaunched":
        run["players"]["player-1"]["launched"] = False
    else:
        run.pop("subgoal_plan")
    store.put("runs", run)
    with pytest.raises(ValueError):
        await coordinator.recalculate_goals(run["id"], "not-allowed")
    assert not calls and coordinator.ai.plan_subgoals.await_count == 0
    assert not store.require("runs", run["id"]).get("goal_recalculation")


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["pause", "stop"])
async def test_recalculation_can_be_cancelled_and_does_not_hold_heartbeat_lock(environment, profile, command):
    store, coordinator, run, _ = await recovery_instance(environment, profile)
    entered = asyncio.Event()
    real_planner = coordinator.ai.plan_subgoals.side_effect

    async def slow_planner(context, run_id):
        assert not coordinator.run_lock(run_id).locked()
        entered.set()
        await asyncio.Future()

    coordinator.ai.plan_subgoals.side_effect = slow_planner
    task = asyncio.create_task(coordinator.recalculate_goals(run["id"], "cancel-me"))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        with pytest.raises(ValueError, match="already in progress"):
            await coordinator.recalculate_goals(run["id"], "cancel-me")
        with pytest.raises(ValueError, match="Wait for goal recalculation"):
            await coordinator.control(run["id"], "resume", command_id="unsafe-resume")
        await asyncio.wait_for(coordinator.control(run["id"], command, command_id="cancel-plan"), 2)
        with pytest.raises(asyncio.CancelledError):
            await task
        latest = store.require("runs", run["id"])
        assert latest["state"] == ("Paused" if command == "pause" else "Ended")
        assert latest["goal_recalculation"]["status"] == "cancelled"
        assert latest["subgoal_plan"] == run["subgoal_plan"]
        if command == "pause":
            coordinator.ai.plan_subgoals.side_effect = real_planner
            assert (await coordinator.recalculate_goals(run["id"], "cancel-me"))["goal_recalculation"]["status"] == "completed"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["archive", "acknowledgement"])
async def test_recalculation_durability_failure_keeps_original_plan(environment, profile, monkeypatch, failure):
    store, coordinator, run, _ = await recovery_instance(environment, profile)
    original_event, original_command = store.event, store.command

    def event(run_id, kind, *args, **kwargs):
        if failure == "archive" and kind == "subgoal_plan_recalculation_prepared":
            raise OSError("Archive append failed")
        return original_event(run_id, kind, *args, **kwargs)

    def command(scope, key, result=None):
        if failure == "acknowledgement" and result is not None and scope.startswith("recalculate:"):
            raise OSError("Acknowledgement commit failed")
        return original_command(scope, key, result)

    monkeypatch.setattr(store, "event", event)
    monkeypatch.setattr(store, "command", command)
    with pytest.raises(OSError):
        await coordinator.recalculate_goals(run["id"], "durable-plan")
    latest = store.require("runs", run["id"])
    assert latest["subgoal_plan"] == run["subgoal_plan"]
    assert latest["state"] == "Paused" and latest["goal_recalculation"]["status"] == "failed"
    assert store.command("recalculate:" + run["id"], "durable-plan") is None
    monkeypatch.setattr(store, "event", original_event)
    monkeypatch.setattr(store, "command", original_command)
    assert (await coordinator.recalculate_goals(run["id"], "durable-plan"))["goal_recalculation"]["status"] == "completed"


@pytest.mark.asyncio
async def test_cancel_during_error_cleanup_never_strands_planning_status(environment, profile):
    store, coordinator, run, _ = await recovery_instance(environment, profile)
    entered, fail = asyncio.Event(), asyncio.Event()
    original = coordinator.ai.plan_subgoals.side_effect

    async def failed_planner(context, run_id):
        entered.set()
        await fail.wait()
        raise ValueError("Planner output rejected")

    coordinator.ai.plan_subgoals.side_effect = failed_planner
    task = asyncio.create_task(coordinator.recalculate_goals(run["id"], "cleanup-race"))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        async with coordinator.run_lock(run["id"]):
            fail.set()
            await asyncio.sleep(0)  # Let error cleanup reach the occupied lifecycle lock.
            await asyncio.wait_for(coordinator.cancel_players(run["id"]), 2)
        with pytest.raises(asyncio.CancelledError):
            await task
        latest = store.require("runs", run["id"])
        assert latest["goal_recalculation"]["status"] == "cancelled"
        assert latest["subgoal_plan"] == run["subgoal_plan"]
        assert (run["id"], "player-1") not in coordinator.tasks
        coordinator.ai.plan_subgoals.side_effect = original
        assert (await coordinator.recalculate_goals(run["id"], "cleanup-race"))["goal_recalculation"]["status"] == "completed"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_restart_clears_abandoned_recalculation_and_keeps_old_plan(environment, profile):
    store, coordinator, run, _ = await recovery_instance(environment, profile)
    run.update(state="Paused", goal_recalculation={"command_id": "abandoned", "status": "planning"})
    store.put("runs", run)
    await coordinator.recover()
    recovered = store.require("runs", run["id"])
    assert recovered["goal_recalculation"]["status"] == "cancelled"
    assert recovered["subgoal_plan"] == run["subgoal_plan"]
    assert recovered["state"] == "Paused"
    assert (await coordinator.recalculate_goals(run["id"], "abandoned"))["goal_recalculation"]["status"] == "completed"


def test_recalculation_endpoint_validates_body_and_exposes_external_ownership(tmp_path):
    app = app_at(tmp_path)
    coordinator = app.state.coordinator
    coordinator.recalculate_goals = AsyncMock(return_value={"id": "r", "state": "Paused"})
    coordinator.structured.control_owners[("r", "p")] = "external-session"
    with TestClient(app, headers={"X-ASHAD-Request": "1"}) as client:
        for body in ({}, {"command_id": ""}, {"command_id": "x", "primary_goal": "replace goal"}, {"command_id": 7}):
            assert client.post("/api/runs/r/recalculate-goals", json=body).status_code == 422
        assert coordinator.recalculate_goals.await_count == 0
        result = client.post("/api/runs/r/recalculate-goals", json={"command_id": "same-id"})
        assert result.status_code == 200 and result.json()["external_control"] is True
        coordinator.recalculate_goals.assert_awaited_once_with("r", "same-id")


def invent_quotes(coordinator, every_call):
    valid = coordinator.ai.plan_subgoals.side_effect
    seen = 0

    async def planner(supplied, run_id):
        nonlocal seen
        answer = await valid(supplied, run_id)
        if every_call or not seen:
            answer["evidence"] = [{"source": "observation", "path": "/entities/0/state/recipes", "quote": "invented"}]
        seen += 1
        return answer

    coordinator.ai.plan_subgoals.side_effect = planner


@pytest.mark.asyncio
async def test_recalculation_retries_once_after_a_rejected_proposal(environment, profile):
    store, coordinator, run, calls = await recovery_instance(environment, profile)
    invent_quotes(coordinator, every_call=False)
    revised = await coordinator.recalculate_goals(run["id"], "recalculate-retry")
    assert revised["goal_recalculation"]["status"] == "completed"
    assert coordinator.ai.plan_subgoals.await_count == 2
    assert "previous_attempt_rejected" in coordinator.ai.plan_subgoals.await_args_list[1].args[0]
    rejected = [e["payload"] for e in store.events(run["id"], limit=1000) if e["kind"] == "subgoal_plan_rejected"]
    assert len(rejected) == 1
    assert rejected[0]["attempt"] == 1 and rejected[0]["goal_id"] == "root"
    assert rejected[0]["proposal"]["evidence"][0]["quote"] == "invented"
    assert goals.active_goal(revised["subgoal_plan"])["objective"] == "Obtain 10 wheat"


@pytest.mark.asyncio
async def test_recalculation_fails_in_plain_words_after_two_rejections(environment, profile):
    store, coordinator, run, calls = await recovery_instance(environment, profile)
    invent_quotes(coordinator, every_call=True)
    with pytest.raises(ValueError, match="could not verify"):
        await coordinator.recalculate_goals(run["id"], "recalculate-reject")
    latest = store.require("runs", run["id"])
    assert latest["goal_recalculation"]["status"] == "failed"
    assert "could not verify" in latest["goal_recalculation"]["error"]
    assert "\n" not in latest["goal_recalculation"]["error"]
    assert "could not verify" in latest["pause"]["reason"]
    assert latest["subgoal_plan"] == run["subgoal_plan"]
    assert coordinator.ai.plan_subgoals.await_count == 2
    rejected = [e["payload"] for e in store.events(run["id"], limit=1000) if e["kind"] == "subgoal_plan_rejected"]
    assert [r["attempt"] for r in rejected] == [1, 2]
    assert not any(path == "/resume" for path, _ in calls)
