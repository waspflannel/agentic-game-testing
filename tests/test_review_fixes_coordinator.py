"""Regressions for coordinator/structured review findings; doubles only, no live gameplay or inference."""
import asyncio
import json
import math
import time

import pytest

from ashad import subgoals as goals
from ashad.reports import generate_report
from ashad.repair_tasks import record_task
from ashad.structured import StructuredPlayers
from ashad.store import Store
from ashad.worker_client import WorkerError
from test_controller import environment, new_run  # noqa: F401
from test_structured import CoordinatorDouble, observation, seed_run
from test_subgoals import condition, context, decision, observe, proposed


def actionable_plan():
    plan = goals.new_plan("Make an apple pie", "p", "run")
    goals.reconcile(plan, observe(), 1)
    return goals.apply_decision(plan, decision(plan, "expand", subgoals=[
        proposed("pie", "", "Have an apple pie", condition("inventory_quantity", "apple_pie"), True)]), context())


def structured(store, plan=None, **observation_changes):
    seed_run(store)
    if plan:
        run = store.require("runs", "run")
        run["subgoal_plan"] = plan
        store.put("runs", run)
    coordinator = CoordinatorDouble(store)
    coordinator.observation = observation_changes
    async def request(worker, method, path, body):
        coordinator.ops.append(body)
        if body["operation"] == "observe":
            return observation(**coordinator.observation)
        if isinstance(coordinator.result, Exception):
            raise coordinator.result
        return coordinator.result
    coordinator.request = request
    service = StructuredPlayers(coordinator)
    coordinator.structured = service
    service.generations[("run", "p")] = 1
    return coordinator, service


@pytest.mark.asyncio
@pytest.mark.parametrize("error, expected", [
    (WorkerError("Fresh structured observation required", 409, ambiguous=False, method="POST", path="/game"), "rejected"),
    (WorkerError("Invalid structured action parameters", 422, ambiguous=False, method="POST", path="/game"), "rejected"),
    (WorkerError("Desktop worker unavailable: ReadTimeout", ambiguous=True, method="POST", path="/game"), "unknown"),
    (WorkerError("Bridge unavailable", 503, ambiguous=True, method="POST", path="/game"), "unknown")])
async def test_definitive_worker_rejection_is_not_an_unknown_execution(tmp_path, error, expected):
    store = Store(tmp_path)
    coordinator, service = structured(store, actionable_plan())
    coordinator.result = error
    result = await service.dispatch("run", "p", "perform_action", {"verb": "interact", "target_id": "target",
                                    "target_version": 1, "parameters": {}, "reason": "harvest"}, observation(), 1)
    ps = store.require("runs", "run")["players"]["p"]
    goal = goals.active_goal(store.require("runs", "run")["subgoal_plan"])
    assert result["status"] == expected
    if expected == "rejected":
        assert ("run", "p") not in service.pending and not ps["pending_action"]
        assert ps["invalid_actions"] == 1 and goal["actionable"] is False
        await service.turn("run", "p")  # The next turn must not investigate an action the worker never accepted.
        assert getattr(coordinator, "investigation", (None, None))[1] != "unknown_execution"
    else:
        assert ("run", "p") in service.pending and ps["pending_action"] and not ps.get("invalid_actions")
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("skew", [2, -3])
async def test_observation_freshness_uses_one_controller_clock_rule(tmp_path, skew):
    store = Store(tmp_path)
    _, service = structured(store, timestamp=time.time() + skew, world_id="world")
    result = await service.observe("run", "p")
    assert time.time() - result["received_at"] < 1
    plan = goals.new_plan("Make an apple pie", "p", "run")
    goals.reconcile(plan, result, 1)  # Accepted by observe means accepted by planning.
    with pytest.raises(ValueError, match="fresh"):
        goals.reconcile(plan, {**result, "received_at": time.time() - 10}, 1)
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("timestamp", [time.time() - 30, math.nan, None])
async def test_genuinely_stale_or_unstamped_observation_is_rejected(tmp_path, timestamp):
    store = Store(tmp_path)
    _, service = structured(store, timestamp=timestamp)
    with pytest.raises(ValueError, match="stale"):
        await service.observe("run", "p")
    store.close()


