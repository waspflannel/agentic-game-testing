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
    assert all(path == "/health" for desktop in desktops.values() for path, _ in desktop.calls)


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
    assert all(path == "/health" for desktop in desktops.values() for path, _ in desktop.calls)


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
    return create_app(root, credentials=MemoryCredentials(), start_services=False, initialize=False,
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
    app.state.store.command("launch", "start-saved", {"run_id": "saved-run"})
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
        saved = client.post("/api/agents/tune", json={"build_id": "missing", "request_id": "tune-saved"}, headers={"X-ASHAD-Request": "1"})
        assert saved.status_code == 200 and saved.json()["id"] == "saved"
        assert app.state.tuning_task is not None  # Never clear the other live tuning task.
        app.state.tuning_task = None
        assert not app.state.store.list("usage") and not app.state.store.leases()


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
