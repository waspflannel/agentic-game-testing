"""Dashboard boundary checks; controlled records live only in pytest tmp_path."""
import asyncio
import sqlite3
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from ashad.app import create_app
from ashad.models import MODEL, Plan
from test_controller import FakeCodexBackend


class NoCredentials:
    def get(self, reference="openai"):
        return None


def app_at(tmp_path):
    app = create_app(tmp_path / "ui-contract", credentials=NoCredentials(), start_services=False, initialize=False)
    app.state.ai.backend = FakeCodexBackend(ready=False)
    return app


def test_codex_migrates_settings_and_drafts_without_rewriting_run_history(tmp_path):
    from ashad.store import Store
    root = tmp_path / "migration"
    old_plan = {**Plan().model_dump(), "reasoning_effort": "high"}
    historical = {"id": "old-run", "state": "Ended", "model": "gpt-6.1-sol", "plan": old_plan}
    store = Store(root)
    store.put("settings", {"id": "main", "concurrent_sessions": 3, "reasoning_effort": "high", "pricing": {"output_per_million": 10}})
    store.put("settings", {"model": "gpt-6.1-sol", "status": "ready"}, "ai_status")
    store.put("runs", historical)
    store.close()
    app = create_app(root, NoCredentials(), start_services=False, initialize=False)
    app.state.ai.backend = FakeCodexBackend(ready=False)
    with TestClient(app) as client:
        data = client.get("/api/bootstrap").json()
        assert data["settings"]["concurrent_sessions"] == 3
        assert "reasoning_effort" not in data["settings"] and "defaults" not in data
        assert data["settings"]["pricing"]["billing"] == "subscription"
        assert data["ai"]["api"] == "codex" and data["ai"]["model"] == "gpt-6-luna"
        assert data["runs"] == [historical]
        assert "reasoning_effort" not in Plan.model_validate(old_plan).model_dump()




def test_ended_run_exposes_evidence_gaps_without_synthetic_frames(tmp_path):
    app = app_at(tmp_path)
    plan = Plan().model_dump()
    run = {
        "id": "ui-contract-only", "session_id": "ui-contract-only", "scenario_id": "ui-contract-scenario",
        "scenario_revision": 1, "state": "Ended", "created_at": 1, "ended_at": 2,
        "plan": plan, "approved_plan": plan, "build": {"source_revision": "controlled contract fixture"},
        "players": {"player-1": {"phase": "ended", "launched": False, "teardown_confirmed": True}},
        "events": {}, "progress": {}, "model": MODEL, "controller_version": "contract-test",
        "evidence_status": "incomplete", "retained_files": [], "ending_reason": "infrastructure_failure",
        "validity": "invalid", "goal_complete": False, "demo_complete": False,
        "limitations": ["Controlled contract fixture: no game client or frames were recorded."],
    }
    app.state.store.put("runs", run)
    with TestClient(app) as client:
        evidence = client.get("/api/runs/ui-contract-only/evidence").json()
        assert evidence["files"] == []
        assert evidence["gaps"] == run["limitations"]
        assert client.get("/api/runs/ui-contract-only/players/player-1/frame").status_code == 503
        report = client.get("/api/runs/ui-contract-only/report").json()["markdown"]
        assert "infrastructure_failure" in report
        assert "no game client or frames were recorded" in report
        assert "Goal completion: **False**" in report
        detail = client.get("/api/runs/ui-contract-only").json()
        assert detail["event_states"] == {} and detail["events"] == []
        assert "scheduled_events" not in detail
        search = client.get("/api/runs/ui-contract-only/events?search=missing").json()
        assert search["events"] == [] and search["has_more"] is False
        assert client.get("/api/runs/ui-contract-only/files/%2e%2e%2fashad.sqlite3").status_code == 404


def test_dashboard_serves_local_assets_and_offers_chatgpt_sign_in(tmp_path):
    with TestClient(app_at(tmp_path)) as client:
        index = client.get("/")
        assert index.status_code == 200
        assert '/static/app.js' in index.text
        script = client.get("/static/app.js")
        assert script.status_code == 200
        assert "/api/settings/codex/login" in script.text
        assert "Sign in with ChatGPT" in script.text
        assert 'name="key"' not in script.text and "/api/settings/key" not in script.text
        assert "localStorage" not in script.text and "sessionStorage" not in script.text
        assert "X-ASHAD-Request" in script.text
        assert client.get("/static/style.css").status_code == 200
        assert "script-src 'self'" in index.headers["content-security-policy"]






