"""Actual workspace HTTP orchestration; disposable files and provider/worker doubles only."""
import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

import ashad.app as app_module
from ashad.app import create_app
from ashad.process_lock import acquire
from ashad.repository import sha256_file
from ashad.workspace_environment import ENGINE, inspect_environment
from test_controller import MemoryCredentials, FakeCodexBackend


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "application data"
    with acquire(root / "controller.lock") as lock:
        app = create_app(root, credentials=MemoryCredentials(), ai_backend=FakeCodexBackend(),
                         start_services=False, controller_lock=lock)
        store = app.state.store
        store.put("projects", {"id": "project", "name": "Same game"})
        folder = tmp_path / "supported game ユーザー"
        folder.mkdir()
        (folder / "source").mkdir()
        (folder / "engine").write_bytes(b"fixture engine, never executed")
        (folder / "game.pck").write_bytes(b"fixture pack, never executed")
        build = {"id": "build", "project_id": "project", "name": "Fixture", "source_revision": "fixture-commit",
                 "source_root": str(folder / "source"), "structured_version": 1, "integration_version": 2,
                 "engine_version": ENGINE, "engine_sha256": sha256_file(folder / "engine"),
                 "artifact_sha256": sha256_file(folder / "game.pck"),
                 "command": ["/game/engine", "--main-pack", "/game/game.pck"]}
        manifest = folder / "manifest.json"
        manifest.write_text(json.dumps(build), encoding="utf-8")
        build.update(manifest_path=str(manifest), manifest_sha256=sha256_file(manifest))
        store.put("builds", build)
        app.state.ai.validate = AsyncMock(return_value={"configured": True, "status": "ready", "message": "Fixture ready"})
        yield app, build
        store.close()


def positive(build, worker):
    probe = {"ready": True, "contract_version": "ashad-worker/1", "worker_id": worker["id"],
             "independent_desktop": True, "build": build, "desktop": {"rendered": True, "capture_ready": True, "input_ready": True},
             "lifecycle": "unbound", "binding": None, "game_running": False, "world_may_continue": False,
             "human_control": False, "held_keys": [], "held_buttons": [], "infrastructure_error": None,
             "steam": {"setup_active": False, "requires_human_setup": True}}
    return inspect_environment(build, worker, probe, probe)


def test_readiness_requires_positive_fresh_environment_and_provider(workspace, monkeypatch):
    app, build = workspace
    worker = {"id": "fixture-worker", "name": "Desktop", "enabled": True}
    app.state.store.put("workers", worker)
    inspect = AsyncMock(return_value=positive(build, worker))
    monkeypatch.setattr(app_module, "inspect_fresh_environment", inspect)
    with TestClient(app) as client:
        result = client.get("/api/projects/project/readiness?build_id=build").json()
        assert result["ready"] and result["build_validation"]["ready"] and result["environments"][0]["ready"]
        assert result["steam"]["ready"] is False and not app.state.store.list("runs")
        assert inspect.await_count == 1 and app.state.ai.validate.await_count == 2
        app.state.ai.validate.return_value = {"configured": False, "status": "configuration_required", "message": "Signed out"}
        assert client.get("/api/projects/project/readiness?build_id=build").json()["ready"] is False
        app.state.coordinator.draining = True
        slot = client.get("/api/projects/project/readiness?build_id=build").json()["slot"]
        assert slot["available"] is False and not slot["owners"] and "setup" in slot["blocked_reason"]
        app.state.coordinator.draining = False


def test_missing_or_changed_build_never_probes_or_claims_ready(workspace, monkeypatch):
    app, build = workspace
    inspect = AsyncMock(side_effect=AssertionError("Invalid build must not probe"))
    monkeypatch.setattr(app_module, "inspect_fresh_environment", inspect)
    app.state.store.put("workers", {"id": "fixture-worker"})
    app.state.store.put("builds", {**build, "engine_sha256": "0" * 64})
    with TestClient(app) as client:
        result = client.get("/api/projects/project/readiness?build_id=build").json()
        assert not result["ready"] and not result["build_validation"]["ready"] and result["environments"] == []
        assert inspect.await_count == 0 and not app.state.store.list("runs")


