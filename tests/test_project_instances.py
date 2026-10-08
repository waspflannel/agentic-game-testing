"""Project/admission API contracts: temporary data and provider/worker doubles only."""
import asyncio
import copy
from unittest.mock import AsyncMock
from contextlib import closing
import threading
import httpx
from ashad.store import Store
from ashad.process_lock import acquire

import pytest
from fastapi.testclient import TestClient

from ashad.app import create_app
from ashad.coordinator import Coordinator, InstanceConflict
from ashad.instances import Instances, InstanceRequest
from ashad.agent_profiles import AgentProfiles
from test_controller import environment, new_run, MemoryCredentials, FakeCodexBackend
from test_instances import profile
from test_agent_profiles import setup


@pytest.mark.asyncio
async def test_competing_projects_share_atomic_slot_across_independent_callers(environment, profile, monkeypatch):
    store, first_coord, _, desktops = environment
    store.put("projects", {"id": "project-2", "name": "Another game"})
    second_build = {**store.require("builds", "build-1"), "id": "build-2", "project_id": "project-2"}
    store.put("builds", second_build)
    store.put("agents", {**profile, "id": "agent-two", "build_id": "build-2", "project_id": "project-2"})
    monkeypatch.setattr(AgentProfiles, "validated", lambda self, agent_id: self.store.require("agents", agent_id))
    second_coord = Coordinator(store, first_coord.credentials, first_coord.ai, first_coord.client_factory)
    # Make both callers reach a genuine suspended preflight before either commits.
    ready, count = asyncio.Event(), 0
    original = first_coord.ai.validate

    async def concurrent_status(*args):
        nonlocal count
        count += 1
        if count == 2:
            ready.set()
        await ready.wait()
        return await original(*args)

    monkeypatch.setattr(first_coord.ai, "validate", concurrent_status)
    requests = [InstanceRequest(agent_id=profile["id"], project_id="project-1", primary_goal="Explore A", request_id="race-a"),
                InstanceRequest(agent_id="agent-two", project_id="project-2", primary_goal="Explore B", request_id="race-b")]
    results = await asyncio.wait_for(asyncio.gather(
        Instances(first_coord).start(requests[0]), Instances(second_coord).start(requests[1]),
        return_exceptions=True), 5)
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, InstanceConflict) for result in results) == 1
    winner = next(result for result in results if isinstance(result, dict))
    assert store.list("runs") == [winner]
    assert store.leases() == [{"kind": "instance", "resource_id": "global", "run_id": winner["id"]}]
    assert all(path in {"/prepare/probe", "/status"} for desktop in desktops.values() for path, _ in desktop.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["Queued", "Starting", "Running", "Paused", "Investigating", "Stopping", "Ended"])
async def test_every_occupied_phase_blocks_before_worker_or_provider_preflight(environment, profile, state):
    store, coord, _, desktops = environment
    first = await Instances(coord).start(InstanceRequest(agent_id=profile["id"], primary_goal="First", request_id="first"))
    first["state"] = state
    store.put("runs", first)
    calls = {worker: list(desktop.calls) for worker, desktop in desktops.items()}
    coord.ai.validate = AsyncMock()
    with pytest.raises(InstanceConflict) as conflict:
        await Instances(coord).start(InstanceRequest(agent_id=profile["id"], primary_goal="Second", request_id="second"))
    assert conflict.value.owners[0]["run_id"] == first["id"]
    assert len(store.list("runs")) == 1
    coord.ai.validate.assert_not_awaited()
    assert calls == {worker: desktop.calls for worker, desktop in desktops.items()}


@pytest.mark.asyncio
async def test_exact_retry_can_read_frozen_ended_run_while_another_owns_slot(environment, profile):
    store, coord, _, _ = environment
    facade = Instances(coord)
    original = InstanceRequest(agent_id=profile["id"], primary_goal="Frozen first", request_id="first")
    first = await facade.start(original)
    await coord.finish(first["id"], "user_stopped")
    second = await facade.start(original.model_copy(update={"request_id": "second", "primary_goal": "New execution"}))
    before = copy.deepcopy(store.require("runs", first["id"]))
    assert await facade.start(original) == before
    assert len(store.list("runs")) == 2 and coord.occupied_instances()[0]["run_id"] == second["id"]
    with pytest.raises(ValueError, match="different instance"):
        await facade.start(original.model_copy(update={"project_id": "project-1"}))


