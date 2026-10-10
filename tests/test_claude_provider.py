"""Provider metadata checks; no CLI, keyring, or model calls."""
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
