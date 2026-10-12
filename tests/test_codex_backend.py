"""Managed Codex backend checks use injected SDK doubles; no account or model calls."""
import asyncio
from contextlib import asynccontextmanager, suppress
import json
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest

import ashad.codex_backend as module
from ashad.codex_backend import CodexBackend, CodexError
from ashad.models import MODEL, CODEX_MODELS, Plan
from ashad.store import Store
from test_controller import environment  # noqa: F401


QUESTIONS = [{"type": "choice", "name": "action", "instructions": "Select one observed action",
              "choices": [{"value": "wait", "description": "Wait"}, {"value": "move", "description": "Move"}]}]


class Handle:
    def __init__(self):
        self.entered = asyncio.Event()
        self.block = False
        self.error = None
        self.interrupt = AsyncMock()

    async def run(self):
        self.entered.set()
        if self.block:
            await asyncio.Event().wait()
        if self.error:
            raise self.error
        return Obj(id="turn-1", status="completed", final_response='{"action":"wait"}',
                   usage=Obj(last=Obj(model_dump=lambda: {"input_tokens": 3, "output_tokens": 1}),
                             total=Obj(model_dump=lambda: {"input_tokens": 12, "output_tokens": 4, "cached_input_tokens": 2, "cache_write_input_tokens": 1})))


class Client:
    def __init__(self, auth="chatgpt", models=(MODEL,)):
        self.account = AsyncMock(return_value=Obj(account=Obj(root=Obj(type=auth, plan_type="pro")) if auth else None))
        self.models = AsyncMock(return_value=Obj(data=[Obj(model=model) for model in models]))
        self.handle = Handle()
        self.thread = Obj(id="thread-1", turn=AsyncMock(return_value=self.handle))
        self.thread_start = AsyncMock(return_value=self.thread)
        self._client = Obj(request=AsyncMock())
        self.logout = AsyncMock()
        self.close = AsyncMock()
        self.login_chatgpt = AsyncMock()


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path)
    yield value
    value.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("auth,status", [(None, "configuration_required"), ("apiKey", "invalid_authentication")])
@pytest.mark.parametrize("operation", ["selection", "planning"])
async def test_only_chatgpt_auth_can_start_a_request(store, auth, status, operation):
    client = Client(auth=auth)
    backend = CodexBackend(store, client=client)
    result = await backend.status()
    assert result["status"] == status and result["configured"] is False
    with pytest.raises(CodexError) as caught:
        if operation == "planning":
            await backend.plan("game evidence", {})
        else:
            await backend.select("game evidence", QUESTIONS)
    assert caught.value.status == status
    client.models.assert_not_awaited()
    client.thread_start.assert_not_awaited()


