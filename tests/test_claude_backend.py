import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

from ashad import claude_backend
from ashad.claude_backend import ClaudeBackend, ClaudeError, SYSTEM_SUFFIX
from ashad.prompts import selection_request
from ashad.store import Store
from test_controller import MemoryCredentials

FAKE = str(Path(__file__).parent / "fixtures" / "fake_claude.py")
TOKEN = "sk-ant-oat01-" + "t" * 40
QUESTIONS = [{"name": "action", "choices": [{"value": "a"}, {"value": "b"}]}]
EVIDENCE = {"note": "secret-evidence-text"}
SCHEMA = {"type": "object"}


@pytest.fixture
def backend(tmp_path, monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_HOME", "CLAUDE_OAUTH_TEST_SECRET"):
        monkeypatch.setenv(name, "decoy-" + name)
    store = Store(tmp_path)
    claude = ClaudeBackend(store, MemoryCredentials(None), binary=[sys.executable, FAKE])
    claude.save_token(TOKEN)
    return claude


def use_scenario(backend, name):
    # An environment variable would be scrubbed by the backend's allowlist, so the fake takes it as an argument.
    backend.binary = [sys.executable, FAKE, f"--scenario={name}"]
    backend.version = None


def invocation(backend):
    return json.loads((backend.home / "invocation.json").read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_request_uses_every_hardening_flag_and_no_bare(backend):
    await backend.select(EVIDENCE, QUESTIONS, model="haiku")
    _, schema, instructions = selection_request(EVIDENCE, QUESTIONS)
    assert invocation(backend)["argv"] == ["-p", "--output-format", "json", "--model", "haiku",
        "--effort", "low", "--system-prompt", instructions + SYSTEM_SUFFIX, "--json-schema", json.dumps(schema),
        "--tools", "", "--strict-mcp-config", "--setting-sources", "", "--disable-slash-commands",
        "--no-session-persistence", "--permission-prompts", "none"]
    assert "--bare" not in invocation(backend)["argv"]
    await backend.plan(EVIDENCE, SCHEMA)
    argv = invocation(backend)["argv"]
    assert argv[argv.index("--effort") + 1] == "medium"
    await backend.tune(EVIDENCE)
    argv = invocation(backend)["argv"]
    assert argv[argv.index("--effort") + 1] == "medium"
    assert "--bare" not in argv


@pytest.mark.asyncio
async def test_environment_is_allowlist_plus_token(backend):
    await backend.select(EVIDENCE, QUESTIONS)
    recorded = invocation(backend)
    env = recorded["env"]
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert env["CLAUDE_CODE_DISABLE_FAST_MODE"] == "1"
    for name in ("HOME", "USERPROFILE", "CLAUDE_CONFIG_DIR"):
        assert env[name] == str(backend.home)
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_HOME", "CLAUDE_OAUTH_TEST_SECRET"):
        assert env[name] is None
    assert Path(recorded["cwd"]).resolve() == backend.home.resolve()


@pytest.mark.asyncio
async def test_stdin_carries_payload_not_argv(backend):
    await backend.select(EVIDENCE, QUESTIONS)
    recorded = invocation(backend)
    payload, _, _ = selection_request(EVIDENCE, QUESTIONS)
    assert json.loads(recorded["stdin"]) == payload
    assert not any("secret-evidence-text" in item for item in recorded["argv"])


@pytest.mark.asyncio
async def test_success_returns_codex_shape(backend):
    result = await backend.select(EVIDENCE, QUESTIONS)
    assert set(result) == {"answers", "usage", "usage_scope", "thread_id", "turn_id"}
    assert result["answers"] == {"answer": "ok"}


@pytest.mark.parametrize("name,status", [
    ("unauthorized", "invalid_authentication"), ("rate-limited", "rate_limited"),
    ("server-error", "provider_failure"), ("crash", "provider_failure"), ("nonzero-exit", "provider_failure"),
    ("list-output", "invalid_decision"), ("missing-output", "invalid_decision"),
    ("list-output", "invalid_plan"), ("missing-output", "invalid_plan")])
@pytest.mark.asyncio
async def test_failures_map_to_status(backend, name, status):
    use_scenario(backend, name)
    with pytest.raises(ClaudeError) as caught:
        if status == "invalid_plan":
            await backend.plan(EVIDENCE, SCHEMA)
        else:
            await backend.select(EVIDENCE, QUESTIONS)
    assert caught.value.status == status
    assert "raw-provider-secret" not in caught.value.message
    assert TOKEN not in caught.value.message


@pytest.mark.asyncio
async def test_oversized_output_is_rejected(backend, monkeypatch):
    use_scenario(backend, "oversized")
    with pytest.raises(ClaudeError) as caught:
        await backend.select(EVIDENCE, QUESTIONS)
    assert caught.value.status == "provider_failure"


@pytest.mark.asyncio
async def test_hang_times_out_and_kills_process(backend, monkeypatch):
    use_scenario(backend, "hang")
    monkeypatch.setattr(claude_backend, "REQUEST_TIMEOUT_SECONDS", 1)
    started = time.monotonic()
    with pytest.raises(ClaudeError) as caught:
        await backend.select(EVIDENCE, QUESTIONS)
    assert caught.value.status == "connection_failure"
    assert time.monotonic() - started < 5


@pytest.mark.asyncio
async def test_cancel_kills_process(backend, monkeypatch):
    use_scenario(backend, "hang")
    task = asyncio.create_task(backend.select(EVIDENCE, QUESTIONS))
    await asyncio.sleep(0.5)
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - started < 5


@pytest.mark.asyncio
async def test_status_configuration_required_without_token_binary_or_new_version(backend):
    backend.clear_token()
    assert (await backend.status())["status"] == "configuration_required"
    backend.save_token(TOKEN)
    assert (await backend.status())["status"] == "ready"

    missing = ClaudeBackend(Store(backend.home.parent / "other"), backend.credentials)
    assert (await missing.status())["status"] == "configuration_required"

    old = ClaudeBackend(Store(backend.home.parent / "old"), backend.credentials)
    use_scenario(old, "old-version")
    assert (await old.status())["status"] == "configuration_required"


@pytest.mark.asyncio
async def test_unauthorized_flips_status_until_new_token(backend, monkeypatch):
    use_scenario(backend, "unauthorized")
    with pytest.raises(ClaudeError):
        await backend.select(EVIDENCE, QUESTIONS)
    assert (await backend.status())["status"] == "invalid_authentication"
    backend.save_token(TOKEN)
    assert (await backend.status())["status"] == "ready"


@pytest.mark.asyncio
async def test_status_makes_no_model_call(backend):
    assert (await backend.status())["status"] == "ready"
    assert not (backend.home / "invocation.json").exists()
