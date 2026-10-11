"""Fresh-instance contracts. All desktops, game controls and provider calls are doubles."""
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from ashad.game_controls import CONFIGURATION_DELETED, MODEL_NOT_ALLOWED, GameControls
from ashad.instances import InstanceRequest, Instances, default_model
from ashad.models import MODEL
from test_controller import FakeClaudeBackend, environment  # noqa: F401
from test_ui_http import app_at


@pytest.fixture
def profile(monkeypatch, environment):
    record = {"id": "agent-test", "name": "Agronauts", "build_id": "build-1", "source_revision": "test-revision",
              "status": "ready", "controls_version": 1}
    record["project_id"] = "project-1"
    record["artifact"] = artifact = {"controls": "Use next_text to advance cutscenes; check the UI before moving."}
    environment[0].put("agents", {k: v for k, v in record.items() if k != "artifact"})
    monkeypatch.setattr(GameControls, "validated", lambda self, agent_id: (
        {k: v for k, v in record.items() if k != "artifact"}, copy.deepcopy(record["artifact"])))
    return record


@pytest.mark.asyncio
async def test_fresh_instance_freezes_profile_and_deduplicates_start(environment, profile):
    store, coordinator, _, desktops = environment
    instances = Instances(coordinator)
    request = InstanceRequest(agent_id=profile["id"], primary_goal="  Learn to fish  ", request_id="start-one")
    run = await instances.start(request)
    assert run["agent_id"] == profile["id"]
    assert run["game_controls"]["controls"] == profile["artifact"]["controls"]
    assert run["agent_profile"]["id"] == profile["id"] and "model" not in run["agent_profile"]
    plan = run["plan"]
    assert plan["primary_goal"] == plan["players"][0]["goal"] == "Learn to fish"
    assert len(plan["players"]) == 1 and plan["players"][0]["role"] == "host"
    assert plan["world_setup"]["mode"] == plan["world_setup"]["character_policy"] == "fresh"
    assert plan["events"] == [] and plan["balancing_targets"] == []
    assert plan["success_criteria"]["predicate"] == "manual"
    assert (await instances.start(request))["id"] == run["id"]
    assert len(store.list("runs")) == 1
    assert all(path in {"/prepare/probe", "/status"} for desktop in desktops.values() for path, _ in desktop.calls)
    profile["artifact"]["controls"] = "new controls"
    assert store.require("runs", run["id"])["game_controls"]["controls"] != profile["artifact"]["controls"]
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
async def test_instance_model_defaults_to_connected_provider_and_accepts_any_supported_model(environment, profile):
    store, coordinator, _, _ = environment
    instances = Instances(coordinator)
    coordinator.ai.status = {"status": "ready"}
    run = await instances.start(InstanceRequest(agent_id=profile["id"], primary_goal="Explore", request_id="model-default"))
    assert run["model"] == MODEL
    assert "model" not in store.command("instance", "model-default")["request"]
    await coordinator.finish(run["id"], "user_stopped")
    coordinator.ai.claude_backend = FakeClaudeBackend()
    second = await instances.start(InstanceRequest(agent_id=profile["id"], primary_goal="Explore", request_id="model-override", model="sonnet"))
    assert second["model"] == second["plan"]["model"] == "sonnet"
    assert second["api"] == "claude" and "model" not in second["agent_profile"]


def test_default_model_falls_back_to_claude_when_only_claude_is_ready():
    ai = SimpleNamespace(status={"status": "login_required"}, claude_status={"status": "ready"})
    assert default_model(ai) == "sonnet"
    ai.status = {"status": "ready"}
    assert default_model(ai) == MODEL
    ai = SimpleNamespace(status={"status": "login_required"}, claude_status={"status": "configuration_required"})
    assert default_model(ai) == "gpt-6-luna"


@pytest.mark.asyncio
async def test_instance_rejects_unknown_model_without_side_effects(environment, profile):
    store, coordinator, _, _ = environment
    with pytest.raises(ValueError, match="Select a supported model"):
        await Instances(coordinator).start(InstanceRequest(agent_id=profile["id"], primary_goal="Explore", request_id="bad-model", model="nope"))
    assert store.list("scenarios") == []
    assert store.command("instance", "bad-model") is None


@pytest.mark.asyncio
async def test_launch_no_longer_requires_matching_provider(environment, profile):
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
    launched = await coordinator.launch(scenario["id"], scenario["revision"], "launch-claude")
    assert launched["model"] == "sonnet" and launched["api"] == "claude"


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