@pytest.mark.asyncio
async def test_positive_status_is_reused_briefly_and_rechecked_after_auth_changes_or_errors(store, monkeypatch):
    client = Client()
    backend = CodexBackend(store, client=client)
    now = [1000.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    await backend.select("evidence", QUESTIONS)
    await backend.select("evidence", QUESTIONS)
    assert client.account.await_count == client.models.await_count == 1  # Not two account+models checks per turn.
    now[0] += module.READY_TTL_SECONDS
    await backend.select("evidence", QUESTIONS)
    assert client.account.await_count == 2
    await backend.logout()
    client.account.return_value = Obj(account=None)
    with pytest.raises(CodexError) as signed_out:
        await backend.select("evidence", QUESTIONS)
    assert signed_out.value.status == "configuration_required" and client.thread_start.await_count == 3
    client.account.return_value = Obj(account=Obj(root=Obj(type="chatgpt", plan_type="pro")))
    await backend.select("evidence", QUESTIONS)
    client.handle.error = RuntimeError("401 unauthorized")
    with pytest.raises(CodexError):
        await backend.select("evidence", QUESTIONS)
    checks = client.account.await_count
    client.handle.error = None
    await backend.select("evidence", QUESTIONS)
    assert client.account.await_count == checks + 1  # A provider failure forces a fresh check.


@pytest.mark.asyncio
async def test_missing_luna_model_has_no_substitution(store):
    client = Client(models=("another-model",))
    backend = CodexBackend(store, client=client)
    status = await backend.status()
    assert status["status"] == "model_unavailable" and status["configured"] is True
    assert MODEL in status["message"]
    with pytest.raises(CodexError) as caught:
        await backend.select("game evidence", QUESTIONS)
    assert caught.value.status == "model_unavailable"
    client.thread_start.assert_not_awaited()


@pytest.mark.asyncio
async def test_selection_uses_schema_and_deny_all_with_observed_choices(store):
    client = Client()
    backend = CodexBackend(store, client=client)
    result = await backend.select("visible game evidence", QUESTIONS)
    assert result == {"answers": {"action": "wait"}, "thread_id": "thread-1", "turn_id": "turn-1",
                      "usage": {"input_tokens": 12, "output_tokens": 4, "cached_input_tokens": 2, "cache_write_input_tokens": 1},
                      "usage_scope": "selection_total"}
    started = client.thread_start.call_args.kwargs
    assert started["model"] == MODEL == "gpt-6-luna"
    assert started["model_provider"] == "openai" and started["ephemeral"] is True
    assert started["approval_mode"] == module.ApprovalMode.deny_all
    assert started["sandbox"] == module.Sandbox.read_only
    assert started["cwd"] == str(backend.workspace)
    turn = client.thread.turn.call_args
    assert json.loads(turn.args[0]) == {"evidence": "visible game evidence", "questions": QUESTIONS}
    assert turn.kwargs["output_schema"] == {"type": "object", "properties": {"action": {"type": "string", "enum": ["wait", "move"]}},
                                           "required": ["action"], "additionalProperties": False}
    assert turn.kwargs["approval_mode"] == module.ApprovalMode.deny_all
    client._client.request.assert_awaited_once_with("thread/unsubscribe", {"threadId": "thread-1"},
        response_model=module.ThreadUnsubscribeResponse)


@pytest.mark.asyncio
@pytest.mark.parametrize("model", CODEX_MODELS)
async def test_planning_uses_contract_schema_grounding_and_same_no_tools_runtime(store, model):
    from ashad.subgoals import PlanningDecision
    from test_decisions import planning_answers
    client = Client(models=CODEX_MODELS)
    response = await client.handle.run()
    response.final_response = json.dumps(planning_answers()["answers"])
    client.handle.run = AsyncMock(return_value=response)
    backend = CodexBackend(store, client=client)
    schema = PlanningDecision.model_json_schema()
    result = await backend.plan("visible quest/recipe evidence", schema, model=model)
    assert result["answers"] == planning_answers()["answers"]
    assert result["usage_scope"] == "planning_total" and result["usage"]["input_tokens"] == 12
    started = client.thread_start.call_args.kwargs
    turn = client.thread.turn.call_args
    assert started["model"] == turn.kwargs["model"] == model
    assert started["model_provider"] == "openai" and started["ephemeral"] is True
    assert started["cwd"] == str(backend.workspace)
    assert started["approval_mode"] == turn.kwargs["approval_mode"] == module.ApprovalMode.deny_all
    assert started["sandbox"] == turn.kwargs["sandbox"] == module.Sandbox.read_only
    assert turn.kwargs["effort"] == module.ReasoningEffort.medium
    assert turn.kwargs["output_schema"] == schema
    assert json.loads(turn.args[0]) == {"evidence": "visible quest/recipe evidence"}
    assert "tools" not in started and "tools" not in turn.kwargs
    prompt = started["base_instructions"]
    for rule in ("Never use tools, read files, browse", "privileged telemetry", "Backward-chain", "visible quest and recipe data",
                 "Reuse existing inventory", "executable leaf", "Never supply completed or status", "exact quote",
                 "JSON-pointer", "cumulative harvested counts", "partial checklist", "criterion stays unknown",
                 "empty parent_key", "Preserve every existing known completion condition", "exact field/value matches"):
        assert rule in prompt
    assert len(prompt.encode("utf-8")) < 4608
    client._client.request.assert_awaited_once_with("thread/unsubscribe", {"threadId": "thread-1"},
        response_model=module.ThreadUnsubscribeResponse)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["selection", "planning"])
