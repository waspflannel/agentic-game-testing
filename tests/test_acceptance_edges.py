"""Acceptance edge regressions using temporary records and explicit desktop/provider doubles.

These tests never represent a completed real gameplay session and never write to
the user's controller store, game repository, or configured provider account.
"""
import asyncio
import copy
import hashlib
import json
import time
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import pytest

from ashad.ai import OpenAI
from ashad.app import create_app
from ashad.repository import Repository, sha256_file
from ashad.validation import validate_plan
from test_controller import environment, FakeCodexBackend, MemoryCredentials, new_run  # noqa: F401


def add_players(plan, count):
    host = copy.deepcopy(plan["players"][0])
    plan["players"] = [
        {**host, "id": f"player-{i}", "name": f"Player {i}",
         "role": "host" if i == 1 else "joiner", "worker_id": f"worker-{i}"}
        for i in range(1, count + 1)
    ]


async def ready_run(env, plan=None, count=1, completed=None):
    store, coord, original, _ = env
    plan = copy.deepcopy(plan or original)
    add_players(plan, count)
    plan["success_criteria"]["corroboration_seconds"] = 0
    run = await new_run(env, plan)
    await coord.admit(run)
    run = store.require("runs", run["id"])
    now = time.time()
    run.update(state="Running", validity="valid", world_ready_at=now)
    for p in run["plan"]["players"]:
        ps = run["players"][p["id"]]
        ps.update(bound=True, launched=True, phase="ready", launched_at=now,
                  ready_at=now, telemetry_received_at=now,
                  last_observation={"timestamp": now, "observation_id": "observed-" + p["id"]},
                  telemetry={"sequence": 1, "simulation_tick": 100, "world_id": "world-alpha",
                             "world_loaded": True, "world_synced": True, "connected": True,
                             "is_authority": p["role"] == "host", "transport": "enet",
                             "world_simulating": True, "completed_quests": list(completed or [])})
    store.put("runs", run)
    return run


@pytest.mark.asyncio
async def test_different_world_ids_cannot_corroborate_full_demo(environment):
    store, coord, _, _ = environment
    run = await ready_run(environment, count=2, completed=["demo_wrapup_moar"])
    run["players"]["player-2"]["telemetry"]["world_id"] = "world-beta"
    store.put("runs", run)
    await coord.monitor(run)
    actual = store.require("runs", run["id"])
    assert actual["goal_complete"] is False, "Equal quest lists in unrelated worlds are not session agreement"
    assert actual["demo_complete"] is False
    assert actual.get("ending_reason") != "completed"


@pytest.mark.asyncio
async def test_unmet_required_side_goal_blocks_then_allows_completion(environment):
    store, coord, original, _ = environment
    plan = copy.deepcopy(original)
    plan["success_criteria"]["side_goals"] = ["first_quota"]
    run = await ready_run(environment, plan, count=2, completed=["demo_wrapup_moar"])
    await coord.monitor(run)
    actual = store.require("runs", run["id"])
    assert actual["goal_complete"] is False, "A required side goal cannot be dropped from the success condition"
    assert actual["state"] == "Running"
    for ps in actual["players"].values():
        ps["telemetry"]["completed_quests"].append("first_quota")
        ps["last_observation"]["timestamp"] = time.time()
    store.put("runs", actual)
    await coord.monitor(actual)
    completed = store.require("runs", run["id"])
    assert completed["goal_complete"] is True and completed["demo_complete"] is True
    assert completed["ending_reason"] == "completed"