@pytest.mark.asyncio
async def test_project_mismatch_or_unassigned_agent_fails_without_side_effects(environment, profile):
    store, coord, _, desktops = environment
    store.put("projects", {"id": "project-other", "name": "Same name allowed"})
    for project_id in ("project-other", "absent-project"):
        with pytest.raises(ValueError):
            await Instances(coord).start(InstanceRequest(agent_id=profile["id"], project_id=project_id,
                primary_goal="Wrong project", request_id="invalid"))
    build = store.require("builds", "build-1")
    build.pop("project_id")
    store.put("builds", build)
    with pytest.raises(ValueError, match="no project"):
        await Instances(coord).start(InstanceRequest(agent_id=profile["id"], primary_goal="Unassigned", request_id="invalid"))
    assert not store.list("runs") and not store.leases()
    assert not any(desktop.calls for desktop in desktops.values())


@pytest.mark.asyncio
async def test_unvalidated_current_account_cannot_launch_from_imported_ready_cache(environment, profile):
    store, coord, _, desktops = environment
    store.put("settings", {"id": "ai_status", "configured": True, "status": "ready"})
    coord.ai.validate = AsyncMock(return_value={"configured": False, "status": "configuration_required", "message": "Sign in in the new controller home"})
    with pytest.raises(ValueError, match="new controller home"):
        await Instances(coord).start(InstanceRequest(agent_id=profile["id"], primary_goal="Not allowed", request_id="no-auth"))
    assert not store.list("runs") and not store.leases()
    assert all(path in {"/prepare/probe", "/status"} for desktop in desktops.values() for path, _ in desktop.calls)


@pytest.mark.asyncio
async def test_launch_key_cannot_be_rebound_to_different_frozen_revision(environment):
    store, coord, plan, _ = environment
    first = coord.save_scenario(plan)
    await coord.launch(first["id"], 1, "frozen-request")
    second = coord.save_scenario({**plan, "title": "Changed"}, first["id"])
    with pytest.raises(ValueError, match="different frozen inputs"):
        await coord.launch(second["id"], 2, "frozen-request")
    assert len(store.list("runs")) == 1


@pytest.mark.asyncio
async def test_tuning_retry_returns_saved_profile_without_inference_even_if_build_is_missing(setup):
    store, build, backend, profiles = setup
    kwargs = {"request_id": "tune-one", "name": " Beach scout ", "project_id": build["project_id"]}
    first = await profiles.tune(build["id"], **kwargs)
    assert first["name"] == "Beach scout" and first["project_id"] == build["project_id"]
    store.delete("builds", build["id"])
    async with profiles.lock:  # Reading a completed retry is not another tuning operation.
        assert await profiles.tune(build["id"], **kwargs) == first
    assert backend.tune.await_count == 1 and len(store.list("agents")) == 1
    with pytest.raises(ValueError, match="different inputs"):
        await profiles.tune(build["id"], **{**kwargs, "name": "Changed"})
    store.put("builds", build)
    second = await profiles.tune(build["id"], **{**kwargs, "request_id": "explicit-retune"})
    assert second["id"] != first["id"] and backend.tune.await_count == 2


def offline_app(root, controller_lock=None):
    return create_app(root, credentials=MemoryCredentials(), start_services=False,
                      ai_backend=FakeCodexBackend(), controller_lock=controller_lock)


