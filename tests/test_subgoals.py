"""Hierarchical planning checks use recorded-shape fixtures, never live inference/gameplay."""
import asyncio
import copy
import json
import time
from unittest.mock import AsyncMock

import pytest

from ashad import subgoals as goals
from ashad.ai import ProviderError
from ashad.structured import StructuredPlayers
from ashad.store import Store
from test_structured import CoordinatorDouble, seed_run
from test_controller import environment  # noqa: F401
from test_instances import profile  # noqa: F401
from ashad.instances import InstanceRequest, Instances


def condition(kind="unknown", target="", quantity=1, collection="", matches=None, description="Verify the requested game effect"):
    return {"kind": kind, "target": target, "quantity": quantity, "collection": collection,
            "matches": matches or [], "description": description}


def observe(**changes):
    return {"schema_version": 1, "run_id": "run", "player_id": "p", "observation_id": "g:obs:1",
            "generation": "g", "world_id": "world", "timestamp": time.time(), "ready": True, "complete": True,
            "state": {"player": {"inventory": [{"item_id": "wheat_seed", "count": 10}], "hotbar": []},
                      "objectives": [{"id": "wheat_tutorial", "name": "Harvest wheat", "requirements": [{"description": "Harvest 10 wheat", "progress": "0/10"}]}],
                      "completed_quests": [], "tiles": [{"id": "tile-1", "ground": True, "state": "UNTILLED", "plant": ""}]},
            "entities": [{"id": "oven", "kind": "crafter", "version": 1, "actions": ["interact"], "state": {"recipes": [
                {"id": "pie_recipe", "name": "Apple pie", "output": "apple_pie", "count": 1,
                 "ingredients": [{"item_id": "flour", "count": 1}, {"item_id": "apple", "count": 2}]},
                {"id": "flour_recipe", "name": "Flour", "output": "flour", "count": 1,
                 "ingredients": [{"item_id": "wheat", "count": 10}]}]}}], **changes}


def evidence():
    return [{"source": "observation", "path": "/entities/0/state/recipes", "quote": "apple_pie"},
            {"source": "controls", "path": "", "quote": "Tilled soil is reported as TILLED."}]


def context(obs=None):
    return {"observation": obs or observe(), "game_controls": "Tilled soil is reported as TILLED. Use normal controls.", "goal": "Make an apple pie"}


def proposed(key, parent_key, objective, completion, actionable=False):
    return {"key": key, "parent_key": parent_key, "objective": objective, "completion": completion,
            "evidence": evidence(), "actionable": actionable}


def decision(plan, mode="act", completion=None, subgoals=None):
    active = goals.active_goal(plan)
    return {"goal_id": active["id"], "mode": mode, "completion": completion or active["completion"],
            "evidence": evidence(), "reason": "Follow the observed recipe and missing prerequisites.", "subgoals": subgoals or []}


def hierarchy():
    plan = goals.new_plan("Make an apple pie", "p", "run")
    goals.reconcile(plan, observe(), 1)
    response = decision(plan, "expand", subgoals=[
        proposed("pie", "", "Have an apple pie", condition("inventory_quantity", "apple_pie")),
        proposed("flour", "pie", "Obtain flour", condition("inventory_quantity", "flour")),
        proposed("wheat", "flour", "Obtain 10 wheat", condition("inventory_quantity", "wheat", 10)),
        proposed("till", "wheat", "Till 10 tiles", condition("observed_count", quantity=10, collection="tiles", matches=[{"field": "state", "value": "TILLED"}]), True),
        proposed("plant", "wheat", "Plant wheat in 10 tiles", condition("observed_count", quantity=10, collection="tiles", matches=[{"field": "plant", "value": "wheat"}]))])
    return goals.apply_decision(plan, response, context())


