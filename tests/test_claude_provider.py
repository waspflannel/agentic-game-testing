"""Claude provider routing, token routes and launch checks against fakes; no real CLI, keyring, or model calls."""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from ashad.ai import OpenAI
from ashad.app import create_app
from ashad.claude_backend import ClaudeBackend
from ashad.models import CLAUDE_MODELS, CODEX_MODELS, SUPPORTED_MODELS, Plan, provider_of
from ashad.store import Store, redact
from ashad.validation import validate_plan
from test_controller import FakeClaudeBackend, FakeCodexBackend, MemoryCredentials, environment  # noqa: F401


def test_provider_of_routes_claude_aliases_and_defaults_to_codex():
    for model in ("sonnet", "opus", "haiku"):
        assert provider_of(model) == "claude"
    assert set(CLAUDE_MODELS) == {"sonnet", "opus", "haiku"}
    for model in CODEX_MODELS:
        assert provider_of(model) == "codex"


def test_plan_accepts_claude_alias():
    assert Plan(model="sonnet").model == "sonnet"


def test_redact_strips_claude_oauth_token():
    assert "sk-ant-oat01" not in redact("x sk-ant-oat01-" + "a" * 40)
    assert redact({"oauth_token": "v"})["oauth_token"] == "[REDACTED]"
    assert redact({"claude_code_oauth_token": "v"})["claude_code_oauth_token"] == "[REDACTED]"


FAKE = str(Path(__file__).parent / "fixtures" / "fake_claude.py")
QUESTIONS = [{"type": "choice", "name": "action", "choices": [{"value": "a"}, {"value": "b"}]}]


def routed_ai(tmp_path):
    store = Store(tmp_path)
    creds = MemoryCredentials(None)
    claude = ClaudeBackend(store, creds, binary=[sys.executable, FAKE, "--scenario=choice"])
    claude.save_token("sk-ant-oat01-" + "t" * 40)
    codex = FakeCodexBackend()
    return store, codex, claude, OpenAI(store, creds, backend=codex, claude_backend=claude)


def test_claude_selection_through_fake_cli_returns_answers_shape(tmp_path):
    store, codex, claude, ai = routed_ai(tmp_path)
    result = asyncio.run(ai.decisions("evidence", QUESTIONS, model="sonnet"))
    assert result == {"action": {"choice": "a"}}
    assert codex.requests == []
    assert [u["api"] for u in store.list("usage")] == ["claude"]
    assert store.list("provider_requests")[0]["auth_method"] == "claude_oauth_token"


def test_codex_model_never_reaches_claude_backend(tmp_path):
    store, codex, claude, ai = routed_ai(tmp_path)

    async def forbidden(*args, **kwargs):
        raise AssertionError("Claude backend must not see Codex models")
    claude.select = forbidden
    asyncio.run(ai.decisions("evidence", QUESTIONS, model="gpt-6-luna"))
    assert [r["model"] for r in codex.requests] == ["gpt-6-luna"]
    assert [u["api"] for u in store.list("usage")] == ["codex"]


def test_validate_keeps_statuses_separate(tmp_path):
    store, codex, claude, ai = routed_ai(tmp_path)
    before = dict(ai.status)
    status = asyncio.run(ai.validate("sonnet"))
    assert ai.status == before
    assert ai.claude_status is status and status["api"] == "claude"
    assert store.get("settings", "ai_status_claude")["api"] == "claude"
    assert store.get("settings", "ai_status", None) is None


HEADERS = {"X-ASHAD-Request": "1"}
TOKEN = "sk-ant-oat01-" + "z" * 40


def claude_app(tmp_path, claude=None, codex=None):
    claude = claude or FakeClaudeBackend(ready=False)
    app = create_app(tmp_path, MemoryCredentials(None), start_services=False,
                     ai_backend=codex or FakeCodexBackend(), claude_backend=claude)
    return app, claude