async def test_cancelling_request_interrupts_active_sdk_turn(store, operation):
    client = Client()
    client.handle.block = True
    backend = CodexBackend(store, client=client)
    request = backend.plan("game evidence", {}) if operation == "planning" else backend.select("game evidence", QUESTIONS)
    task = asyncio.create_task(request)
    await client.handle.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    client.handle.interrupt.assert_awaited_once()
    client._client.request.assert_awaited_once_with("thread/unsubscribe", {"threadId": "thread-1"},
        response_model=module.ThreadUnsubscribeResponse)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["selection", "planning"])
async def test_cancelling_during_turn_start_still_interrupts_returned_handle(store, operation):
    client = Client()
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed_turn(*args, **kwargs):
        entered.set()
        await release.wait()
        return client.handle
    client.thread.turn.side_effect = delayed_turn
    backend = CodexBackend(store, client=client)
    request = backend.plan("game evidence", {}) if operation == "planning" else backend.select("game evidence", QUESTIONS)
    task = asyncio.create_task(request)
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    client.handle.interrupt.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["selection", "planning"])
async def test_timeout_interrupts_turn_and_has_connection_status(store, monkeypatch, operation):
    timeout = asyncio.timeout
    @asynccontextmanager
    async def shortened(seconds):
        async with timeout(0.02):
            yield
    monkeypatch.setattr(module.asyncio, "timeout", shortened)
    client = Client()
    client.handle.block = True
    backend = CodexBackend(store, client=client)
    with pytest.raises(CodexError) as caught:
        if operation == "planning":
            await backend.plan("game evidence", {})
        else:
            await backend.select("game evidence", QUESTIONS)
    assert caught.value.status == "connection_failure" and "timed out" in caught.value.message
    client.handle.interrupt.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("message,status", [("Usage limit reached", "rate_limited"), ("Model gpt-6-luna not available", "model_unavailable")])
async def test_sdk_failed_turn_exception_preserves_provider_error(store, message, status):
    # SDK handle.run raises RuntimeError(turn.error.message), not a failed TurnResult.
    client = Client()
    client.handle.error = RuntimeError(message)
    backend = CodexBackend(store, client=client)
    with pytest.raises(CodexError) as caught:
        await backend.select("game evidence", QUESTIONS)
    assert caught.value.status == status
    client.handle.interrupt.assert_not_awaited()
    client._client.request.assert_awaited_once_with("thread/unsubscribe", {"threadId": "thread-1"},
        response_model=module.ThreadUnsubscribeResponse)


def test_runtime_uses_private_home_and_disables_tools_and_api_keys(store, monkeypatch):
    captured = []
    client = object()
    def create(config):
        captured.append(config)
        return client
    monkeypatch.setattr(module, "AsyncCodex", create)
    backend = CodexBackend(store)
    assert backend.runtime() is client and backend.runtime() is client
    [config] = captured
    assert backend.workspace.is_dir() and backend.workspace.is_relative_to(store.root)
    assert config.cwd == str(backend.workspace)
    assert config.env["CODEX_HOME"] == str(store.root / "codex")
    assert config.env["OPENAI_API_KEY"] == config.env["CODEX_API_KEY"] == config.env["OPENAI_BASE_URL"] == ""
    overrides = set(config.config_overrides)
    assert {'forced_login_method="chatgpt"', 'cli_auth_credentials_store="keyring"',
            'features.shell_tool=false', 'features.unified_exec=false', 'features.apps=false',
            'features.multi_agent=false', 'web_search="disabled"', 'tools.view_image=false'} <= overrides


@pytest.mark.asyncio
async def test_login_completion_failure_is_visible_without_inference(store):
    client = Client(auth=None)
    finished = asyncio.Event()
    async def wait():
        await finished.wait()
        return Obj(success=False)
    handle = Obj(auth_url="https://auth.openai.com/test", login_id="login-1", wait=wait, cancel=AsyncMock())
    client.login_chatgpt.return_value = handle
    backend = CodexBackend(store, client=client)
    assert await backend.login_start() == {"auth_url": handle.auth_url, "login_id": "login-1"}
    assert await backend.login_start() == {"auth_url": handle.auth_url, "login_id": "login-1"}
    client.login_chatgpt.assert_awaited_once()
    assert (await backend.status())["status"] == "login_pending"
    finished.set()
    await backend.login_wait
    assert (await backend.status())["status"] == "invalid_authentication"
    client.thread_start.assert_not_awaited()
    await backend.close()
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_login_start_errors_are_translated(store):
    client = Client(auth=None)
    client.login_chatgpt.side_effect = RuntimeError("authentication failed")
    backend = CodexBackend(store, client=client)
    with pytest.raises(CodexError) as caught:
        await backend.login_start()
    assert caught.value.status == "invalid_authentication"
    assert backend.login_wait is None