def test_selected_profile_model_is_not_replaced_and_membership_is_checked(workspace, monkeypatch):
    app, build = workspace
    app.state.store.put("agents", {"id": "agent", "build_id": "build", "project_id": "project", "model": "gpt-6-sol"})
    monkeypatch.setattr(app.state.profiles, "validated", lambda key: app.state.store.require("agents", key))
    with TestClient(app) as client:
        result = client.get("/api/projects/project/readiness?build_id=build&agent_id=agent").json()
        assert result["provider"]["model"] == "gpt-6-sol"
        app.state.ai.validate.assert_awaited_once_with("gpt-6-sol")
        app.state.store.put("projects", {"id": "other", "name": "Same game"})
        assert client.get("/api/projects/other/readiness?build_id=build&agent_id=agent").status_code == 422
        assert not app.state.store.list("runs")


@pytest.mark.parametrize("body", [{"build_id": "build", "request_id": "prepare", "confirmed": False},
    {"build_id": "build", "request_id": "prepare", "confirmed": True, "target_drained": True}])
def test_prepare_requires_exact_explicit_permission_and_never_trusts_body_drain(workspace, monkeypatch, body):
    app, _ = workspace
    prepare = AsyncMock(side_effect=AssertionError("No preparation without consent"))
    monkeypatch.setattr(app_module, "prepare_environment", prepare)
    with TestClient(app) as client:
        assert client.post("/api/projects/project/environments/prepare", json=body, headers={"X-ASHAD-Request": "1"}).status_code == 422
        assert prepare.await_count == 0


def test_prepare_uses_real_owned_drain_and_does_not_infer_or_launch(workspace, monkeypatch):
    app, _ = workspace
    async def prepare(store, credentials, **kwargs):
        assert app.state.coordinator.lock.locked() and app.state.coordinator.draining
        assert kwargs["target_drained"] is True and kwargs["confirmed"] is True
        assert not store.list("runs")
        return {"status": "completed", "environment": {"ready": False, "reason": "Fixture still booting"}}
    mocked = AsyncMock(side_effect=prepare)
    monkeypatch.setattr(app_module, "prepare_environment", mocked)
    with TestClient(app) as client:
        result = client.post("/api/projects/project/environments/prepare", json={"build_id": "build", "request_id": "prepare", "confirmed": True}, headers={"X-ASHAD-Request": "1"})
        assert result.status_code == 200 and result.json()["environment"]["ready"] is False
        assert not app.state.coordinator.draining and not app.state.store.list("runs")
        assert app.state.ai.validate.await_count == 0
        app.state.controller_lock = None
        assert client.post("/api/projects/project/environments/prepare", json={"build_id": "build", "request_id": "prepare", "confirmed": True}, headers={"X-ASHAD-Request": "1"}).status_code == 422
        assert mocked.await_count == 1


def test_occupied_instance_refuses_prepare_with_owner_link(workspace, monkeypatch):
    app, _ = workspace
    app.state.store.put("runs", {"id": "owner", "state": "Paused", "players": {}, "plan": {"players": []}})
    prepare = AsyncMock(side_effect=AssertionError("Never prepare occupied instance"))
    monkeypatch.setattr(app_module, "prepare_environment", prepare)
    with TestClient(app) as client:
        response = client.post("/api/projects/project/environments/prepare", json={"build_id": "build", "request_id": "prepare", "confirmed": True}, headers={"X-ASHAD-Request": "1"})
        assert response.status_code == 409 and response.json()["code"] == "active_instance_conflict"
        assert response.json()["owning_instances"][0]["run_id"] == "owner" and prepare.await_count == 0


@pytest.mark.asyncio
async def test_cancelled_prepare_keeps_drain_until_actual_operation_settles(workspace, monkeypatch):
    app, _ = workspace
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def prepare(*args, **kwargs):
        entered.set()
        await release.wait()
        assert app.state.coordinator.draining
        finished.set()
        return {"status": "completed"}
    monkeypatch.setattr(app_module, "prepare_environment", prepare)
    task = None
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            task = asyncio.create_task(client.post("/api/projects/project/environments/prepare",
                json={"build_id": "build", "request_id": "prepare", "confirmed": True}, headers={"X-ASHAD-Request": "1"}))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            assert app.state.coordinator.draining and app.state.coordinator.lock.locked() and not finished.is_set()
            blocked = await client.post("/api/settings", json={}, headers={"X-ASHAD-Request": "1"})
            assert blocked.status_code == 409
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.wait_for(finished.wait(), 2)
            assert not app.state.coordinator.draining and not app.state.coordinator.lock.locked()
    finally:
        release.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