def test_project_api_duplicate_names_idempotency_and_fixed_saved_cap(tmp_path):
    app = offline_app(tmp_path)
    headers = {"X-ASHAD-Request": "1"}
    with TestClient(app) as client:
        first = client.post("/api/projects", json={"name": "Game", "request_id": "one"}, headers=headers).json()
        assert client.post("/api/projects", json={"name": "Game", "request_id": "one"}, headers=headers).json() == first
        second = client.post("/api/projects", json={"name": "Game", "request_id": "two"}, headers=headers).json()
        assert second["id"] != first["id"]
        assert client.post("/api/projects", json={"name": "Changed", "request_id": "one"}, headers=headers).status_code == 422
        assert client.get(f"/api/projects/{first['id']}").json()["runs"] == []
        settings = app.state.store.require("settings", "main")
        settings["concurrent_sessions"] = 12
        app.state.store.put("settings", settings)
        bootstrap = client.get("/api/bootstrap").json()
        assert len(bootstrap["projects"]) == 2 and bootstrap["instance_slot"]["available"] is True
        assert bootstrap["settings"]["concurrent_sessions"] == 1 and bootstrap["settings"]["saved_concurrent_sessions"] == 12
        for cap in (2, 12, True, "1"):
            assert client.post("/api/settings", json={"concurrent_sessions": cap}, headers=headers).status_code == 422
        assert app.state.store.require("settings", "main")["concurrent_sessions"] == 12
        assert not app.state.store.list("runs") and not app.state.store.leases()


def test_import_api_preserves_history_and_does_not_trust_old_auth_or_caller_drain(tmp_path):
    source = tmp_path / "legacy"
    with closing(Store(source)) as old:
        old.put("settings", {"id": "main", "concurrent_sessions": 9})
        old.put("settings", {"id": "ai_status", "configured": True, "status": "ready", "auth_method": "chatgpt"})
        old.put("runs", {"id": "old-run", "state": "Ended", "players": {}, "plan": {"primary_goal": "Frozen history"}})
        old.event("old-run", "ended", {"reason": "fixture"})
    target_lock = acquire(tmp_path / "desktop" / "controller.lock")
    app = offline_app(tmp_path / "desktop", controller_lock=target_lock)
    payload = {"source_root": str(source), "backup_dir": str(tmp_path / "backup"),
               "external_inventory": {"codex_home": str(source / "codex"), "worker_registration_paths": [],
                                      "external_build_paths": [], "docker_volumes": [], "keyring_services": ["ASHAD.Bot.Local"]},
               "source_idle_confirmed": True, "external_backup_confirmed": True, "target_drained": False}
    headers = {"X-ASHAD-Request": "1"}
    with target_lock, TestClient(app) as client:
        assert client.post("/api/workspace/import", json=payload, headers=headers).status_code == 422
        result = client.post("/api/workspace/import", json={**payload, "confirmed": True}, headers=headers)
        assert result.status_code == 200 and result.json()["readiness_verified"] is False
        data = client.get("/api/bootstrap").json()
        assert data["runs"][0]["plan"] == {"primary_goal": "Frozen history"}
        assert data["ai"]["configured"] is False and data["settings"]["saved_concurrent_sessions"] == 9
        assert app.state.store.require("settings", "ai_status")["configured"] is True  # Raw history untouched.
        assert client.post("/api/workspace/import", json={**payload, "confirmed": True}, headers=headers).json() == result.json()
        assert not app.state.coordinator.draining


@pytest.mark.asyncio
async def test_request_lookup_reports_real_inflight_tasks_before_durable_preparation(tmp_path, monkeypatch):
    releases = [asyncio.Event(), asyncio.Event()]
    started = [asyncio.Event(), asyncio.Event()]
    count = 0
    async def waiting(self, body):
        nonlocal count
        index = count
        count += 1
        started[index].set()
        await releases[index].wait()
        return {"id": "fixture-response-only"}
    monkeypatch.setattr(Instances, "start", waiting)
    app = offline_app(tmp_path)
    requests = []
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            payload = {"agent_id": "fixture", "primary_goal": "Fixture", "request_id": "same-inflight"}
            for index in range(2):
                requests.append(asyncio.create_task(client.post("/api/instances", json=payload, headers={"X-ASHAD-Request": "1"})))
                await asyncio.wait_for(started[index].wait(), 2)
            assert (await client.get("/api/instances/requests/same-inflight")).json()["status"] == "in_progress"
            releases[0].set()
            await requests[0]
            assert (await client.get("/api/instances/requests/same-inflight")).json()["status"] == "in_progress"
            releases[1].set()
            await requests[1]
            assert (await client.get("/api/instances/requests/same-inflight")).json()["status"] == "unknown"
            assert not app.state.instance_requests and not app.state.store.leases()
    finally:
        for release in releases:
            release.set()
        await asyncio.gather(*requests, return_exceptions=True)
        app.state.store.close()