@pytest.mark.parametrize("simulation_advances", [False, True])
@pytest.mark.asyncio
async def test_game_tick_freshness_is_distinct_from_telemetry_packet_freshness(environment, monkeypatch, simulation_advances):
    store, coord, _, desktops = environment
    run = await ready_run(environment)
    clock = [time.time()]
    monkeypatch.setattr("ashad.coordinator.time.time", lambda: clock[0])
    monkeypatch.setattr(coord, "player_turn", AsyncMock())
    sequence = [0]
    original_request = desktops["worker-1"].request

    async def responding_worker(method, path, *, data=None, **kwargs):
        if path == "/status":
            return {**await original_request(method, path, data=data, **kwargs), "game_running": True, "capture": {"healthy": True, "last_at": clock[0]}}
        if path == "/telemetry":
            sequence[0] += 1
            return {**run["players"]["player-1"]["telemetry"], "sequence": sequence[0] + 10,
                    "simulation_tick": 100 + sequence[0] if simulation_advances else 100,
                    "captured_at": clock[0]}
        return await original_request(method, path, data=data, **kwargs)

    monkeypatch.setattr(desktops["worker-1"], "request", responding_worker)
    try:
        await coord.tick(store.require("runs", run["id"]))
        await asyncio.sleep(0)
        clock[0] += run["plan"]["stall_policy"]["simulation_timeout_seconds"] + 1
        await coord.tick(store.require("runs", run["id"]))
        await asyncio.sleep(0)
        actual = store.require("runs", run["id"])
        assert actual["players"]["player-1"]["telemetry_received_at"] == clock[0]
        if simulation_advances:
            assert actual["state"] == "Running", "Slow quest progress with advancing simulation is not a freeze"
            assert store.list("incidents") == []
        else:
            assert actual["state"] == "Investigating", "Fresh diagnostic packets must not conceal a stalled game simulation"
            assert actual["investigation_deadline"] > clock[0]
            assert any(i["category"] == "suspected_game_freeze" for i in store.list("incidents"))
    finally:
        for task in coord.tasks.values():
            task.cancel()
        await asyncio.gather(*coord.tasks.values(), return_exceptions=True)


@pytest.mark.parametrize("join_at,expected", [(60, "executing"), (15, "skipped")])
@pytest.mark.asyncio
async def test_pause_only_applies_skip_policy_to_events_that_became_overdue(environment, monkeypatch, join_at, expected):
    store, coord, original, desktops = environment
    plan = copy.deepcopy(original)
    add_players(plan, 2)
    plan["players"][1]["initial"] = False
    plan["events"] = [{"id": "late-join", "actor": "player-2", "action": "join", "trigger": "elapsed",
                       "value": join_at, "time_basis": "run", "overdue_policy": "skip"}]
    run = await new_run(environment, plan)
    await coord.admit(run)
    run = store.require("runs", run["id"])
    run["state"] = "Running"
    store.put("runs", run)
    base = run["started_at"]
    clock = [base + 10]
    monkeypatch.setattr("ashad.coordinator.time.time", lambda: clock[0])
    await coord.control(run["id"], "pause", "Controlled pause before trigger", "pause-once")
    clock[0] = base + 20
    await coord.control(run["id"], "resume", "Controlled resume", "resume-once")
    resumed = store.require("runs", run["id"])
    await coord.schedule(resumed)
    if join_at > 20:
        assert store.require("runs", run["id"])["events"]["late-join"]["status"] == "pending"
        assert not any(path in {"/bind", "/launch", "/action"} for path, _ in desktops["worker-2"].calls)
        clock[0] = base + join_at + 1
        await coord.schedule(store.require("runs", run["id"]))
    actual = store.require("runs", run["id"])
    assert actual["events"]["late-join"]["status"] == expected
    assert any(path == "/launch" for path, _ in desktops["worker-2"].calls) is (expected == "executing")


def save_package(store, plan, tmp_path, *, revision="test-revision", identities=None):
    build = store.require("builds", plan["build_id"])
    build.setdefault("save_version", 30)
    store.put("builds", build)
    root = tmp_path / "immutable-save-package"
    root.mkdir(exist_ok=True)
    identities = identities or {p["id"]: "local-" + p["id"] for p in plan["players"]}
    players = {}
    for p in plan["players"]:
        name = p["id"] + ".tar.gz"
        archive = root / name
        archive.write_bytes(b"Controlled fixture manifest validation only; no real save archive")
        players[p["id"]] = {"identity": identities[p["id"]], "archive": name, "sha256": sha256_file(archive)}
    fixture = {"id": "fixture-edge", "path": str(root), "build_id": plan["build_id"],
               "build_revision": revision, "save_version": 30, "transport": "enet", "world_id": "saved-world",
               "starting_milestones": [], "players": players}
    store.put("fixtures", fixture)
    plan["world_setup"].update(mode="saved", fixture_id=fixture["id"], character_policy="restore",
                               identity_mapping={p["id"]: p["id"] for p in plan["players"]})
    return fixture