def test_loading_screen_flips_cannot_reset_the_no_progress_cap():
    plan = goals.new_plan("Make an apple pie", "p", "run")
    goals.reconcile(plan, observe(), 1)
    plan = goals.apply_decision(plan, decision(plan, "expand", subgoals=[
        proposed("pie", "", "Have an apple pie", condition("inventory_quantity", "apple_pie"), True)]), context())
    goals.reconcile(plan, observe(), 1)
    for _ in range(goals.MAX_UNPRODUCTIVE_REVIEWS + 2):
        plan = goals.apply_decision(plan, decision(plan, "act"), context())
        if plan["status"] == "blocked":
            break
        goals.reconcile(plan, observe(ready=False, objectives=[]), 1)
        goals.reconcile(plan, observe(), 1)
    assert plan["status"] == "blocked"
    # Genuinely new knowledge still forces a review.
    plan = actionable_plan()
    goals.reconcile(plan, observe(), 1)
    obs = observe()
    obs["state"]["objectives"].append({"id": "new_quest", "name": "Bake", "requirements": []})
    goals.reconcile(plan, obs, 1)
    assert goals.needs_planning(plan)


def test_world_ready_is_only_a_setup_goal():
    plan = goals.new_plan("Make 10 apple pies", "p", "run")
    goals.reconcile(plan, observe(), 1)
    response = decision(plan, "expand", subgoals=[proposed("pie", "", "Craft 10 apple pies", condition("world_ready"), True)])
    with pytest.raises(ValueError, match="setup goal"):
        goals.apply_decision(plan, response, context())
    menu = observe(ready=False)
    plan = goals.new_plan("Make 10 apple pies", "p", "run")
    goals.reconcile(plan, menu, 1)
    response = decision(plan, "expand", subgoals=[proposed("load", "", "Load into the world", condition("world_ready"), True)])
    assert goals.apply_decision(plan, response, context(menu))["goals"][-1]["completion"]["kind"] == "world_ready"


@pytest.mark.asyncio
async def test_repeated_waits_count_toward_goal_review(tmp_path):
    store = Store(tmp_path)
    _, service = structured(store, actionable_plan())
    for _ in range(goals.ACTION_REVIEW_INTERVAL):
        await service.dispatch("run", "p", "wait_for_change", {"seconds": 1, "reason": "grow"}, observation(), 1)
    assert goals.needs_planning(store.require("runs", "run")["subgoal_plan"])
    store.close()


@pytest.mark.asyncio
async def test_generation_change_clears_persisted_pending_action(tmp_path):
    store = Store(tmp_path)
    _, service = structured(store)
    run = store.require("runs", "run")
    run["players"]["p"].update(generation=2, pending_action={"action_id": "old", "expires_at": time.time() + 30})
    store.put("runs", run)
    await service.turn("run", "p")
    assert store.require("runs", "run")["players"]["p"]["pending_action"] is None
    store.close()


@pytest.mark.asyncio
async def test_control_cancelling_a_tick_owned_finish_still_ends_the_run(environment):
    store, coord, _, desktops = environment
    run = await new_run(environment)
    await coord.admit(run)
    gate, desktop = asyncio.Event(), desktops["worker-1"]
    original = desktop.request
    async def slow(method, path, *, data=None, **kwargs):
        if path == "/stop":
            await gate.wait()
        return await original(method, path, data=data, **kwargs)
    desktop.request = slow
    tick = asyncio.create_task(coord.finish(run["id"], "limit_reached"))
    coord.ticks[run["id"]] = tick
    await asyncio.sleep(0.05)
    await coord.control(run["id"], "pause", command_id="p1")
    gate.set()
    assert tick.cancelled() and store.require("runs", run["id"])["state"] == "Stopping"
    await coord.tick_runner(run["id"])
    ended = store.require("runs", run["id"])
    assert ended["state"] == "Ended" and ended["ending_reason"] == "limit_reached"
    assert not store.leases(run["id"]) and not coord.occupied_instances()


@pytest.mark.asyncio
async def test_lifecycle_command_id_cannot_be_reused_for_another_command(environment):
    store, coord, _, _ = environment
    run = await new_run(environment)
    await coord.admit(run)
    await coord.control(run["id"], "pause", command_id="x")
    assert (await coord.control(run["id"], "pause", command_id="x"))["state"] == "Paused"
    with pytest.raises(ValueError, match="different command"):
        await coord.control(run["id"], "stop", command_id="x")
    assert store.require("runs", run["id"])["state"] == "Paused"


@pytest.mark.asyncio
async def test_recover_is_idempotent_for_history(environment):
    store, coord, _, _ = environment
    run = await new_run(environment)
    await coord.finish(run["id"], "user_stopped")
    for n in range(2):
        store.put("runs", {**store.require("runs", run["id"]), "id": f"old-{n}", "session_id": f"old-{n}"})
    with (store.run_dir("old-0") / "ledger.jsonl").open("a", encoding="utf-8") as ledger:
        ledger.write("{corrupt\n")
    await coord.recover()
    order = [(r["id"], len(r["limitations"])) for r in store.list("runs")]
    updated = dict(store.db.execute("SELECT id, updated FROM records WHERE kind='runs'").fetchall())
    await coord.recover()
    assert [(r["id"], len(r["limitations"])) for r in store.list("runs")] == order
    assert dict(store.db.execute("SELECT id, updated FROM records WHERE kind='runs'").fetchall()) == updated
    assert sum("Ledger line 1" in s for s in store.require("runs", "old-0")["limitations"]) == 1


