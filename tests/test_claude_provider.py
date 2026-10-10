"""Provider metadata checks; no CLI, keyring, or model calls."""
import asyncio
import sys
from pathlib import Path

from ashad.ai import OpenAI
from ashad.claude_backend import ClaudeBackend
from ashad.store import Store
from test_controller import FakeCodexBackend, MemoryCredentials
from ashad.models import CLAUDE_MODELS, CODEX_MODELS, MODEL_OPTIONS, SUPPORTED_MODELS, Plan, provider_of


def test_provider_of_routes_claude_aliases_and_defaults_to_codex():
    for model in ("sonnet", "opus", "haiku"):
        assert provider_of(model) == "claude"
    assert set(CLAUDE_MODELS) == {"sonnet", "opus", "haiku"}
    for model in CODEX_MODELS:
        assert provider_of(model) == "codex"


def test_model_options_cover_supported_models_in_order():
    assert [option["id"] for option in MODEL_OPTIONS] == list(SUPPORTED_MODELS)


def test_plan_accepts_claude_alias():
    assert Plan(model="sonnet").model == "sonnet"


def test_redact_strips_claude_oauth_token():
    from ashad.store import redact
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