def test_saved_package_same_build_id_does_not_hide_revision_mismatch(environment, tmp_path):
    store, _, original, _ = environment
    plan = copy.deepcopy(original)
    build = store.require("builds", plan["build_id"])
    build["save_version"] = 30
    store.put("builds", build)
    fixture = save_package(store, plan, tmp_path, revision=build["source_revision"])
    assert validate_plan(plan, store)["valid"], "The matching saved-start control should be admissible"
    fixture["build_revision"] = "different-source-even-though-build-id-is-reused"
    store.put("fixtures", fixture)
    result = validate_plan(plan, store)
    assert result["valid"] is False, "A reused catalog ID is insufficient proof of saved-build compatibility"
    assert any("revision" in e.lower() or "compatib" in e.lower() for e in result["errors"])


@pytest.mark.parametrize("operation", ["read", "search"])
def test_repository_retrieval_rejects_drift_after_snapshot_was_pinned(tmp_path, operation):
    source = tmp_path / "source"
    source.mkdir()
    target = source / "quest.gd"
    target.write_text("# original pinned quest\n", encoding="utf-8")
    hashes = {"quest.gd": sha256_file(target)}
    hash_manifest = tmp_path / "source-hashes.json"
    hash_manifest.write_text(json.dumps(hashes), encoding="utf-8")
    source_hash = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    build = {"source_root": str(source), "build_root": str(tmp_path), "source_revision": "pinned-fixture",
             "source_hash": source_hash, "content_sha256": source_hash,
             "artifacts": [{"path": str(hash_manifest), "sha256": sha256_file(hash_manifest)}]}
    repo = Repository(build)
    assert "original pinned quest" in repo.read("quest.gd")["text"]
    target.write_text("# substituted quest AFTER pinning\n", encoding="utf-8")
    with pytest.raises(ValueError, match="(?i)changed|drift|hash|pinned|integrity|revision"):
        repo.read("quest.gd") if operation == "read" else repo.search("substituted")


def test_four_player_plans_reject_duplicate_explicit_and_restored_identities(environment, tmp_path):
    store, _, original, _ = environment
    plan = copy.deepcopy(original)
    add_players(plan, 4)
    for i, p in enumerate(plan["players"], 1):
        p["identity"] = f"unique-player-{i}"
    assert validate_plan(plan, store)["valid"]
    plan["players"][3]["identity"] = plan["players"][0]["identity"]
    assert not validate_plan(plan, store)["valid"], "Four separate desktops cannot share one player identity"
    for p in plan["players"]:
        p["identity"] = None
    fixture = save_package(store, plan, tmp_path)
    assert validate_plan(plan, store)["valid"]
    fixture["players"]["player-4"]["identity"] = fixture["players"]["player-1"]["identity"]
    store.put("fixtures", fixture)
    assert not validate_plan(plan, store)["valid"], "Resolved character identity collisions must be caught before worker allocation"


def game_context_worker(monkeypatch, desktop, run):
    original = desktop.request
    observation = {"observation_id": "own-frame-1", "timestamp": time.time(), "sequence": 1,
                   "run_id": run["id"], "worker_id": "worker-1", "player_id": "player-1",
                   "schema_version": 1, "generation": "game-fixture", "revision": 1, "state": {"objectives": []}, "entities": []}

    async def request(method, path, *, data=None, **kwargs):
        if path == "/game" and data["operation"] == "observe":
            return observation
        if path == "/game" and data["operation"] == "perform_action":
            desktop.calls.append((path, data))
            return {"status": "completed"}
        return await original(method, path, data=data, **kwargs)

    monkeypatch.setattr(desktop, "request", request)
    monkeypatch.setattr(desktop, "screenshot", AsyncMock(side_effect=AssertionError("Gameplay must never request screenshots")), raising=False)
    return observation