@pytest.mark.asyncio
async def test_login_cancel_failure_cleans_handle_and_close_still_closes_client(store):
    client = Client(auth=None)
    async def wait():
        await asyncio.Event().wait()
    handle = Obj(auth_url="https://auth.openai.com/test", login_id="login-1", wait=wait,
                 cancel=AsyncMock(side_effect=RuntimeError("authentication cancellation failed")))
    client.login_chatgpt.return_value = handle
    backend = CodexBackend(store, client=client)
    await backend.login_start()
    with pytest.raises(CodexError):
        await backend.login_cancel()
    assert backend.login is None and backend.login_wait is None
    await backend.login_start()
    with suppress(CodexError):
        await backend.close()
    client.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["selection", "planning"])
async def test_cancelling_before_thread_start_response_closes_sdk_runtime(store, operation):
    client = Client()
    entered = asyncio.Event()
    async def blocked_start(**kwargs):
        entered.set()
        await asyncio.Event().wait()
    client.thread_start.side_effect = blocked_start
    backend = CodexBackend(store, client=client)
    request = backend.plan("game evidence", {}) if operation == "planning" else backend.select("game evidence", QUESTIONS)
    task = asyncio.create_task(request)
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    client.close.assert_awaited_once()
    assert backend.client is None


@pytest.mark.asyncio
async def test_unsubscribe_failure_closes_ephemeral_runtime(store):
    client = Client()
    client._client.request.side_effect = RuntimeError("unsubscribe unavailable")
    backend = CodexBackend(store, client=client)
    result = await backend.select("game evidence", QUESTIONS)
    assert result["answers"] == {"action": "wait"}
    client.close.assert_awaited_once()
    assert backend.client is None


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_mask_original_provider_error(store):
    client = Client()
    client.handle.error = RuntimeError("Usage limit reached")
    client._client.request.side_effect = RuntimeError("unsubscribe unavailable")
    client.close.side_effect = RuntimeError("SDK close failed")
    backend = CodexBackend(store, client=client)
    with pytest.raises(CodexError) as caught:
        await backend.select("game evidence", QUESTIONS)
    assert caught.value.status == "rate_limited"

@pytest.mark.asyncio
async def test_pending_reauthentication_blocks_selections_with_existing_account(store):
    client = Client()
    async def wait():
        await asyncio.Event().wait()
    handle = Obj(auth_url="https://auth.openai.com/test", login_id="login-1", wait=wait, cancel=AsyncMock())
    client.login_chatgpt.return_value = handle
    backend = CodexBackend(store, client=client)
    try:
        await backend.login_start()
        assert (await backend.status())["status"] == "login_pending"
        with pytest.raises(CodexError) as caught:
            await backend.select("game evidence", QUESTIONS)
        assert caught.value.status == "login_pending"
        client.thread_start.assert_not_awaited()
    finally:
        await backend.close()

@pytest.mark.parametrize("route", ["login", "logout"])
def test_queued_run_blocks_codex_account_changes(tmp_path, route):
    from fastapi.testclient import TestClient
    from ashad.app import create_app
    from test_controller import FakeCodexBackend, MemoryCredentials
    backend = FakeCodexBackend()
    backend.login_start = AsyncMock()
    backend.logout = AsyncMock()
    app = create_app(tmp_path, MemoryCredentials(None), start_services=False, ai_backend=backend)
    app.state.store.put("runs", {"id": "queued-run", "state": "Queued"})
    with TestClient(app) as client:
        result = client.post(f"/api/settings/codex/{route}", json={}, headers={"X-ASHAD-Request": "1"})
        assert result.status_code == 422 and "queued" in result.json()["detail"].lower()
    backend.login_start.assert_not_awaited()
    backend.logout.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_while_signed_out_keeps_inputs_held_and_history_unchanged(environment):
    from test_acceptance_edges import ready_run
    store, coordinator, _, desktops = environment
    run = await ready_run(environment)
    await coordinator.control(run["id"], "pause", command_id="pause-for-auth")
    before = store.require("runs", run["id"])
    coordinator.ai.backend.ready = False
    calls_before = len(desktops["worker-1"].calls)
    with pytest.raises(ValueError, match="Sign in"):
        await coordinator.control(run["id"], "resume", command_id="resume-signed-out")
    after = store.require("runs", run["id"])
    assert after["state"] == "Paused" and after["interventions"] == before["interventions"]
    assert after["players"]["player-1"]["generation"] == before["players"]["player-1"]["generation"]
    assert not desktops["worker-1"].calls[calls_before:]