def test_recursive_goals_execute_leaves_and_verify_each_parent_independently():
    plan = hierarchy()
    assert [g["objective"] for g in goals.goal_path(plan)] == ["Make an apple pie", "Have an apple pie", "Obtain flour", "Obtain 10 wheat", "Till 10 tiles"]
    obs = observe()
    goals.reconcile(plan, obs, 1)
    assert not goals.needs_planning(plan)
    # Delivered input and an untruncated snapshot are not goal success.
    obs["active_action"] = {"status": "completed"}
    assert goals.reconcile(plan, obs, 1) == []
    obs["state"]["tiles"] = [{"id": f"tile-{n}", "state": "TILLED", "plant": ""} for n in range(10)]
    assert len(goals.reconcile(plan, obs, 1)) == 1
    assert goals.active_goal(plan)["objective"] == "Plant wheat in 10 tiles"
    assert goals.needs_planning(plan), "New siblings must reassess prerequisites, not blindly replay old plans"
    plan = goals.apply_decision(plan, decision(plan), context(obs))
    for tile in obs["state"]["tiles"]:
        tile["plant"] = "wheat"
    goals.reconcile(plan, obs, 1)
    assert goals.active_goal(plan)["objective"] == "Obtain 10 wheat"
    assert plan["status"] != "completed", "Completed prerequisites do not prove the parent outcome"
    obs["state"]["player"]["inventory"] = [{"item_id": "wheat", "count": 8}]
    obs["state"]["player"]["hotbar"] = [{"item_id": "wheat", "count": 2}]
    goals.reconcile(plan, obs, 1)
    assert goals.active_goal(plan)["objective"] == "Obtain flour"
    # Ingredients can be consumed; earlier achievements retain their observed evidence.
    obs["state"]["player"].update(inventory=[{"item_id": "flour", "count": 1}], hotbar=[])
    goals.reconcile(plan, obs, 1)
    assert goals.active_goal(plan)["objective"] == "Have an apple pie"
    obs["state"]["player"]["inventory"] = [{"item_id": "apple_pie", "count": 1}]
    goals.reconcile(plan, obs, 1)
    assert plan["status"] == "active" and plan["active_goal_id"] == "root"
    assert all(g.get("completion_evidence", {}).get("observation_id") for g in plan["goals"][1:])
    assert not plan["goals"][0].get("completion_evidence"), "Having an item doesn't prove arbitrary production/tutorial intent"



def test_unknown_primary_does_not_complete_when_known_children_finish():
    plan = goals.new_plan("Finish the tutorial", "p", "run")
    goals.reconcile(plan, observe(), 1)
    response = decision(plan, "expand", subgoals=[proposed("wheat", "", "Obtain wheat", condition("inventory_quantity", "wheat", 10), True)])
    plan = goals.apply_decision(plan, response, context())
    obs = observe()
    obs["state"]["player"]["inventory"] = [{"item_id": "wheat", "count": 10}]
    goals.reconcile(plan, obs, 1)
    assert goals.active_goal(plan)["id"] == "root"
    assert plan["status"] == "active" and goals.needs_planning(plan)


def test_conditions_require_real_typed_ready_evidence_not_absence_or_ui_complete():
    obs = observe()
    quest = condition("quest_completed", "wheat_tutorial")
    obs["state"]["objectives"] = []
    assert goals.check_completion(quest, obs) == (False, 0)
    obs["state"]["completed_quests"] = ["wheat_tutorial"]
    assert goals.check_completion(quest, obs)[0]
    obs["ready"] = False
    assert not goals.check_completion(quest, obs)[0]
    obs["ready"] = True
    inventory = condition("inventory_quantity", "wheat", 10)
    obs["state"]["player"].update(inventory=[{"item_id": "wheat", "count": 10}], hotbar=[])
    assert goals.check_completion(inventory, obs)[0]
    assert not goals.check_completion(condition("inventory_quantity", "flour"), obs)[0]
    del obs["state"]["player"]["hotbar"]
    assert goals.check_completion(inventory, obs) == (False, None)
    tile = condition("observed_count", collection="tiles", matches=[{"field": "harvestable", "value": True}])
    obs["state"]["tiles"] = [{"harvestable": 1}, {}]
    assert not goals.check_completion(tile, obs)[0]
    assert not goals.check_completion(condition(), obs)[0]