@pytest.mark.asyncio
async def test_provider_unavailable_still_leaves_http_readable_deterministic_report(environment, monkeypatch):
    store, coord, _, desktops = environment
    run = await ready_run(environment)
    game_context_worker(monkeypatch, desktops["worker-1"], run)
    coord.ai = OpenAI(store, backend=FakeCodexBackend(ready=False))
    await coord.player_turn(run["id"], "player-1")
    interrupted = store.require("runs", run["id"])
    assert interrupted["state"] == "Investigating"
    assert not any(path == "/action" for path, _ in desktops["worker-1"].calls)
    await coord.finish(run["id"], "infrastructure_failure", terminate=True)
    app = create_app(store.root, MemoryCredentials(None), start_services=False)
    app.state.ai.backend = FakeCodexBackend(ready=False)
    app.state.ai.review = AsyncMock(side_effect=AssertionError("Base report must not call a provider"))
    with TestClient(app) as client:
        response = client.get(f"/api/runs/{run['id']}/report")
        assert response.status_code == 200
        report = response.json()["markdown"]
        assert "infrastructure_failure" in report and "Complete chronological action appendix" in report
        assert "Goal completion: **False**" in report
        app.state.ai.review.assert_not_called()


@pytest.mark.asyncio
async def test_codex_selects_bound_action_and_excludes_hidden_context(environment, monkeypatch):
    store, coord, _, desktops = environment
    run = await ready_run(environment, count=2)
    observation = game_context_worker(monkeypatch, desktops["worker-1"], run)
    observation["entities"] = [{"id": "play", "version": 4, "kind": "control", "label": "Play", "actions": ["ui_click"]}]
    run["players"]["player-1"]["telemetry"]["diagnostics"] = {"hidden_inventory": "MONITOR_ONLY_CANARY"}
    run["players"]["player-2"]["last_observation"]["private_text"] = "OTHER_DESKTOP_CANARY"
    store.put("runs", run)
    store.event(run["id"], "monitor_observation", {"player_id": "player-1", "hidden": "MONITOR_ONLY_CANARY"})
    store.event(run["id"], "activity", {"player_id": "player-2", "description": "OTHER_PLAYER_ACTIVITY_CANARY"})
    store.event(run["id"], "action_result", {"player_id": "player-1", "result": "OWN_ACTION_CANARY"})
    store.event(run["id"], "team_message", {"player_id": "player-2", "message": "DECLARED_TEAM_CANARY"})
    backend = FakeCodexBackend()
    coord.ai = OpenAI(store, backend=backend)
    await coord.player_turn(run["id"], "player-1")
    requests = backend.requests
    assert 1 <= len(requests) <= 2
    assert all(set(r) == {"input", "questions", "model"} and r["model"] == "gpt-6-luna" for r in requests)
    assert all(u["model"] == "gpt-6-luna" and u["billing"] == "subscription" for u in store.list("usage"))
    desktops["worker-1"].screenshot.assert_not_called()
    serialized = json.dumps(requests)
    assert "input_image" not in serialized
    for private in ("MONITOR_ONLY_CANARY", "OTHER_DESKTOP_CANARY", "OTHER_PLAYER_ACTIVITY_CANARY", "test-key"):
        assert private not in serialized
    assert "OWN_ACTION_CANARY" in serialized and "DECLARED_TEAM_CANARY" in serialized
    delivered = [payload for path, payload in desktops["worker-1"].calls if path == "/game" and payload["operation"] == "perform_action"]
    assert len(delivered) == 1
    assert delivered[0]["run_id"] == run["id"] and delivered[0]["player_id"] == "player-1"
    action = delivered[0]["arguments"]
    assert action["observation_id"] == observation["observation_id"]
    assert action["verb"] == "ui_click" and action["target_id"] == "play" and action["target_version"] == 4
    assert not any(path == "/action" for path, _ in desktops["worker-2"].calls)
    assert any(e["kind"] == "decision_selected" for e in store.events(run["id"], limit=1000))