@pytest.mark.asyncio
async def test_blocked_instance_cannot_start_until_the_project_is_reconfigured(environment, profile, monkeypatch):
    store, coordinator, _, _ = environment
    artifact = profile["artifact"]

    def validated(self, agent_id):
        record = self.store.get("agents", agent_id)
        if record is None:
            raise ValueError(CONFIGURATION_DELETED)
        return record, copy.deepcopy(artifact)

    monkeypatch.setattr(GameControls, "validated", validated)
    instances = Instances(coordinator)
    first = await instances.start(InstanceRequest(agent_id="agent-test", primary_goal="Explore", request_id="r1"))
    await coordinator.finish(first["id"], "user_stopped")
    coordinator.finishing.pop(first["id"], None)  # finish() ran inline in this task
    before = copy.deepcopy(store.require("runs", first["id"]))
    await coordinator.delete_agent("agent-test")
    scenarios = len(store.list("scenarios"))

    with pytest.raises(ValueError, match="project configuration was deleted") as refused:
        await instances.start(InstanceRequest(agent_id="agent-test", primary_goal="Explore", request_id="fresh"))
    assert str(refused.value) == CONFIGURATION_DELETED
    assert len(store.list("scenarios")) == scenarios and store.command("instance", "fresh") is None

    # A replayed start: the instance command exists but no launch was ever committed.
    scenario = coordinator.save_scenario(copy.deepcopy(first["plan"]))
    replay = InstanceRequest(agent_id="agent-test", primary_goal="Explore", request_id="replayed")
    store.command("instance", "replayed", {"request": replay.model_dump(exclude_none=True),
                                          "scenario_id": scenario["id"], "revision": scenario["revision"]})
    with pytest.raises(ValueError) as replayed:
        await instances.start(replay)
    assert str(replayed.value) == CONFIGURATION_DELETED
    assert store.command("launch", "instance_replayed") is None

    store.put("agents", {**{k: v for k, v in profile.items() if k != "artifact"}, "id": "a2"})
    second = await instances.start(InstanceRequest(agent_id="a2", primary_goal=before["plan"]["primary_goal"], request_id="again"))
    assert second["agent_id"] == "a2" and second["id"] != first["id"]
    assert store.require("runs", first["id"]) == before


def test_public_workflow_and_retired_routes(tmp_path, monkeypatch):
    app = app_at(tmp_path)
    headers = {"X-ASHAD-Request": "1"}
    gather = AsyncMock(return_value={"id": "controls-1"})
    monkeypatch.setattr(GameControls, "gather", gather)
    with TestClient(app) as client:
        bootstrap = client.get("/api/bootstrap").json()
        assert bootstrap["agents"] == []
        assert "usage_count" in bootstrap and "usage" not in bootstrap and "leases" in bootstrap
        assert "scenarios" not in bootstrap and "fixtures" not in bootstrap
        assert client.post("/api/agents/controls", json={"build_id": "build-1"}).status_code == 403
        refused = client.post("/api/agents/controls", json={"build_id": "build-1", "model": "gpt-6-sol"}, headers=headers)
        assert refused.status_code == 422 and MODEL_NOT_ALLOWED in refused.text
        gather.assert_not_awaited()
        result = client.post("/api/agents/controls", json={"build_id": "build-1"}, headers=headers)
        assert result.status_code == 200
        gather.assert_awaited_once_with("build-1", project_id=None, name=None, request_id=None)
        assert client.post("/api/instances", json={"primary_goal": "Explore"}, headers=headers).status_code == 422
        for path in ("/api/plan", "/api/scenarios", "/api/validate", "/api/launch", "/api/fixtures",
                     "/api/runs/old/rerun", "/api/runs/old/review", "/api/findings/old/review"):
            assert client.post(path, json={}, headers=headers).status_code in {404, 405}
        assert client.get("/api/compare?run_ids=old").status_code == 404


@pytest.mark.parametrize("old", ["tune", "tuning/x"])
def test_retired_tuning_routes_are_removed_not_aliased(tmp_path, old):
    with TestClient(app_at(tmp_path)) as client:
        headers = {"X-ASHAD-Request": "1"}
        assert client.post(f"/api/agents/{old}", json={"build_id": "build-1"}, headers=headers).status_code in {404, 405}
        assert client.get(f"/api/agents/{old}").status_code in {404, 405}


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
    assert context["game_controls"] == profile["artifact"]["controls"]
    assert context["goal"] == "Explore" and context["agent_profile_id"] == profile["id"]
    assert "demo_wrapup_moar" not in context["guidance"]["completion"]


@pytest.mark.asyncio
async def test_legacy_run_with_only_agent_profile_instructions_still_reaches_context(environment, profile):
    store, coordinator, _, _ = environment
    run = await Instances(coordinator).start(InstanceRequest(agent_id=profile["id"], primary_goal="Explore", request_id="legacy-one"))
    run.pop("game_controls")
    run["agent_profile"]["instructions"] = "legacy instructions"
    player = run["plan"]["players"][0]
    context = coordinator.structured.decision_context(run, player, {"state": {}, "entities": []})
    assert context["game_controls"] == "legacy instructions"