@pytest.mark.parametrize("damage", ["invented_quote", "missing_path", "invented_target", "invented_quest", "cycle", "ancestor", "model_status", "unsupported_field"])
def test_invalid_plans_leave_persisted_hierarchy_unchanged(damage):
    plan = goals.new_plan("Make an apple pie", "p", "run")
    goals.reconcile(plan, observe(), 1)
    before = copy.deepcopy(plan)
    response = decision(plan, "expand", subgoals=[proposed("wheat", "", "Obtain wheat", condition("inventory_quantity", "wheat", 10), True)])
    child = response["subgoals"][0]
    if damage == "invented_quote":
        child["evidence"][0]["quote"] = "secret cheat recipe"
    elif damage == "missing_path":
        child["evidence"][0]["path"] = "/telemetry/private_inventory"
    elif damage == "invented_target":
        child["completion"]["target"] = "unseen_diamond"
    elif damage == "invented_quest":
        child["completion"] = condition("quest_completed", "wheat")
    elif damage == "cycle":
        child["parent_key"] = "wheat"
    elif damage == "ancestor":
        child["objective"] = plan["primary_goal"]
    elif damage == "model_status":
        child["status"] = "completed"
    else:
        child["completion"] = condition("observed_count", collection="tiles", matches=[{"field": "__import__", "value": "os"}])
    with pytest.raises(ValueError):
        goals.apply_decision(plan, response, context())
    assert plan == before


def test_cannot_weaken_goal_condition_and_bounds_stop_recursive_planning():
    plan = hierarchy()
    response = decision(plan, completion=condition("observed_count", collection="tiles", matches=[{"field": "state", "value": "TILLED"}]))
    with pytest.raises(ValueError, match="weakened"):
        goals.apply_decision(plan, response, context())
    root = goals.new_plan("Make an apple pie", "p", "run")
    goals.reconcile(root, observe(), 1)
    response = decision(root, "expand", subgoals=[proposed(f"g{n}", f"g{n-1}" if n else "", f"Prerequisite {n}", condition()) for n in range(goals.MAX_DEPTH + 1)])
    with pytest.raises(ValueError, match="deep"):
        goals.apply_decision(root, response, context())
    # Repeated claimed executable strategies with no observed progress cannot run forever.
    root = goals.apply_decision(root, decision(root, "expand", subgoals=[
        proposed("discover", "", "Discover the required recipe", condition(), True)]), context())
    for _ in range(goals.MAX_UNPRODUCTIVE_REVIEWS + 1):
        root = goals.apply_decision(root, decision(root), context())
    assert root["status"] == "blocked"


def test_rebinding_resume_and_world_change_never_reuse_stale_proof():
    plan = hierarchy()
    obs = observe()
    goals.reconcile(plan, obs, 1)
    for key, value in (("timestamp", time.time()-10), ("run_id", "other"), ("player_id", "other")):
        with pytest.raises(ValueError):
            goals.reconcile(copy.deepcopy(plan), {**obs, key: value}, 1)
    resumed = copy.deepcopy(plan)
    goals.reconcile(resumed, observe(), 2)
    assert goals.needs_planning(resumed)
    rebound = copy.deepcopy(plan)
    goals.reconcile(rebound, observe(generation="new", observation_id="new:obs:1"), 1)
    assert goals.active_goal(rebound)["completion"]["kind"] == "unknown"
    goals.reconcile(plan, observe(world_id="different", generation="new"), 2)
    assert len(plan["goals"]) == 1 and plan["goals"][0]["completion"]["kind"] == "unknown"
    # Real game unloading passes through an empty world ID before loading a new world.
    finished = hierarchy()
    old_world = observe()
    old_world["state"]["player"]["inventory"] = [{"item_id": "apple_pie", "count": 1}]
    goals.reconcile(finished, old_world, 1)
    assert any(g["status"] == "completed" for g in finished["goals"])
    goals.reconcile(finished, observe(world_id="", generation="menu", ready=False), 1)
    assert finished["world_id"] == "world"
    goals.reconcile(finished, observe(world_id="different", generation="new"), 1)
    assert len(finished["goals"]) == 1 and finished["status"] == "active"
    assert not finished["goals"][0].get("completion_evidence")