def test_token_post_stores_and_never_echoes(tmp_path):
    app, claude = claude_app(tmp_path)
    with TestClient(app) as client:
        result = client.post("/api/settings/claude/token", json={"token": f"  {TOKEN}  "}, headers=HEADERS)
        assert result.status_code == 200 and result.json()["status"] == "ready"
        assert TOKEN not in result.text
        assert TOKEN not in client.get("/api/bootstrap").text
        row = app.state.store.get("settings", "ai_status_claude")
        assert row and TOKEN not in str(row)
    assert claude.saved == [TOKEN]


@pytest.mark.parametrize("token", ["", "abc", "sk-ant-oat x y"])
def test_token_post_rejects_bad_tokens(tmp_path, token):
    app, claude = claude_app(tmp_path)
    with TestClient(app) as client:
        result = client.post("/api/settings/claude/token", json={"token": token}, headers=HEADERS)
        assert result.status_code == 422
    assert claude.saved == []


def test_token_delete_clears(tmp_path):
    app, claude = claude_app(tmp_path, FakeClaudeBackend(ready=True))
    with TestClient(app) as client:
        result = client.delete("/api/settings/claude/token", headers=HEADERS)
        assert result.status_code == 200
    assert claude.cleared == 1


@pytest.mark.parametrize("method", ["post", "delete"])
def test_token_routes_blocked_while_run_active(tmp_path, method):
    app, claude = claude_app(tmp_path)
    app.state.store.put("runs", {"id": "queued-run", "state": "Queued"})
    with TestClient(app) as client:
        kwargs = {"json": {"token": TOKEN}} if method == "post" else {}
        result = getattr(client, method)("/api/settings/claude/token", headers=HEADERS, **kwargs)
        assert result.status_code == 422 and "queued" in result.json()["detail"].lower()
    assert claude.saved == [] and claude.cleared == 0


def test_bootstrap_lists_claude_options_and_status(tmp_path):
    claude = FakeClaudeBackend()
    claude.status = AsyncMock(side_effect=AssertionError("bootstrap must not call the backend"))
    app, _ = claude_app(tmp_path, claude)
    with TestClient(app) as client:
        data = client.get("/api/bootstrap").json()
    sonnet = next(o for o in data["model_options"] if o["id"] == "sonnet")
    assert sonnet["provider"] == "claude"
    assert "ai_claude" in data


def test_launch_validation_checks_selected_provider(environment):
    store, _, plan, _ = environment
    ready = lambda api, auth: {"configured": True, "status": "ready", "model": "x", "api": api,
                               "auth_method": auth, "available_models": list(SUPPORTED_MODELS)}
    codex_ready = ready("codex", "chatgpt")
    claude_ready = {**ready("claude", "claude_oauth_token"), "available_models": list(CLAUDE_MODELS)}
    not_ready = {"configured": False, "status": "configuration_required", "message": "Connect."}
    for codex, claude, good, bad in ((not_ready, claude_ready, "sonnet", "gpt-6-luna"),
                                     (codex_ready, not_ready, "gpt-6-luna", "sonnet")):
        store.put("settings", codex, "ai_status")
        store.put("settings", claude, "ai_status_claude")
        assert validate_plan({**plan, "model": good}, store, check_credentials=True)["valid"]
        assert not validate_plan({**plan, "model": bad}, store, check_credentials=True)["valid"]


@pytest.mark.asyncio
async def test_sonnet_profile_run_records_claude_api(environment):
    from test_acceptance_edges import ready_run
    store, coordinator, plan, _ = environment
    coordinator.ai.claude_backend = FakeClaudeBackend()
    plan["model"] = "sonnet"
    run = await ready_run(environment, plan)
    assert run["api"] == "claude" and run["billing"] == "subscription"
    assert run["model"] == "sonnet"
    await coordinator.control(run["id"], "pause", command_id="pause-sonnet")
    await coordinator.control(run["id"], "model", command_id="model-opus", model="opus")
    await coordinator.control(run["id"], "resume", command_id="resume-sonnet")
    assert store.get("settings", "ai_status_claude")["model"] == "opus"
    changed = store.require("runs", run["id"])
    assert changed["model"] == "opus" and changed["api"] == "claude"
    await coordinator.ai.decisions("evidence", QUESTIONS, run_id=run["id"])
    assert coordinator.ai.claude_backend.requests[-1]["model"] == "opus"
