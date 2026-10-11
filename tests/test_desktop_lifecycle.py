"""Desktop quit contracts use temporary stores and worker/provider doubles only."""
import asyncio
import copy
import io
import json
import secrets
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from ashad.app import create_app
from ashad.desktop import DesktopLifecycle, read_launch_config
from test_controller import environment, new_run, MemoryCredentials, FakeCodexBackend


def lifecycle(environment):
    store, coord, _, _ = environment
    state = SimpleNamespace(store=store, coordinator=coord, ai=coord.ai, operations={}, controls_task=None)
    server = SimpleNamespace(should_exit=False)
    return DesktopLifecycle(SimpleNamespace(state=state), server), state, server


@pytest.mark.asyncio
async def test_cancel_quit_leaves_paused_human_owned_instance_untouched(environment):
    store, coord, _, desktops = environment
    run = await new_run(environment)
    await coord.admit(run)
    run = store.require("runs", run["id"])
    run.update(state="Paused", human_control={"player_id": "player-1"})
    store.put("runs", run)
    before, calls = copy.deepcopy(run), list(desktops["worker-1"].calls)
    quit, _, server = lifecycle(environment)
    result = await quit.shutdown()
    assert result["drained"] is False
    assert result["owners"][0]["reason"] == "human_control"
    assert store.require("runs", run["id"]) == before
    assert desktops["worker-1"].calls == calls
    assert not coord.draining and not server.should_exit


@pytest.mark.asyncio
async def test_stop_and_quit_drains_all_legacy_owners_and_blocks_racing_start(environment, monkeypatch):
    store, coord, plan, desktops = environment
    first = await new_run(environment)
    await coord.admit(first)
    # Prior versions allowed concurrent owners; upgrade must not forget them.
    legacy = copy.deepcopy(store.require("runs", first["id"]))
    legacy["id"] = "legacy-second-owner"
    legacy["players"]["player-1"].update(worker_id="worker-2", epoch="legacy-epoch")
    legacy["plan"]["players"][0]["worker_id"] = "worker-2"
    store.put("runs", legacy)
    assert store.reserve(legacy["id"], [("worker", "worker-2")])
    started, release = asyncio.Event(), asyncio.Event()
    original = desktops["worker-1"].request

    async def slow_stop(method, path, **kwargs):
        if path == "/stop":
            started.set()
            await release.wait()
        return await original(method, path, **kwargs)

    monkeypatch.setattr(desktops["worker-1"], "request", slow_stop)
    quit, _, server = lifecycle(environment)
    task = asyncio.create_task(quit.shutdown(stop_runs=True))
    await started.wait()
    scenario = coord.save_scenario(plan)
    with pytest.raises(ValueError, match="draining"):
        await coord.launch(scenario["id"], scenario["revision"], "racing-start")
    assert len(store.list("runs")) == 2
    release.set()
    assert await task == {"drained": True}
    assert server.should_exit and coord.draining
    assert coord.occupied_instances() == [] and store.leases() == []
    assert all(run["state"] == "Ended" for run in store.list("runs"))
    assert any(path == "/stop" for path, _ in desktops["worker-2"].calls)


@pytest.mark.asyncio
async def test_stop_and_quit_consent_is_bound_to_the_listed_owners(environment):
    store, coord, _, desktops = environment
    run = await new_run(environment)
    await coord.admit(run)
    before, calls = store.require("runs", run["id"]), list(desktops["worker-1"].calls)
    quit, _, server = lifecycle(environment)
    # The user confirmed an idle quit (or a different run) before this owner appeared.
    for shown in ([], ["other-run"], [run["id"], run["id"]]):
        result = await quit.shutdown(stop_runs=True, expected_run_ids=shown)
        assert result["drained"] is False and "changed" in result["detail"]
        assert result["owners"][0]["run_id"] == run["id"]
    assert store.require("runs", run["id"]) == before and desktops["worker-1"].calls == calls
    assert not coord.draining and not server.should_exit
    assert await quit.shutdown(stop_runs=True, expected_run_ids=[run["id"]]) == {"drained": True}


@pytest.mark.asyncio
async def test_failed_teardown_keeps_service_and_ownership_for_reconciliation(environment):
    store, coord, _, desktops = environment
    run = await new_run(environment)
    await coord.admit(run)
    desktops["worker-1"].fail_stop = True
    quit, _, server = lifecycle(environment)
    result = await quit.shutdown(stop_runs=True)
    assert not result["drained"] and "unconfirmed" in result["detail"]
    assert not coord.draining and not server.should_exit
    assert store.leases(run["id"]) and coord.occupied_instances()[0]["reason"] == "teardown_unconfirmed"
    desktops["worker-1"].fail_stop = False
    assert await quit.shutdown(stop_runs=True) == {"drained": True}
    assert not store.leases() and not coord.occupied_instances()


@pytest.mark.asyncio
async def test_unknown_lease_never_reports_successful_quit(environment):
    store, coord, _, _ = environment
    assert store.reserve("absent-run", [("worker", "worker-1")])
    quit, _, server = lifecycle(environment)
    result = await quit.shutdown(stop_runs=True)
    assert not result["drained"] and not server.should_exit
    assert result["owners"] == [{"run_id": "absent-run", "state": "Unknown", "reason": "orphaned_lease"}]