def test_free_text_root_and_unproven_quantities_cannot_silently_end_the_work():
    plan = goals.new_plan("Make 10 apple pies", "p", "run")
    goals.reconcile(plan, observe(), 1)
    with pytest.raises(ValueError, match="Decompose the primary"):
        goals.apply_decision(plan, decision(plan), context())
    for invented_finish in (condition("world_ready"), condition("inventory_quantity", "apple_pie")):
        with pytest.raises(ValueError, match="manual acceptance"):
            goals.apply_decision(plan, decision(plan, completion=invented_finish), context())
    for objective, count, expected in (("Obtain 10 wheat", 1, "explicit quantity"), ("Obtain wheat", 500, "lacks cited")):
        response = decision(plan, "expand", subgoals=[proposed("wheat", "", objective, condition("inventory_quantity", "wheat", count), True)])
        with pytest.raises(ValueError, match=expected):
            goals.apply_decision(plan, response, context())


def test_recipe_quantities_scale_to_parent_and_reject_short_ingredient_goals():
    plan = goals.new_plan("Make 3 apple pies", "p", "run")
    goals.reconcile(plan, observe(), 1)
    supplied = context()
    supplied["goal"] = plan["primary_goal"]
    response = decision(plan, "expand", subgoals=[
        proposed("pie", "", "Have 3 apple pies", condition("inventory_quantity", "apple_pie", 3)),
        proposed("flour", "pie", "Obtain flour", condition("inventory_quantity", "flour", 1), True)])
    with pytest.raises(ValueError, match="recipe's requirement"):
        goals.apply_decision(plan, response, supplied)
    response["subgoals"][1]["completion"]["quantity"] = 3
    response["subgoals"][1]["actionable"] = False
    response["subgoals"].append(proposed("wheat", "flour", "Obtain 30 wheat", condition("inventory_quantity", "wheat", 30), True))
    accepted = goals.apply_decision(plan, response, supplied)
    assert goals.active_goal(accepted)["completion"]["quantity"] == 30


def test_discovering_parent_condition_and_expanding_it_uses_new_quantities_atomically():
    plan = goals.new_plan("Make 3 apple pies", "p", "run")
    goals.reconcile(plan, observe(), 1)
    supplied = context()
    supplied["goal"] = plan["primary_goal"]
    plan = goals.apply_decision(plan, decision(plan, "expand", subgoals=[
        proposed("flour", "", "Obtain 3 flour", condition())]), supplied)
    before = copy.deepcopy(plan)
    response = decision(plan, "expand", completion=condition("inventory_quantity", "flour", 3), subgoals=[
        proposed("wheat", "", "Obtain enough wheat", condition("inventory_quantity", "wheat", 10), True)])
    with pytest.raises(ValueError, match="recipe's requirement"):
        goals.apply_decision(plan, response, supplied)
    assert plan == before
    response["subgoals"][0]["completion"]["quantity"] = 30
    accepted = goals.apply_decision(plan, response, supplied)
    assert goals.active_goal(accepted)["completion"]["quantity"] == 30
    assert accepted["goals"][1]["completion"]["quantity"] == 3


