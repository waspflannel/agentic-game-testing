"""Fresh-instance contracts. All desktops, profiles and provider calls are doubles."""
import copy
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from ashad.agent_profiles import AgentProfiles
from ashad.instances import InstanceRequest, Instances
from ashad.models import MODEL
from test_controller import FakeClaudeBackend, environment  # noqa: F401
from test_ui_http import app_at


@pytest.fixture
def profile(monkeypatch, environment):
    record = {"id": "agent-test", "name": "Agronauts", "build_id": "build-1", "source_revision": "test-revision",
              "model": MODEL, "instructions": "Use next_text to advance cutscenes; check the UI before moving.", "status": "ready"}
    record["project_id"] = "project-1"
    environment[0].put("agents", record)
    monkeypatch.setattr(AgentProfiles, "validated", lambda self, agent_id: copy.deepcopy(record))
    return record


@pytest.mark.asyncio
async def test_fresh_instance_freezes_profile_and_deduplicates_start(environment, profile):
    store, coordinator, _, desktops = environment
    instances = Instances(coordinator)
    request = InstanceRequest(agent_id=profile["id"], primary_goal="  Learn to fish  ", request_id="start-one")
    run = await instances.start(request)
    assert run["agent_id"] == profile["id"]
    assert run["agent_profile"]["instructions"] == profile["instructions"]
    plan = run["plan"]
    assert plan["primary_goal"] == plan["players"][0]["goal"] == "Learn to fish"
    assert len(plan["players"]) == 1 and plan["players"][0]["role"] == "host"
    assert plan["world_setup"]["mode"] == plan["world_setup"]["character_policy"] == "fresh"
    assert plan["events"] == [] and plan["balancing_targets"] == []
    assert plan["success_criteria"]["predicate"] == "manual"
    assert (await instances.start(request))["id"] == run["id"]
    assert len(store.list("runs")) == 1
    assert all(path in {"/prepare/probe", "/status"} for desktop in desktops.values() for path, _ in desktop.calls)
    profile["instructions"] = "new tuning"
    assert store.require("runs", run["id"])["agent_profile"]["instructions"] != profile["instructions"]
    with pytest.raises(ValueError, match="different instance"):
        await instances.start(request.model_copy(update={"primary_goal": "Another objective"}))
    with pytest.raises(ValueError, match="different instance"):
        await instances.start(request.model_copy(update={"model": "gpt-6-sol"}))
    with pytest.raises(ValueError, match="One active instance"):
        await instances.start(request.model_copy(update={"request_id": "start-two"}))
    assert len(store.list("runs")) == 1
    await coordinator.finish(run["id"], "user_stopped")
    second = await instances.start(request.model_copy(update={"request_id": "start-two"}))
    assert second["id"] != run["id"] and second["project_id"] == "project-1"
    assert second["plan"]["world_setup"]["mode"] == "fresh"


@pytest.mark.asyncio
async def test_instance_model_defaults_to_profile_and_accepts_same_provider_override(environment, profile):
    store, coordinator, _, _ = environment
    instances = Instances(coordinator)
    run = await instances.start(InstanceRequest(agent_id=profile["id"], primary_goal="Explore", request_id="model-default"))
    assert run["model"] == MODEL
    assert "model" not in store.command("instance", "model-default")["request"]
    await coordinator.finish(run["id"], "user_stopped")
    second = await instances.start(InstanceRequest(agent_id=profile["id"], primary_goal="Explore", request_id="model-override", model="gpt-6-sol"))
    assert second["model"] == second["plan"]["model"] == "gpt-6-sol"
    assert second["agent_profile"]["model"] == MODEL
    assert second["api"] == "codex"


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["sonnet", "nope"])
async def test_instance_rejects_other_provider_or_unknown_model_without_side_effects(environment, profile, model):
    store, coordinator, _, _ = environment
    with pytest.raises(ValueError, match="Choose a Codex model"):
        await Instances(coordinator).start(InstanceRequest(agent_id=profile["id"], primary_goal="Explore", request_id="bad-model", model=model))
    assert store.list("scenarios") == []
    assert store.command("instance", "bad-model") is None


@pytest.mark.asyncio
async def test_launch_requires_profile_provider_not_model(environment, profile):
    store, coordinator, _, _ = environment
    run = await Instances(coordinator).start(InstanceRequest(agent_id=profile["id"], primary_goal="Explore", request_id="launch-base"))
    await coordinator.finish(run["id"], "user_stopped")
    plan = copy.deepcopy(run["plan"])
    plan["model"] = "gpt-6-sol"
    scenario = coordinator.save_scenario(plan)
    launched = await coordinator.launch(scenario["id"], scenario["revision"], "launch-codex")
    assert launched["model"] == "gpt-6-sol"
    await coordinator.finish(launched["id"], "user_stopped")
    coordinator.ai.claude_backend = FakeClaudeBackend()
    plan["model"] = "sonnet"
    scenario = coordinator.save_scenario(plan)
    with pytest.raises(ValueError, match="provider must match"):
        await coordinator.launch(scenario["id"], scenario["revision"], "launch-claude")