@pytest.mark.asyncio
@pytest.mark.parametrize("model", CODEX_MODELS)
async def test_selected_model_is_checked_and_sent_to_both_sdk_stages(store, model):
    client = Client(models=(*CODEX_MODELS, "unrelated-model"))
    backend = CodexBackend(store, client=client)
    status = await backend.status(model)
    assert status["status"] == "ready" and status["model"] == model
    assert status["available_models"] == list(CODEX_MODELS)
    await backend.select("game evidence", QUESTIONS, model=model)
    assert client.thread_start.call_args.kwargs["model"] == model
    assert client.thread.turn.call_args.kwargs["model"] == model


@pytest.mark.asyncio
async def test_sol_available_without_luna_can_be_selected(store):
    client = Client(models=("gpt-6-sol",))
    backend = CodexBackend(store, client=client)
    assert (await backend.status())["status"] == "model_unavailable"
    assert (await backend.status("gpt-6-sol"))["status"] == "ready"
    await backend.select("game evidence", QUESTIONS, model="gpt-6-sol")
    assert client.thread_start.call_args.kwargs["model"] == "gpt-6-sol"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["selection", "planning"])
async def test_completed_invalid_json_keeps_total_usage(store, operation):
    client = Client()
    result = await client.handle.run()
    result.final_response = "not JSON"
    client.handle.run = AsyncMock(return_value=result)
    backend = CodexBackend(store, client=client)
    with pytest.raises(CodexError) as caught:
        if operation == "planning":
            await backend.plan("game evidence", {})
        else:
            await backend.select("game evidence", QUESTIONS)
    assert caught.value.status == ("invalid_plan" if operation == "planning" else "invalid_decision")
    assert caught.value.selection["usage"]["input_tokens"] == 12
    assert caught.value.selection["usage"]["output_tokens"] == 4
    assert caught.value.selection["usage_scope"] == operation + "_total"


def test_plan_defaults_to_luna_and_rejects_unknown_models():
    assert Plan().model == "gpt-6-luna"
    for model in CODEX_MODELS:
        assert Plan(model=model).model == model
    with pytest.raises(ValueError, match="supported model"):
        Plan(model="unapproved-model")


@pytest.mark.asyncio
async def test_run_freezes_selected_model_and_resume_rechecks_it(environment):
    from test_acceptance_edges import ready_run
    from ashad.validation import validate_plan
    store, coordinator, plan, _ = environment
    plan["model"] = "gpt-6-sol"
    run = await ready_run(environment, plan)
    assert run["model"] == run["plan"]["model"] == run["approved_plan"]["model"] == "gpt-6-sol"
    # Another settings/instance check must not change this run's chosen model.
    await coordinator.ai.validate("gpt-6-luna")
    assert validate_plan(run["plan"], store, check_credentials=True)["valid"]
    await coordinator.control(run["id"], "pause", command_id="pause-sol")
    await coordinator.control(run["id"], "resume", command_id="resume-sol")
    assert store.get("settings", "ai_status")["model"] == "gpt-6-sol"
    assert store.require("runs", run["id"])["model"] == "gpt-6-sol"


def test_plan_validation_checks_account_availability_not_last_selected_model(environment):
    from ashad.validation import validate_plan
    store, _, plan, _ = environment
    plan["model"] = "gpt-6-sol"
    store.put("settings", {"configured": True, "status": "model_unavailable", "model": "gpt-6-luna",
        "api": "codex", "auth_method": "chatgpt", "available_models": ["gpt-6-sol"]}, "ai_status")
    assert validate_plan(plan, store, check_credentials=True)["valid"]
    plan["model"] = "gpt-6-astra"
    assert not validate_plan(plan, store, check_credentials=True)["valid"]