def test_oscillating_inventory_cannot_reset_no_progress_forever():
    plan = goals.new_plan("Make an apple pie", "p", "run")
    obs = observe()
    goals.reconcile(plan, obs, 1)
    plan = goals.apply_decision(plan, decision(plan, "expand", subgoals=[
        proposed("wheat", "", "Obtain 10 wheat", condition("inventory_quantity", "wheat", 10), True)]), context())
    reviews = 0
    for n in range(100):
        obs["state"]["player"]["inventory"] = [{"item_id": "wheat", "count": n % 2}]
        goals.record_action(plan)
        goals.reconcile(plan, obs, 1)
        if goals.needs_planning(plan):
            reviews += 1
            plan = goals.apply_decision(plan, decision(plan), context(obs))
        if plan["status"] == "blocked":
            break
    assert reviews > 0 and plan["status"] == "blocked"
    assert goals.active_goal(plan)["progress_high_water"] == 1


class PlannerCoordinator(CoordinatorDouble):
    def __init__(self, store):
        super().__init__(store)
        self.snapshot = observe()
        self.planning_contexts = []
        self.action_contexts = []
        self.plan_response = None

    async def request(self, worker, method, path, body):
        self.ops.append(body)
        return copy.deepcopy({**self.snapshot, "timestamp": time.time()}) if body["operation"] == "observe" else self.result

    async def plan_subgoals(self, context, run_id):
        self.planning_contexts.append(copy.deepcopy(context))
        if self.plan_response:
            return await self.plan_response(context)
        ref = [{"source": "observation", "path": "/ready", "quote": "true"}]
        is_root = context["current_goal"]["id"] == "root"
        children = [{"key": "discover", "parent_key": "", "objective": "Discover an observable recipe",
                     "completion": goals.unknown(), "evidence": ref, "actionable": True}] if is_root else []
        return {"goal_id": context["current_goal"]["id"], "mode": "expand" if is_root else "act",
                "completion": context["current_goal"]["completion"], "evidence": ref,
                "reason": "Use the advertised ordinary controls.", "subgoals": children}

    async def player_turn(self, context, dispatch, current, run_id):
        self.action_contexts.append(copy.deepcopy(context))
        await dispatch("perform_action", {"verb": "interact", "target_id": "oven", "target_version": 1, "parameters": {}, "reason": "fixture action"})


def runtime(tmp_path, plan=None):
    store = Store(tmp_path)
    seed_run(store)
    run = store.require("runs", "run")
    run["plan"]["players"][0]["goal"] = "Make an apple pie"
    run["agent_profile"] = {"instructions": context()["game_controls"]}
    run["build"] = {"source_root": str(tmp_path)}
    run["subgoal_plan"] = plan or hierarchy()
    store.put("runs", run)
    coordinator = PlannerCoordinator(store)
    coordinator.structured = StructuredPlayers(coordinator)
    return store, coordinator, coordinator.structured


@pytest.mark.asyncio
async def test_runtime_uses_active_leaf_and_progress_survives_store_reopen(tmp_path):
    store, coordinator, service = runtime(tmp_path)
    await service.turn("run", "p")
    assert coordinator.action_contexts[-1]["current_goal"]["objective"] == "Till 10 tiles"
    assert len(coordinator.action_contexts[-1]["goal_path"]) == 5
    assert coordinator.planning_contexts == []
    saved = store.require("runs", "run")["subgoal_plan"]
    assert goals.active_goal(saved)["attempts"] == 1
    store.close()
    reopened = Store(tmp_path)
    assert reopened.require("runs", "run")["subgoal_plan"] == saved
    reopened.close()


@pytest.mark.asyncio
async def test_planning_reobserves_before_any_game_action_and_ignores_cancelled_results(tmp_path):
    store, coordinator, service = runtime(tmp_path, goals.new_plan("Make an apple pie", "p", "run"))
    await service.turn("run", "p")
    assert len(coordinator.planning_contexts) == 1 and not coordinator.action_contexts
    assert sum(op["operation"] == "observe" for op in coordinator.ops) == 2
    await service.turn("run", "p")
    assert len(coordinator.action_contexts) == 1
    service.request_replan("run", "p", 1, "Test cancellation")
    entered, release = asyncio.Event(), asyncio.Event()
    async def slow(context):
        entered.set()
        await release.wait()
        return {"goal_id": "root", "mode": "blocked", "completion": goals.unknown(), "evidence": evidence(), "reason": "late response", "subgoals": []}
    coordinator.plan_response = slow
    task = asyncio.create_task(service.turn("run", "p"))
    await entered.wait()
    live = store.require("runs", "run")
    before = copy.deepcopy(live["subgoal_plan"])
    live["state"] = "Paused"
    live["players"]["p"]["generation"] += 1
    store.put("runs", live)
    release.set()
    await task
    assert store.require("runs", "run")["subgoal_plan"] == before
    assert len(coordinator.action_contexts) == 1
    store.close()