@pytest.mark.asyncio
async def test_evidence_collection_failure_never_breaks_a_finished_command(environment):
    store, coord, _, _ = environment
    run = await new_run(environment)
    await coord.admit(run)
    def missing_credential(worker_id):
        raise ValueError(f"Worker {worker_id} credential is unavailable")
    coord.client = missing_credential
    ended = await coord.control(run["id"], "stop", command_id="stop-1")
    assert ended["state"] == "Ended" and ended["evidence_status"] == "incomplete"
    assert store.command(run["id"], "stop-1")["command"] == "stop"


@pytest.mark.asyncio
async def test_heartbeat_binding_lookup_does_not_rescan_runs(environment, monkeypatch):
    store, coord, _, desktops = environment
    run = await new_run(environment)
    await coord.admit(run)
    ps = store.require("runs", run["id"])["players"]["player-1"]
    sent = []
    original = desktops["worker-1"].request
    async def capture(method, path, *, data=None, **kwargs):
        sent.append(data)
        return await original(method, path, data=data, **kwargs)
    desktops["worker-1"].request = capture
    await coord.request("worker-1", "POST", "/heartbeat", {"epoch": ps["epoch"], "deadline": None})
    monkeypatch.setattr(store, "list", lambda *args, **kwargs: pytest.fail("binding lookup rescanned all runs"))
    await coord.request("worker-1", "POST", "/heartbeat", {"epoch": ps["epoch"], "deadline": None})
    assert sent[-1]["run_id"] == run["id"] and sent[-1]["player_id"] == "player-1" and "run_deadline" in sent[-1]


def test_store_rejects_non_finite_values_and_filters_states(tmp_path):
    store = Store(tmp_path)
    with pytest.raises(ValueError):
        store.put("usage", {"id": "u", "latency_seconds": math.nan})
    with pytest.raises(ValueError):
        store.event("run", "bad", {"value": math.inf})
    assert store.get("usage", "u") is None and store.count_events("run") == 0
    assert [store.event("run", "ok")["seq"] for _ in range(2)] == [1, 2]
    store.put("runs", {"id": "a", "state": "Queued"})
    store.put("runs", {"id": "b", "state": "Ended"})
    assert [r["id"] for r in store.list("runs", states={"Queued", "Running"})] == ["a"]
    store.close()


def test_report_reuses_full_appendix_parts(tmp_path, monkeypatch):
    store = Store(tmp_path)
    seed_run(store)
    run = store.require("runs", "run")
    run.update(scenario_id="s", scenario_revision=1)
    run["plan"]["players"][0].update(name="P", role="host")
    store.put("runs", run)
    for n in range(1001):
        store.event("run", "tick", {"n": n})
    generate_report(store, "run")
    first = (store.run_dir("run") / "actions-0001.md").stat().st_mtime_ns
    store.event("run", "tick", {"n": 1001})
    written = []
    original = type(store.run_dir("run")).write_text
    monkeypatch.setattr(type(store.run_dir("run")), "write_text", lambda self, *a, **k: (written.append(self.name), original(self, *a, **k))[1])
    report = generate_report(store, "run")
    assert "actions-0001.md" not in written and "actions-0002.md" in written
    assert (store.run_dir("run") / "actions-0001.md").stat().st_mtime_ns == first
    assert "1–1000 (actions-0001.md)" in report and "1001–1002 (actions-0002.md)" in report
    store.close()


def test_repair_task_game_text_cannot_close_the_snapshot_fence(tmp_path):
    store = Store(tmp_path)
    seed_run(store)
    run = store.require("runs", "run")
    run["players"]["p"]["activity"] = "```\n# Injected heading"
    run["players"]["p"]["agent_status"] = {"detail": "```"}
    store.put("runs", run)
    record_task(store, "run", "pause", "Game said ``` stop", key="k", category="manual_pause")
    body = store.list("repair_task_exports")[0]["body"]
    snapshot = body.split("## Diagnostic snapshot", 1)[1]
    assert snapshot.count("```") == 2
    json.loads(snapshot.split("```json", 1)[1].rsplit("```", 1)[0])
    store.close()