def test_import_cannot_forge_target_process_ownership_through_request_body(tmp_path):
    app = offline_app(tmp_path)
    with TestClient(app) as client:
        result = client.post("/api/workspace/import", json={"source_root": str(tmp_path / "source"),
            "backup_dir": str(tmp_path / "backup"), "target_drained": True, "confirmed": True,
            "source_idle_confirmed": True, "external_backup_confirmed": True}, headers={"X-ASHAD-Request": "1"})
        assert result.status_code == 422 and "process lock" in result.json()["detail"]
        assert not (tmp_path / "backup").exists() and not app.state.store.list("migrations")


def test_request_reconciliation_apis_return_saved_outputs_without_work(tmp_path):
    app = offline_app(tmp_path)
    app.state.store.put("agents", {"id": "saved", "instructions": "Immutable manual"})
    app.state.store.command("tuning", "tune-saved", {"agent_id": "saved", "request": {"build_id": "missing", "model": "gpt-6-luna", "name": None, "project_id": None}})
    app.state.store.put("runs", {"id": "saved-run", "state": "Ended", "players": {}})
    app.state.store.command("launch", "instance_start-saved", {"run_id": "saved-run"})  # Instances.start's real key.
    app.state.store.command("instance", "start-legacy", {"run_id": "saved-run"})
    app.state.store.command("instance", "prepared", {"request": {"agent_id": "saved", "primary_goal": "Retained draft", "token": "fixture-private"}})
    with TestClient(app) as client:
        assert client.get("/api/agents/tuning/tune-saved").json()["agent"]["id"] == "saved"
        assert client.get("/api/instances/requests/start-saved").json()["run"]["id"] == "saved-run"
        assert client.get("/api/instances/requests/start-legacy").json()["run"]["id"] == "saved-run"
        prepared = client.get("/api/instances/requests/prepared").json()
        assert prepared["status"] == "prepared" and prepared["request"] == {"agent_id": "saved", "primary_goal": "Retained draft"}
        assert client.get("/api/agents/tuning/missing").json()["status"] == "unknown"
        app.state.tuning_task = object()
        app.state.tuning_request_id = "active-request"
        assert client.get("/api/agents/tuning/active-request").json()["status"] == "in_progress"
        # Progress: stage and batch from the running tune, model calls since it started from usage rows.
        app.state.profiles.progress = {"stage": "source_notes", "batch": 3, "batch_count": 8, "started_at": 1000.0}
        for i, status in enumerate(["completed", "invalid_tuning", "completed"]):
            app.state.store.put("usage", {"id": f"u{i}", "purpose": "tuning", "status": status, "timestamp": 1001.0 + i})
        app.state.store.put("usage", {"id": "old", "purpose": "tuning", "status": "completed", "timestamp": 999.0})
        app.state.store.put("usage", {"id": "play", "purpose": "player", "status": "completed", "timestamp": 1005.0})
        progress = client.get("/api/agents/tuning/active-request").json()["progress"]
        assert progress == {"stage": "source_notes", "batch": 3, "batch_count": 8, "started_at": 1000.0,
                            "model_calls": 3, "rejected_replies": 1, "last_call_at": 1003.0}
        assert "progress" not in client.get("/api/agents/tuning/missing").json()
        app.state.profiles.progress = None
        for record in ("u0", "u1", "u2", "old", "play"):
            app.state.store.delete("usage", record)  # The rest of this test asserts that no work was recorded.
        saved = client.post("/api/agents/tune", json={"build_id": "missing", "request_id": "tune-saved"}, headers={"X-ASHAD-Request": "1"})
        assert saved.status_code == 200 and saved.json()["id"] == "saved"
        assert app.state.tuning_task is not None  # Never clear the other live tuning task.
        app.state.tuning_task = None
        assert not app.state.store.list("usage") and not app.state.store.leases()


@pytest.mark.asyncio
async def test_lost_reply_lookup_finds_run_accepted_through_real_instance_start(environment, profile):
    store, coord, _, _ = environment
    run = await Instances(coord).start(InstanceRequest(agent_id=profile["id"], primary_goal="Lost reply", request_id="lost-reply"))
    app = offline_app(store.root)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            found = (await client.get("/api/instances/requests/lost-reply")).json()
        assert found["status"] == "accepted" and found["run"]["id"] == run["id"]
    finally:
        app.state.store.close()