@pytest.mark.asyncio
async def test_steam_requires_a_ready_account_and_never_falls_back(environment, profile):
    store, coordinator, _, _ = environment
    instances = Instances(coordinator)
    request = InstanceRequest(agent_id=profile["id"], primary_goal="Explore", transport="steam", request_id="steam-one")
    with pytest.raises(ValueError, match="verified, licensed Steam"):
        await instances.start(request)
    assert store.list("runs") == []
    store.put("accounts", {"id": "steam-account", "worker_id": "worker-2", "steam_id": "76561198000000001",
                           "app_id": 3753020, "readiness": "ready"})
    with pytest.raises(ValueError, match="Steam readiness is unknown.*No ENet fallback"):
        await instances.start(request)
    assert not store.list("runs") and not store.leases()
    assert not store.command("instance", request.request_id)


@pytest.mark.asyncio
async def test_allocation_skips_known_wrong_build_and_unconfigured_enet(environment, profile):
    store, coordinator, _, desktops = environment
    first = store.list("workers")[0]
    desktops[first["id"]].probe["build"]["artifact_sha256"] = "0" * 64
    second = store.list("workers")[1]
    second.pop("host_address")
    store.put("workers", second)
    run = await Instances(coordinator).start(InstanceRequest(agent_id=profile["id"], primary_goal="Explore", request_id="allocate"))
    assert run["plan"]["players"][0]["worker_id"] not in {first["id"], second["id"]}


@pytest.mark.asyncio
async def test_rejected_start_can_be_corrected_and_reallocated(environment, profile):
    store, coordinator, _, _ = environment
    instances = Instances(coordinator)
    request = InstanceRequest(agent_id=profile["id"], primary_goal="Explore", request_id="retry")
    coordinator.ai.backend.ready = False
    with pytest.raises(ValueError, match="ChatGPT"):
        await instances.start(request)
    assert store.command("instance", "retry") is None
    assert store.list("runs") == []
    first = store.list("workers")[0]
    first["enabled"] = False
    store.put("workers", first)
    coordinator.ai.backend.ready = True
    run = await instances.start(request.model_copy(update={"primary_goal": "Learn to fish"}))
    assert run["plan"]["primary_goal"] == "Learn to fish"
    assert run["plan"]["players"][0]["worker_id"] != first["id"]


def test_public_workflow_and_retired_routes(tmp_path, monkeypatch):
    app = app_at(tmp_path)
    headers = {"X-ASHAD-Request": "1"}
    tune = AsyncMock(return_value={"id": "tuned", "model": "gpt-6-sol"})
    monkeypatch.setattr(AgentProfiles, "tune", tune)
    with TestClient(app) as client:
        bootstrap = client.get("/api/bootstrap").json()
        assert bootstrap["agents"] == []
        assert "usage_count" in bootstrap and "usage" not in bootstrap and "leases" in bootstrap
        assert "scenarios" not in bootstrap and "fixtures" not in bootstrap
        assert client.post("/api/agents/tune", json={"build_id": "build-1"}).status_code == 403
        result = client.post("/api/agents/tune", json={"build_id": "build-1", "model": "gpt-6-sol"}, headers=headers)
        assert result.status_code == 200
        tune.assert_awaited_once_with("build-1", "gpt-6-sol", project_id=None, name=None, request_id=None)
        assert client.post("/api/instances", json={"primary_goal": "Explore"}, headers=headers).status_code == 422
        for path in ("/api/plan", "/api/scenarios", "/api/validate", "/api/launch", "/api/fixtures",
                     "/api/runs/old/rerun", "/api/runs/old/review", "/api/findings/old/review"):
            assert client.post(path, json={}, headers=headers).status_code in {404, 405}
        assert client.get("/api/compare?run_ids=old").status_code == 404


@pytest.mark.asyncio
async def test_frozen_controls_are_in_every_player_context(environment, profile):
    import time
    store, coordinator, _, _ = environment
    run = await Instances(coordinator).start(InstanceRequest(agent_id=profile["id"], primary_goal="Explore", request_id="context-one"))
    run["state"] = "Running"
    run["players"]["player-1"].update(bound=True, launched=True)
    store.put("runs", run)
    observation = {"schema_version": 1, "run_id": run["id"], "player_id": "player-1", "observation_id": "obs",
                   "timestamp": time.time(), "state": {}, "entities": []}
    coordinator.structured.call_game = AsyncMock(return_value=observation)
    coordinator.ai.player_turn = AsyncMock(return_value={"status": "waiting"})
    from ashad.subgoals import unknown
    reference = [{"source": "observation", "path": "/state", "quote": "{}"}]
    coordinator.ai.plan_subgoals = AsyncMock(return_value={"goal_id": "root", "mode": "expand", "completion": unknown(),
        "evidence": reference, "reason": "Discover the current world with advertised controls.",
        "subgoals": [{"key": "discover", "parent_key": "", "objective": "Inspect the visible surroundings",
                      "completion": unknown(), "evidence": reference, "actionable": True}]})
    await coordinator.structured.turn(run["id"], "player-1")
    await coordinator.structured.turn(run["id"], "player-1")
    context = coordinator.ai.player_turn.call_args.args[0]
    assert context["game_controls"] == profile["instructions"]
    assert context["goal"] == "Explore" and context["agent_profile_id"] == profile["id"]
    assert "demo_wrapup_moar" not in context["guidance"]["completion"]
