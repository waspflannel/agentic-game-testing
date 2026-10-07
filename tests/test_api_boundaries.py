"""HTTP boundary and input-validation contracts: temporary stores and doubles only."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from ashad.app import create_app
from test_controller import MemoryCredentials, FakeCodexBackend

HEADERS = {"X-ASHAD-Request": "1"}


def offline_app(root):
    return create_app(root, credentials=MemoryCredentials(), start_services=False, initialize=False,
                      ai_backend=FakeCodexBackend())


@pytest.mark.parametrize("host", ["attacker.example:8740", "attacker.example", "127.0.0.1.attacker.example:8740",
                                  "localhost.attacker.example", "10.0.0.5:8740", "testserver", ""])
def test_rebound_or_foreign_host_is_rejected_even_with_matching_origin(tmp_path, host):
    app = offline_app(tmp_path)
    before = app.state.store.require("settings", "main")
    forged = {"Host": host, "Origin": f"http://{host}", **HEADERS}
    with TestClient(app) as client:
        assert client.get("/api/bootstrap", headers=forged).status_code == 403
        assert client.get("/static/app.js", headers=forged).status_code == 403
        assert client.post("/api/settings", json={"storage_reserve_bytes": 5}, headers=forged).status_code == 403
        assert client.post("/mcp", json={}, headers={**forged, "Authorization": "Bearer guess"}).status_code == 403
        assert client.post("/api/runs/r/players/p/mcp-session", json={"control": True}, headers=forged).status_code == 403
        assert app.state.store.require("settings", "main") == before


@pytest.mark.parametrize("host", ["127.0.0.1:8740", "127.0.0.1", "localhost:8740", "localhost", "[::1]:8740", "127.0.0.1:51234"])
def test_loopback_hosts_pass_the_boundary(tmp_path, host):
    app = offline_app(tmp_path)
    with TestClient(app, base_url=f"http://{host}") as client:
        assert client.get("/api/bootstrap").status_code == 200
        assert client.post("/api/settings", json={"storage_reserve_bytes": 5}, headers={**HEADERS, "Origin": f"http://{host}"}).status_code == 200
        assert client.post("/mcp", json={}).status_code == 401  # Past the Host check; MCP auth still applies.
        assert app.state.store.require("settings", "main")["storage_reserve_bytes"] == 5


@pytest.mark.parametrize("body", ['{"spend_usd": Infinity}', '{"duration_seconds": NaN}', '{"duration_seconds": -Infinity}',
                                  '{"spend_usd": 1e400}', '{"spend_usd": true}', '{"storage_reserve_bytes": true}',
                                  '{"storage_reserve_bytes": 1.5}'])
def test_non_finite_or_boolean_settings_are_rejected_without_persisting(tmp_path, body):
    app = offline_app(tmp_path)
    before = app.state.store.require("settings", "main")
    with TestClient(app) as client:
        result = client.post("/api/settings", content=body, headers={**HEADERS, "Content-Type": "application/json"})
        assert result.status_code == 422
        assert app.state.store.require("settings", "main") == before
        assert client.get("/api/bootstrap").status_code == 200
        assert client.post("/api/settings", json={"spend_usd": 2.5, "duration_seconds": 10**30}, headers=HEADERS).status_code == 200


def test_malformed_bodies_are_422_not_500(tmp_path):
    app = offline_app(tmp_path)
    store = app.state.store
    store.put("runs", {"id": "r1", "state": "Ended", "players": {}, "plan": {}})
    store.put("workers", {"id": "w1", "url": "http://10.0.0.5:8766", "credential_ref": "worker:w1"})
    with TestClient(app, raise_server_exceptions=False) as client:
        for path, body in [("/api/workers", {}), ("/api/workers", {"url": 5}), ("/api/workers", {"url": "http://h:1", "enabled": "yes", "token": "t"}),
                           ("/api/runs/r1/flag", {}), ("/api/runs/r1/flag", {"description": "  "}),
                           ("/api/accounts", {"steam_id": "76561198000000001", "app_id": 3753020}),
                           ("/api/accounts", {"name": "A", "worker_id": "w1", "steam_id": "76561198000000001", "app_id": "Infinity"}),
                           ("/api/runs/r1/control", {"command": "stop", "command_id": "c1", "run_id": "other"}),
                           ("/api/runs/r1/control", {"command_id": "c1"}),
                           ("/api/builds", {"manifest_path": str(tmp_path / "missing" / "manifest.json"), "project_id": "p"}),
                           ("/api/workers/w1/steam/verify", {})]:
            result = client.post(path, json=body, headers=HEADERS)
            assert result.status_code == 422, (path, body, result.text)
            assert isinstance(result.json()["detail"], str)
        accounts = client.post("/api/accounts", content='{"name":"A","worker_id":"w1","steam_id":"76561198000000001","app_id":Infinity}',
                               headers={**HEADERS, "Content-Type": "application/json"})
        assert accounts.status_code == 422
        assert not store.list("accounts") and not store.list("incidents")


def test_evidence_files_for_unknown_runs_do_not_create_directories(tmp_path):
    app = offline_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/runs/never-created/files/ledger.jsonl").status_code == 404
    assert not (app.state.store.root / "runs" / "never-created").exists()


def test_manifest_project_id_never_becomes_membership(tmp_path):
    app = offline_app(tmp_path / "data")
    store = app.state.store
    store.put("projects", {"id": "mine", "name": "Mine"})
    store.put("projects", {"id": "other-project", "name": "Other"})
    source = tmp_path / "source"
    source.mkdir()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"id": "b1", "source_root": str(source), "command": ["/game/engine"],
                                    "project_id": "other-project"}), encoding="utf-8")
    with TestClient(app) as client:
        unassigned = client.post("/api/builds", json={"manifest_path": str(manifest)}, headers=HEADERS)
        assert unassigned.status_code == 422 and "project" in unassigned.json()["detail"]
        assert not store.list("builds")
        saved = client.post("/api/builds", json={"manifest_path": str(manifest), "project_id": "mine"}, headers=HEADERS)
        assert saved.status_code == 200 and saved.json()["project_id"] == "mine"
        assert store.require("builds", "b1")["project_id"] == "mine"


def test_worker_url_change_requires_new_token_and_owned_workers_are_frozen(tmp_path):
    app = offline_app(tmp_path)
    store, credentials = app.state.store, app.state.credentials
    credentials.save("worker:w1", "old-token")
    original = {"id": "w1", "name": "Desk", "url": "http://10.0.0.5:8766", "credential_ref": "worker:w1", "enabled": True}
    store.put("workers", original)
    with TestClient(app) as client:
        for url in ("http://attacker.example:8766", "https://10.0.0.5:8766", "http://10.0.0.5:9999"):
            moved = client.post("/api/workers", json={"id": "w1", "url": url}, headers=HEADERS)
            assert moved.status_code == 422 and "token" in moved.json()["detail"]
        assert store.require("workers", "w1") == original and credentials.get("worker:w1") == "old-token"
        assert client.post("/api/workers", json={"url": "http://new.example:8766"}, headers=HEADERS).status_code == 422
        renamed = client.post("/api/workers", json={"id": "w1", "name": "Renamed", "url": "http://10.0.0.5:8766/"}, headers=HEADERS)
        assert renamed.status_code == 200 and credentials.get("worker:w1") == "old-token"
        moved = client.post("/api/workers", json={"id": "w1", "url": "http://new.example:8766", "token": "new-token"}, headers=HEADERS)
        assert moved.status_code == 200 and credentials.get("worker:w1") == "new-token"
        assert store.reserve("run-owner", [("worker", "w1")])
        leased = client.post("/api/workers", json={"id": "w1", "url": "http://new.example:8766", "token": "x"}, headers=HEADERS)
        assert leased.status_code == 422 and "lease" in leased.json()["detail"]
        store.release("run-owner")
        store.put("runs", {"id": "queued", "state": "Queued", "players": {}, "plan": {"players": [{"worker_id": "w1"}]}})
        referenced = client.post("/api/workers", json={"id": "w1", "url": "http://new.example:8766", "token": "x"}, headers=HEADERS)
        assert referenced.status_code == 422 and "approved run" in referenced.json()["detail"]
    assert credentials.get("worker:w1") == "new-token"


@pytest.mark.parametrize("busy", ["recalculating", "pending_inference"])
def test_sign_in_changes_wait_for_paused_goal_recalculation_and_inference(tmp_path, busy):
    app = offline_app(tmp_path)
    backend = app.state.ai.backend
    backend.login_start, backend.logout = AsyncMock(return_value={"auth_url": "https://auth.openai.com/x", "login_id": "l"}), AsyncMock()
    coordinator = app.state.coordinator
    if busy == "recalculating":
        app.state.store.put("runs", {"id": "paused", "state": "Paused", "players": {},
                                     "goal_recalculation": {"command_id": "c", "status": "planning"}})
    else:
        coordinator.tasks[("paused", "player-1")] = SimpleNamespace(done=lambda: False)
    try:
        with TestClient(app) as client:
            for path in ("/api/settings/codex/login", "/api/settings/codex/logout"):
                result = client.post(path, json={}, headers=HEADERS)
                assert result.status_code == 422 and "recalculation" in result.json()["detail"]
            backend.login_start.assert_not_awaited()
            backend.logout.assert_not_awaited()
            coordinator.tasks.clear()
            app.state.store.delete("runs", "paused")
            assert client.post("/api/settings/codex/login", json={}, headers=HEADERS).status_code == 200
    finally:
        coordinator.tasks.clear()


def test_bootstrap_carries_usage_count_and_run_detail_filters_its_rows(tmp_path):
    app = offline_app(tmp_path)
    store = app.state.store
    store.put("runs", {"id": "mine", "state": "Ended", "players": {}, "events": {}, "plan": {}})
    for index in range(3):
        store.put("usage", {"id": f"u{index}", "run_id": "mine" if index < 2 else "other"})
    store.put("usage", {"id": "tuning", "purpose": "tuning"})
    store.put("incidents", {"id": "i1", "run_id": "mine"})
    store.put("incidents", {"id": "i2", "run_id": "other"})
    with TestClient(app) as client:
        bootstrap = client.get("/api/bootstrap").json()
        assert "usage" not in bootstrap and bootstrap["usage_count"] == 4
        assert {row["id"] for row in client.get("/api/usage").json()["usage"]} == {"u0", "u1", "u2", "tuning"}
        detail = client.get("/api/runs/mine").json()
        assert {row["id"] for row in detail["usage"]} == {"u0", "u1"}
        assert [row["id"] for row in detail["incidents"]] == ["i1"]


def test_steam_verification_requires_the_free_global_slot_and_unleased_worker(tmp_path):
    app = offline_app(tmp_path)
    store = app.state.store
    store.put("workers", {"id": "w1", "url": "http://10.0.0.5:8766", "credential_ref": "worker:w1"})
    store.put("builds", {"id": "b1", "steam_app_id": 3753020, "command": ["/game/engine"]})
    store.put("accounts", {"id": "a1", "worker_id": "w1", "steam_id": "76561198000000001", "app_id": 3753020})
    worker = SimpleNamespace(steam_verify=AsyncMock(return_value={"verified": True}), steam_setup_finish=AsyncMock(return_value={}))
    app.state.coordinator.client_factory = lambda record: worker
    body = {"account_id": "a1", "build_id": "b1"}
    with TestClient(app) as client:
        store.put("runs", {"id": "owner", "state": "Running", "players": {}})
        blocked = client.post("/api/workers/w1/steam/verify", json=body, headers=HEADERS)
        assert blocked.status_code == 409 and blocked.json()["code"] == "active_instance_conflict"
        store.put("runs", {"id": "owner", "state": "Ended", "players": {}})
        assert store.reserve("owner", [("worker", "w1")])
        assert client.post("/api/workers/w1/steam/verify", json=body, headers=HEADERS).status_code == 409
        assert client.post("/api/workers/w1/steam/finish", json={}, headers=HEADERS).status_code == 422
        worker.steam_verify.assert_not_awaited()
        worker.steam_setup_finish.assert_not_awaited()
        assert store.require("accounts", "a1").get("readiness") is None
        store.release("owner")
        assert client.post("/api/workers/w1/steam/verify", json=body, headers=HEADERS).json() == {"verified": True}
        assert client.post("/api/workers/w1/steam/finish", json={}, headers=HEADERS).status_code == 200
        assert store.require("accounts", "a1")["readiness"] == "ready"


@pytest.mark.asyncio
async def test_tuning_cancelled_outside_owned_quit_propagates_instead_of_409(tmp_path):
    app = offline_app(tmp_path)
    entered = asyncio.Event()

    async def tune(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    app.state.profiles.tune = tune
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
                request = asyncio.create_task(client.post("/api/agents/tune", json={}, headers=HEADERS))
                await asyncio.wait_for(entered.wait(), 2)
                assert not app.state.coordinator.draining
                tuning = app.state.tuning_task
                tuning.cancel()  # Not the owned quit drain: no 409 is fabricated from the cancellation.
                with pytest.raises(Exception):
                    await asyncio.wait_for(request, 5)
                assert tuning.cancelled()
                assert app.state.tuning_task is None and app.state.operations == {}
    finally:
        app.state.store.close()