def test_interrupted_import_exposes_history_but_blocks_all_mutations(tmp_path):
    app = offline_app(tmp_path)
    (app.state.store.root / ".workspace-import-in-progress.json").write_text("{}", encoding="utf-8")
    with TestClient(app) as client:
        data = client.get("/api/bootstrap").json()
        assert data["workspace_import"]["interrupted"] is True and data["instance_slot"]["available"] is False
        result = client.post("/api/projects", json={"name": "No edit", "request_id": "blocked"}, headers={"X-ASHAD-Request": "1"})
        assert result.status_code == 409 and "journal" in result.json()["detail"]
        assert not app.state.store.list("projects")
        with pytest.raises(ValueError, match="incomplete"):
            app.state.coordinator.require_instance_slot()


@pytest.mark.asyncio
async def test_import_http_cancellation_retains_drain_until_real_disk_work_finishes(tmp_path, monkeypatch):
    import ashad.app as app_module
    started, release = threading.Event(), threading.Event()
    def disk_work(store, **kwargs):
        assert kwargs["target_drained"] is True
        started.set()
        assert release.wait(5)
        raise ValueError("Fixture cancellation; no data touched")
    monkeypatch.setattr(app_module, "import_workspace", disk_work)
    target_lock = acquire(tmp_path / "controller.lock")
    app = offline_app(tmp_path, controller_lock=target_lock)
    request = None
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            request = asyncio.create_task(client.post("/api/workspace/import", json={"source_root": str(tmp_path / "source"),
                "backup_dir": str(tmp_path / "backup")}, headers={"X-ASHAD-Request": "1"}))
            assert await asyncio.to_thread(started.wait, 2)
            request.cancel()
            await asyncio.sleep(0)
            assert app.state.coordinator.draining is True
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert app.state.coordinator.draining is False
    finally:
        release.set()
        if request and not request.done():
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
        app.state.store.close()
        target_lock.close()


def test_http_conflict_exposes_actual_owner_before_requiring_agent(tmp_path):
    app = offline_app(tmp_path)
    app.state.store.put("runs", {"id": "legacy-owned", "state": "Paused", "players": {}})
    with TestClient(app) as client:
        result = client.post("/api/instances", json={"agent_id": "absent", "primary_goal": "Blocked", "request_id": "attempt"},
                             headers={"X-ASHAD-Request": "1"})
        assert result.status_code == 409 and result.json()["code"] == "active_instance_conflict"
        assert result.json()["owning_instances"][0]["run_id"] == "legacy-owned"
        assert len(app.state.store.list("runs")) == 1