@pytest.mark.asyncio
async def test_no_action_candidate_returns_to_planner_and_bad_plan_never_executes(tmp_path):
    store, coordinator, service = runtime(tmp_path)
    coordinator.player_turn = AsyncMock(side_effect=ProviderError("decision_needs_instructions", "Need a prerequisite"))
    await service.turn("run", "p")
    assert goals.needs_planning(store.require("runs", "run")["subgoal_plan"])
    assert not hasattr(coordinator, "investigation")
    async def invalid(context):
        response = decision(store.require("runs", "run")["subgoal_plan"])
        response["evidence"][0]["quote"] = "invented recipe"
        return response
    coordinator.plan_response = invalid
    await service.turn("run", "p")
    assert coordinator.investigation[1] == "provider_failure"
    assert not any(op["operation"] == "perform_action" for op in coordinator.ops)
    store.close()


@pytest.mark.asyncio
async def test_new_instances_are_isolated_and_monitor_cannot_erase_plan(environment, profile):
    store, coordinator, _, _ = environment
    first = await Instances(coordinator).start(InstanceRequest(agent_id=profile["id"], primary_goal="Make pie", request_id="pie"))
    await coordinator.finish(first["id"], "user_stopped")
    first = store.require("runs", first["id"])
    second = await Instances(coordinator).start(InstanceRequest(agent_id=profile["id"], primary_goal="Harvest wheat", request_id="wheat"))
    assert first["subgoal_plan"]["primary_goal"] == "Make pie"
    assert second["subgoal_plan"]["primary_goal"] == "Harvest wheat"
    stale = copy.deepcopy(first)
    first["subgoal_plan"]["revision"] = 4
    first["subgoal_plan"]["reason"] = "New player-task update"
    store.put("runs", first)
    coordinator.ticks[first["id"]] = asyncio.current_task()
    try:
        coordinator.save_run(stale)
    finally:
        coordinator.ticks.pop(first["id"])
    assert store.require("runs", first["id"])["subgoal_plan"]["revision"] == 4
    assert store.require("runs", second["id"])["subgoal_plan"]["revision"] == 0
    assert "subgoal_plan" not in first["agent_profile"] and first["plan"]["success_criteria"]["predicate"] == "manual"


@pytest.mark.asyncio
@pytest.mark.parametrize("pin_fails", [False, True])
async def test_manual_flag_merges_evidence_without_rolling_back_live_planner(environment, profile, pin_fails):
    store, coordinator, _, _ = environment
    run = await Instances(coordinator).start(InstanceRequest(agent_id=profile["id"], primary_goal="Make pie", request_id="flag-plan"))
    await coordinator.admit(run)
    async def pin(worker_id, method, path, body):
        assert path == "/pin"
        current = store.require("runs", run["id"])
        current["subgoal_plan"].update(revision=7, reason="Fresh planner progress during pin I/O")
        current["players"]["player-1"]["agent_status"] = {"phase": "planning"}
        store.put("runs", current)
        if pin_fails:
            raise OSError("pin failed")
        return {"pinned": True}
    coordinator.request = pin
    await coordinator.flag(run["id"], "Review gameplay")
    latest = store.require("runs", run["id"])
    assert latest["subgoal_plan"]["revision"] == 7
    assert latest["players"]["player-1"]["agent_status"]["phase"] == "planning"
    assert bool(latest["evidence"]) is not pin_fails
    assert bool(latest["limitations"]) is pin_fails