@pytest.mark.asyncio
async def test_get_controls_requires_explicit_cancellation_and_keeps_honest_usage(environment):
    store, coord, _, _ = environment
    started = asyncio.Event()

    async def provider(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    coord.ai.claude_backend.write_controls = provider
    quit, state, server = lifecycle(environment)
    task = asyncio.create_task(coord.ai._request("fixture", None, "tuning", None, "sonnet", 7))
    state.controls_task = task
    state.operations[task] = {"kind": "controls", "path": "/api/agents/controls"}
    await started.wait()
    assert not (await quit.shutdown())["drained"] and not task.done()
    assert await quit.shutdown(cancel_operations=True) == {"drained": True}
    assert task.cancelled() and server.should_exit
    assert store.list("agents") == []
    usage = store.list("usage")[0]
    assert usage["status"] == "request_cancelled" and usage["usage"] is None
    assert usage["estimated_cost_usd"] is None


@pytest.mark.asyncio
async def test_setup_mutation_is_waited_not_abandoned(environment):
    quit, state, server = lifecycle(environment)
    state.operations[object()] = {"kind": "mutation", "path": "/api/workers/fixture/steam/start"}
    assert not (await quit.shutdown(cancel_operations=True))["drained"]
    assert not state.coordinator.draining and not server.should_exit
    state.operations.clear()
    assert await quit.shutdown() == {"drained": True}


@pytest.mark.asyncio
async def test_drain_blocks_admission_and_resume_even_outside_http(environment):
    store, coord, _, desktops = environment
    run = await new_run(environment)
    coord.draining = True
    await coord.admit(run)
    assert store.require("runs", run["id"])["state"] == "Queued"
    assert desktops["worker-1"].calls == [("/prepare/probe", {"build": run["build"]}), ("/status", None)]
    run["state"] = "Paused"
    store.put("runs", run)
    coord.ai.validate = AsyncMock()
    with pytest.raises(ValueError, match="draining"):
        await coord.control(run["id"], "resume", command_id="resume-during-drain")
    coord.ai.validate.assert_not_awaited()


def test_http_drain_blocks_mutations_but_keeps_readable_history(tmp_path):
    app = create_app(tmp_path, credentials=MemoryCredentials(), start_services=False,
                     ai_backend=FakeCodexBackend())
    with TestClient(app) as client:
        app.state.coordinator.draining = True
        assert client.get("/api/bootstrap").status_code == 200
        result = client.post("/api/builds", json={"manifest_path": "never-open-this"}, headers={"X-ASHAD-Request": "1"})
        assert result.status_code == 409 and "draining" in result.json()["detail"]
        assert app.state.operations == {}


@pytest.mark.asyncio
async def test_http_get_controls_cancellation_returns_conflict_and_releases_operation(tmp_path):
    import httpx
    app = create_app(tmp_path, credentials=MemoryCredentials(), start_services=False,
                     ai_backend=FakeCodexBackend())
    entered = asyncio.Event()

    async def provider(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    app.state.ai.claude_backend.write_controls = provider

    async def gather(*args, **kwargs):
        return await app.state.ai._request("fixture", None, "tuning", None, "sonnet", 7)

    app.state.controls.gather = gather
    server = SimpleNamespace(should_exit=False)
    quit = DesktopLifecycle(app, server)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            request = asyncio.create_task(client.post("/api/agents/controls", json={}, headers={"X-ASHAD-Request": "1"}))
            await entered.wait()
            assert quit.status()["operations"][0]["kind"] == "controls"
            assert await quit.shutdown(cancel_operations=True) == {"drained": True}
            response = await request
            assert response.status_code == 409 and "cancelled" in response.json()["detail"]
            assert app.state.operations == {} and app.state.controls_task is None
            assert app.state.store.list("usage")[0]["status"] == "request_cancelled"


def test_private_utf8_pipe_preserves_international_user_data_path(tmp_path):
    root = str(tmp_path / "ユーザー café 📁")
    config = {"data_root": root, "session_key": "s" * 43, "owner_key": "o" * 43, "instance_id": "fixture_unicode"}
    source = str(Path(__file__).resolve().parents[1])
    script = f"import sys,json;sys.path.insert(0,{source!r});from ashad.desktop import read_launch_config;print(json.dumps({{'root':read_launch_config(sys.stdin)['data_root']}}))"
    result = subprocess.run([sys.executable, "-I", "-B", "-X", "utf8", "-c", script],
        input=json.dumps(config, ensure_ascii=False).encode("utf-8"), capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert json.loads(result.stdout)["root"] == root


def test_launch_handshake_validates_boundaries_without_echoing_keys(tmp_path):
    config = {"data_root": str(tmp_path), "session_key": secrets.token_urlsafe(32),
              "owner_key": secrets.token_urlsafe(32), "instance_id": "launch-id"}
    assert read_launch_config(io.StringIO(json.dumps(config))) == config
    for changed in ({**config, "data_root": "relative"}, {**config, "owner_key": config["session_key"]},
                    {**config, "instance_id": "../escape"}, {**config, "command": "shell"},
                    {**config, "session_key": "x" * 42}, {**config, "owner_key": "x" * 43 + "\n"}):
        with pytest.raises(ValueError):
            read_launch_config(io.StringIO(json.dumps(changed)))
    with pytest.raises(ValueError):
        read_launch_config(io.StringIO("x" * 4097))