def test_migration_api_is_explicit_backed_up_and_replay_safe(tmp_path):
    target_lock = acquire(tmp_path / "data" / "controller.lock")
    app = offline_app(tmp_path / "data", controller_lock=target_lock)
    app.state.store.put("builds", {"id": "legacy-build", "source_revision": "legacy"})
    app.state.store.put("runs", {"id": "historical", "state": "Ended", "plan": {"build_id": "legacy-build"},
                                 "approved_plan": {"build_id": "legacy-build"}, "players": {}})
    before = copy.deepcopy(app.state.store.require("runs", "historical"))
    groups = [{"project_id": "migrated-game", "name": "Legacy game", "build_ids": ["legacy-build"]}]
    headers = {"X-ASHAD-Request": "1"}
    with target_lock, TestClient(app) as client:
        preview = client.post("/api/projects/migration/preview", json={"groups": groups}, headers=headers).json()
        payload = {"groups": groups, "preview_token": preview["preview_token"], "backup_dir": str(tmp_path / "backup")}
        assert client.post("/api/projects/migration/apply", json=payload, headers=headers).status_code == 422
        result = client.post("/api/projects/migration/apply", json={**payload, "confirmed": True}, headers=headers)
        assert result.status_code == 200
        assert client.post("/api/projects/migration/apply", json={**payload, "confirmed": True}, headers=headers).json() == result.json()
        migrated = app.state.store.require("runs", "historical")
        assert migrated == {**before, "project_id": "migrated-game"}
        assert (tmp_path / "backup" / "ashad.sqlite3").is_file()
        assert not app.state.coordinator.draining and len(app.state.store.list("projects")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("section,field,value", [
    ("probe", "ready", None), ("probe", "independent_desktop", None),
    ("probe", "worker_id", "another-worker"), ("probe", "contract_version", "unsupported"),
    ("build", "artifact_sha256", None), ("build", "engine_sha256", "0" * 64),
    ("build", "manifest_sha256", "0" * 64), ("build", "structured_version", 2),
    ("build", "integration_version", 1), ("build", "engine_version", "unverified-engine"),
    ("build", "source_revision", "other-revision"), ("build", "command", ["/game/other"]),
    ("desktop", "rendered", False), ("desktop", "capture_ready", None), ("desktop", "input_ready", False),
    ("status", "lifecycle", "running"), ("status", "lifecycle", "ended"),
    ("status", "binding", {"run_id": "outside-owner"}), ("status", "game_running", True),
    ("status", "human_control", True), ("status", "world_may_continue", True),
    ("status", "held_keys", ["W"]), ("status", "held_buttons", ["left"]),
    ("status", "independent_desktop", None), ("status", "infrastructure_error", "fixture-unhealthy"),
    ("steam", "setup_active", True),
])
async def test_direct_launch_rejects_unknown_or_incompatible_passive_evidence(environment, section, field, value):
    store, coord, plan, desktops = environment
    desktop = desktops["worker-1"]
    target = desktop.status if section == "status" else desktop.probe if section == "probe" else desktop.probe[section]
    if value is None:
        target.pop(field, None)
    else:
        target[field] = value
    worker = store.require("workers", "worker-1")
    worker["last_probe"] = {"ready": True, "independent_desktop": True}  # Cached metadata must never authorize launch.
    store.put("workers", worker)
    scenario = coord.save_scenario(plan)
    with pytest.raises(ValueError, match="fresh desktop readiness is not verified"):
        await coord.launch(scenario["id"], scenario["revision"], "bypass-attempt")
    assert not store.list("runs") and not store.leases() and not store.command("launch", "bypass-attempt")
    assert not coord.ai.backend.requests and not store.list("usage")
    assert [path for path, _ in desktop.calls] == ["/prepare/probe", "/status"]


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["metadata-only", "missing-manifest-path", "changed-pack", "missing-engine", "changed-manifest"])
async def test_new_launch_rechecks_local_pins_before_any_worker_probe(environment, damage):
    from pathlib import Path
    store, coord, plan, desktops = environment
    build = store.require("builds", "build-1")
    root = Path(build["manifest_path"]).parent
    if damage == "metadata-only":
        build.pop("engine_sha256")
        store.put("builds", build)
    elif damage == "missing-manifest-path":
        build.pop("manifest_path")
        store.put("builds", build)
    elif damage == "changed-pack":
        (root / "fixture.pck").write_bytes(b"changed fixture; never executed")
    elif damage == "missing-engine":
        removed = root / "fixture-engine"
        assert removed.resolve().is_relative_to(root.resolve())
        removed.unlink()
    else:
        Path(build["manifest_path"]).write_text("{}", encoding="utf-8")
    scenario = coord.save_scenario(plan)
    with pytest.raises(ValueError, match="legacy build|missing or changed|cannot be verified"):
        await coord.launch(scenario["id"], 1, "unsupported")
    assert not any(desktop.calls for desktop in desktops.values())
    assert not store.list("runs") and not store.leases() and not coord.ai.backend.requests


@pytest.mark.asyncio
async def test_worker_exception_is_unknown_without_leaking_service_credentials(environment):
    store, coord, plan, desktops = environment
    desktops["worker-1"].request = AsyncMock(side_effect=RuntimeError("fixture-private-token"))
    scenario = coord.save_scenario(plan)
    with pytest.raises(ValueError, match="Authenticated passive probe/status unavailable") as error:
        await coord.launch(scenario["id"], 1, "failed-probe")
    assert "fixture-private-token" not in str(error.value)
    assert not store.list("runs") and not store.leases() and not coord.ai.backend.requests