def test_dashboard_chatgpt_auth_contract_requires_local_intent_without_model_calls(tmp_path):
    app = app_at(tmp_path)
    backend = app.state.ai.backend
    login = {"auth_url": "https://auth.openai.com/authorize?test=contract", "login_id": "test-login"}
    backend.login_start = AsyncMock(return_value=login)
    backend.login_cancel = AsyncMock()
    backend.logout = AsyncMock(side_effect=lambda: setattr(backend, "ready", False))
    headers = {"X-ASHAD-Request": "1"}
    with TestClient(app) as client:
        signed_out = client.get("/api/settings/codex/status").json()
        assert signed_out["auth_method"] == "chatgpt" and signed_out["configured"] is False
        assert client.post("/api/settings/codex/login", json={}).status_code == 403
        backend.login_start.assert_not_awaited()
        assert client.post("/api/settings/codex/login", json={}, headers=headers).json() == login
        backend.login_start.assert_awaited_once()
        assert client.post("/api/settings/codex/login/cancel", json={}, headers=headers).status_code == 200
        backend.login_cancel.assert_awaited_once()
        backend.ready = True
        signed_in = client.get("/api/settings/codex/status").json()
        assert signed_in["configured"] is True and signed_in["status"] == "ready"
        assert client.post("/api/settings/codex/logout", json={}).status_code == 403
        backend.logout.assert_not_awaited()
        assert client.post("/api/settings/codex/logout", json={}, headers=headers).json()["configured"] is False
        backend.logout.assert_awaited_once()
        assert client.get("/api/bootstrap").json()["ai"]["configured"] is False
        assert client.post("/api/settings/key", json={"key": "unused"}, headers=headers).status_code in {404, 405}
        assert backend.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["lifespan", "client_close"])
async def test_controller_closes_all_resources_on_shutdown_failure(tmp_path, failure):
    app = app_at(tmp_path)
    coordinator = app.state.coordinator
    first = AsyncMock()
    second = AsyncMock()
    if failure == "client_close":
        first.close.side_effect = OSError("close failed")
    coordinator.clients.update(first=first, second=second)
    pending = asyncio.create_task(asyncio.Event().wait())
    coordinator.tasks[("run", "player")] = pending
    try:
        with pytest.raises((RuntimeError, OSError, BaseExceptionGroup)):
            async with app.router.lifespan_context(app):
                if failure == "lifespan":
                    raise RuntimeError("lifespan failed")
        assert pending.done()
        assert coordinator.stopping
        first.close.assert_awaited_once()
        second.close.assert_awaited_once()
        with pytest.raises(sqlite3.ProgrammingError):
            app.state.store.list("runs")
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        app.state.store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["startup", "lifespan"])
async def test_worker_closes_on_startup_or_lifespan_failure(failure):
    from worker.app import create_app as create_worker_app
    service = AsyncMock()
    if failure == "startup":
        service.start.side_effect = RuntimeError("startup failed")
    app = create_worker_app(service, token="test-token" * 4)
    with pytest.raises(RuntimeError):
        async with app.router.lifespan_context(app):
            raise RuntimeError("lifespan failed")
    service.close.assert_awaited_once()


def test_controller_launcher_releases_lock_when_watchdog_cannot_start(tmp_path, monkeypatch):
    import io
    import ashad.__main__ as launcher
    import ashad.process_lock

    lock = io.BytesIO()
    monkeypatch.setenv("ASHAD_DATA", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["ashad"])
    monkeypatch.setattr(ashad.process_lock, "acquire", lambda path: lock)

    def fail_watchdog(*args, **kwargs):
        raise OSError("watchdog unavailable")

    monkeypatch.setattr(launcher.subprocess, "Popen", fail_watchdog)
    with pytest.raises(OSError, match="watchdog unavailable"):
        launcher.main()
    assert lock.closed