@pytest.mark.asyncio
async def test_final_shared_guard_reprobes_after_instance_selection(environment, profile, monkeypatch):
    store, coord, _, desktops = environment
    desktop = desktops[store.list("workers")[0]["id"]]
    original, count = desktop.request, 0
    async def became_unavailable(method, path, **kwargs):
        nonlocal count
        result = await original(method, path, **kwargs)
        if path == "/prepare/probe":
            count += 1
            if count == 2:
                result["desktop"]["input_ready"] = False
        return result
    monkeypatch.setattr(desktop, "request", became_unavailable)
    with pytest.raises(ValueError, match="input_ready"):
        await Instances(coord).start(InstanceRequest(agent_id=profile["id"], primary_goal="Probe again", request_id="recheck"))
    assert count == 2
    assert not store.list("runs") and not store.leases() and not store.command("instance", "recheck")
    assert not coord.ai.backend.requests
    assert all(path in {"/prepare/probe", "/status"} for path, _ in desktop.calls)


@pytest.mark.asyncio
async def test_direct_steam_launch_cannot_use_cached_account_entitlement(environment):
    store, coord, plan, desktops = environment
    store.put("accounts", {"id": "steam-fixture", "worker_id": "worker-1", "steam_id": "76561198000000001",
                           "app_id": 3753020, "readiness": "ready", "last_verification": {"verified": True}})
    plan["transport"].update(mode="steam", app_id=3753020)
    plan["players"][0]["account_id"] = "steam-fixture"
    scenario = coord.save_scenario(plan)
    with pytest.raises(ValueError, match="Steam readiness is unknown.*No ENet fallback"):
        await coord.launch(scenario["id"], 1, "steam-no-proof")
    assert not store.list("runs") and not store.leases() and not coord.ai.backend.requests
    assert all(path in {"/prepare/probe", "/status"} for path, _ in desktops["worker-1"].calls)


@pytest.mark.asyncio
async def test_exact_frozen_retry_skips_missing_build_provider_and_worker_checks(environment):
    store, coord, plan, desktops = environment
    scenario = coord.save_scenario(plan)
    first = await coord.launch(scenario["id"], 1, "frozen")
    before = copy.deepcopy(first)
    calls = {key: list(desktop.calls) for key, desktop in desktops.items()}
    store.delete("builds", "build-1")
    coord.draining = True
    coord.ai.validate = AsyncMock(side_effect=AssertionError("retry must not verify provider"))
    assert await coord.launch(scenario["id"], 1, "frozen") == before
    assert {key: desktop.calls for key, desktop in desktops.items()} == calls
    coord.ai.validate.assert_not_awaited()
    assert len(store.list("runs")) == 1


@pytest.mark.asyncio
async def test_drain_beginning_during_probe_prevents_acceptance(environment, monkeypatch):
    store, coord, plan, desktops = environment
    desktop = desktops["worker-1"]
    original = desktop.request
    async def drain_during_status(method, path, **kwargs):
        result = await original(method, path, **kwargs)
        if path == "/status":
            coord.draining = True
        return result
    monkeypatch.setattr(desktop, "request", drain_during_status)
    scenario = coord.save_scenario(plan)
    with pytest.raises(ValueError, match="draining"):
        await coord.launch(scenario["id"], 1, "drain-race")
    assert not store.list("runs") and not store.leases() and not coord.ai.backend.requests


def test_instance_http_cannot_bypass_positive_probe_with_registered_metadata(environment, profile, tmp_path):
    store, _, _, desktops = environment
    app = offline_app(tmp_path / "http-controller")
    for kind in ("settings", "projects", "builds", "workers", "agents"):
        for record in store.list(kind):
            if record.get("id"):
                app.state.store.put(kind, record)
    for desktop in desktops.values():
        desktop.probe = {"ok": True}
    app.state.coordinator.client_factory = lambda record: desktops[record["id"]]
    with TestClient(app) as client:
        result = client.post("/api/instances", json={"agent_id": profile["id"], "primary_goal": "Bypass metadata", "request_id": "http-no-probe"},
                             headers={"X-ASHAD-Request": "1"})
        assert result.status_code == 422 and "fresh positive" in result.json()["detail"]
        assert not app.state.store.list("runs") and not app.state.store.leases()
        assert not app.state.store.list("usage")
    assert all(path in {"/prepare/probe", "/status"} for desktop in desktops.values() for path, _ in desktop.calls)
