"""Codex planning and selection contracts use local backends, never authentication or inference."""
import asyncio
import inspect
import json
import math
from unittest.mock import AsyncMock

import httpx
import pytest

from ashad.ai import SUBSCRIPTION_PRICING, OpenAI, ProviderError, action_groups, choice
from ashad.codex_backend import CodexError
from ashad.models import MODEL, SUPPORTED_MODELS, Plan
from ashad.store import Store
from ashad.structured import prompt_observation


class Credentials:
    def get(self):
        raise AssertionError("Subscription selection must not read API credentials")


def answers(payload, selected=None, usage=True):
    return {"answers": {question["name"]: (selected or {}).get(question["name"], question["choices"][0]["value"])
                        for question in payload["questions"]},
            "thread_id": "thread-test", "turn_id": "turn-test", "usage_scope": "selection_total",
            "usage": {"input_tokens": 1000, "output_tokens": 12} if usage else None}


def planning_answers():
    return {"answers": {"goal_id": "root", "mode": "act",
            "completion": {"kind": "unknown", "description": "Discover a verifiable condition.",
                           "target": "", "quantity": 1, "collection": "", "matches": []},
            "evidence": [{"source": "observation", "path": "/entities/0/label", "quote": "Play"}],
            "reason": "Use observed menu controls to reach the world.", "subgoals": []},
            "thread_id": "planning-thread", "turn_id": "planning-turn", "usage_scope": "planning_total",
            "usage": {"input_tokens": 320, "output_tokens": 80}}


class Backend:
    def __init__(self, respond=answers):
        self.respond, self.calls = respond, []
        self.closed = False

    async def status(self, model=MODEL):
        return {"configured": True, "status": "ready", "api": "codex", "auth_method": "chatgpt",
                "model": model, "available_models": list(SUPPORTED_MODELS), "message": "Codex ChatGPT account connected"}

    async def select(self, input, questions, *, model=MODEL):
        payload = {"model": model, "input": input, "questions": questions}
        self.calls.append(payload)
        result = self.respond(payload)
        return await result if inspect.isawaitable(result) else result

    async def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def no_http_inference(monkeypatch):
    async def unexpected(*args, **kwargs):
        raise AssertionError("Provider tests must not make HTTP requests")
    monkeypatch.setattr(httpx.AsyncClient, "request", unexpected)


def context():
    return {"player": {"id": "p1", "name": "Ash", "context_policy": "repository"}, "world_namespace": "test-world",
            "transport": {"host_address": "worker-1", "port": 7800}, "goal": "Complete the visible tutorial",
            "observation": {"observation_id": "obs-1", "inputs": ["use_item", "inventory", "slot_1"],
                            "state": {"player": {"position": [0, 0], "closest_target": "crop"}, "tiles": [{"position": [32, 32], "ground": True}]},
                            "entities": [{"id": "play", "version": 4, "label": "Play", "actions": ["ui_click"], "state": {}},
                                         {"id": "field", "version": 2, "label": "World name", "actions": ["ui_text"]},
                                         {"id": "crop", "version": 7, "label": "Crop", "actions": ["move_to", "interact"], "position": [20, 20]},
                                         {"id": "far", "version": 8, "label": "Far tree", "actions": ["move_to", "interact"], "position": [1000, 0]},
                                         {"id": "info", "version": 1, "label": "Finish character creation first", "actions": []}]}}


def test_candidates_bind_observed_versions_and_bounded_arguments():
    groups = action_groups(context())
    actions = [a for g in groups for a in g["actions"]]
    ui = next(a for a in actions if a["args"].get("verb") == "ui_click")
    assert ui["args"]["target_id"] == "play" and ui["args"]["target_version"] == 4
    interactions = [a["args"]["target_id"] for a in actions if a["args"].get("verb") == "interact"]
    assert interactions == ["crop"]
    waypoint = next(a for a in actions if a["args"].get("parameters", {}).get("position") == [400.0, 0.0])
    assert "target_id" not in waypoint["args"]
    assert math.dist([0, 0], waypoint["args"]["parameters"]["position"]) <= 480
    text_values = {a["args"]["parameters"]["text"] for a in actions if a["args"].get("verb") == "ui_text"}
    assert text_values == {"Ash", "test-world", "worker-1", "7800"}
    assert not any(a["args"].get("parameters", {}).get("name") == "debug_grant" for a in actions)


def test_text_candidates_use_observed_field_limits_identity_and_skip_matching_text():
    supplied = context()
    field = supplied["observation"]["entities"][1]
    field.update(label="Ash", state={"control_type": "LineEdit", "node_name": "NameInput", "text": "Ash",
                                     "focused": True, "max_length": 10})
    supplied["player"]["identity"] = "test-world-that-would-be-truncated"
    groups = action_groups(supplied)
    group = next(g for g in groups if g.get("evidence", {}).get("id") == "field")
    assert "NameInput" in group["description"] and "focused" in group["description"]
    text_actions = [a for a in group["actions"] if a["args"].get("verb") == "ui_text"]
    values = [a["args"]["parameters"]["text"] for a in text_actions]
    assert "Ash" not in values and len(values) == len(set(values))
    assert all(len(value) <= 10 for value in values)
    assert "test-world" in values
    assert all(a["args"]["target_id"] == "field" and a["args"]["target_version"] == 2 for a in text_actions)
    assert all("does not click Confirm" in a["description"] for a in text_actions)


@pytest.mark.asyncio
async def test_validation_checks_account_without_inference_or_api_key(tmp_path):
    store, backend = Store(tmp_path), Backend()
    ai = OpenAI(store, Credentials(), backend=backend)
    assert (await ai.validate())["status"] == "ready"
    assert store.get("settings", "ai_status")["auth_method"] == "chatgpt"
    assert not backend.calls and not store.list("usage")
    await ai.close()
    assert backend.closed
    store.close()


@pytest.mark.parametrize("damage", ["unknown_choice", "missing", "extra", "wrong_type", "not_object"])
@pytest.mark.asyncio
async def test_invalid_provider_answers_never_dispatch(tmp_path, damage):
    store = Store(tmp_path)
    def respond(payload):
        result = answers(payload)
        name = payload["questions"][0]["name"]
        if damage == "unknown_choice":
            result["answers"][name] = "perform_unobserved_admin_action"
        elif damage == "missing":
            result["answers"] = {}
        elif damage == "extra":
            result["answers"]["unrequested"] = "0"
        elif damage == "wrong_type":
            result["answers"][name] = 0
        elif damage == "not_object":
            result["answers"] = []
        return result
    ai = OpenAI(store, Credentials(), backend=Backend(respond))
    calls = []
    async def dispatch(name, args):
        calls.append((name, args))
    with pytest.raises(ProviderError) as failed:
        await ai.player_turn(context(), dispatch, lambda: True, None)
    assert failed.value.status == "invalid_decision" and not calls
    assert store.list("usage")[0]["status"] == "invalid_decision"
    store.close()


@pytest.mark.asyncio
async def test_selected_action_and_subscription_usage_are_saved_without_confidence(tmp_path):
    store = Store(tmp_path)
    store.put("runs", {"id": "run-1", "plan": Plan().model_dump()})
    def respond(payload):
        assert "Finish character creation first" in payload["input"]
        return answers(payload)
    backend = Backend(respond)
    ai = OpenAI(store, Credentials(), backend=backend)
    calls = []
    async def dispatch(name, args):
        calls.append((name, args))
        return {"status": "running"}
    result = await ai.player_turn(context(), dispatch, lambda: True, "run-1")
    assert result["status"] == "dispatched" and len(calls) == 1
    assert calls[0][0] == "perform_action"
    assert calls[0][1]["target_id"] == "play" and calls[0][1]["target_version"] == 4
    assert "confidence" not in result and "probabilities" not in result
    assert store.events("run-1")[-1]["kind"] == "decision_selected"
    usage = store.list("usage")[0]
    assert usage["billing"] == "subscription" and usage["estimated_cost_usd"] is None
    assert usage["usage"] == {"input_tokens": 1000, "output_tokens": 12}
    assert usage["pricing_basis"] == SUBSCRIPTION_PRICING
    assert usage["api"] == "codex" and usage["model"] == "gpt-6-luna"
    assert usage["thread_id"] == "thread-test" and usage["turn_id"] == "turn-test"
    assert all(item["api"] == "codex" for item in store.list("provider_requests"))
    store.close()


@pytest.mark.asyncio
async def test_generation_change_during_selection_prevents_action(tmp_path):
    store = Store(tmp_path)
    store.put("runs", {"id": "run-1", "plan": Plan().model_dump()})
    current = [True]
    def respond(payload):
        current[0] = False
        return answers(payload)
    ai = OpenAI(store, Credentials(), backend=Backend(respond))
    calls = []
    async def dispatch(name, args):
        calls.append(name)
    await ai.player_turn(context(), dispatch, lambda: current[0], "run-1")
    assert not calls and store.events("run-1")[-1]["kind"] == "decision_cancelled"
    store.close()


@pytest.mark.asyncio
async def test_cancellation_reaches_backend_and_records_unknown_subscription_usage(tmp_path):
    store = Store(tmp_path)
    store.put("runs", {"id": "run-1", "plan": Plan().model_dump()})
    entered, interrupted = asyncio.Event(), asyncio.Event()
    async def slow(payload):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            interrupted.set()
            raise
    ai = OpenAI(store, Credentials(), backend=Backend(slow))
    questions = [choice("action", "Select one", [("wait", "Wait"), ("stop", "Stop")])]
    task = asyncio.create_task(ai.decisions("test", questions, run_id="run-1"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert interrupted.is_set()
    usage = store.list("usage")[0]
    assert usage["usage"] is None and usage["estimated_cost_usd"] is None
    assert usage["billing"] == "subscription" and usage["status"] == "request_cancelled"
    assert store.list("provider_requests")[0]["status"] == "request_cancelled"
    store.close()


@pytest.mark.parametrize("status", ["configuration_required", "model_unavailable", "rate_limited", "refused_decision"])
@pytest.mark.asyncio
async def test_backend_errors_preserve_status_and_do_not_fall_back(tmp_path, status):
    store = Store(tmp_path)
    def fail(payload):
        raise CodexError(status, "Codex requires attention")
    backend = Backend(fail)
    ai = OpenAI(store, Credentials(), backend=backend)
    with pytest.raises(ProviderError) as failed:
        await ai.decisions("test", [choice("action", "Select", [("wait", "Wait")])])
    assert failed.value.status == status and len(backend.calls) == 1
    assert store.list("provider_requests")[0]["status"] == status
    async def bad_status(model=MODEL):
        raise CodexError(status, "Codex requires attention")
    backend.status = bad_status
    assert (await ai.validate())["status"] == status
    assert len(backend.calls) == 1
    store.close()






@pytest.mark.asyncio
async def test_dense_observation_keeps_all_targets_and_full_selected_context(tmp_path):
    store = Store(tmp_path)
    store.put("runs", {"id": "run-1", "plan": Plan().model_dump()})
    value = context()
    entities = [{"id": str(i), "version": 1, "label": f"Button {i}: " + "x" * 1190, "actions": ["ui_click"]} for i in range(512)]
    entities[-1]["label"] += " SELECTED_TARGET_DETAIL"
    value["observation"] = prompt_observation({**value["observation"], "entities": entities})
    assert value["observation"]["context_truncated"]
    def respond(payload):
        assert len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) + 2048 < 128000
        if payload["questions"][0]["name"] == "target":
            assert len(payload["questions"][0]["choices"]) >= 512
            return answers(payload, {"target": "511"})
        assert "SELECTED_TARGET_DETAIL" in payload["input"]
        return answers(payload)
    backend = Backend(respond)
    ai = OpenAI(store, Credentials(), backend=backend)
    actions = []
    async def dispatch(name, args):
        if name == "list_entities":
            offset = args["offset"]
            return {"entities": entities[offset:offset+80], "next_offset": offset+80 if offset+80 < len(entities) else None}
        actions.append(args)
        return {"status": "running"}
    await ai.player_turn(value, dispatch, lambda: True, "run-1")
    assert len(actions) == 1 and actions[0]["target_id"] == "511" and len(backend.calls) == 2
    store.close()



@pytest.mark.asyncio
async def test_gameplay_and_usage_keep_frozen_run_model(tmp_path):
    store = Store(tmp_path)
    run = {"id": "run-model", "model": "gpt-6-sol", "plan": Plan().model_dump()}
    store.put("runs", run)
    backend = Backend()
    ai = OpenAI(store, backend=backend)
    await ai.validate("gpt-6-luna")
    async def dispatch(name, args):
        return {"status": "running"}
    await ai.player_turn(context(), dispatch, lambda: True, run["id"])
    assert backend.calls and all(call["model"] == "gpt-6-sol" for call in backend.calls)
    assert {item["purpose"] for item in store.list("usage")} == {"player"}
    assert all(item["model"] == item["requested_model"] == "gpt-6-sol" for item in store.list("usage"))
    assert all(item["model"] == "gpt-6-sol" for item in store.list("provider_requests"))
    store.close()




@pytest.mark.asyncio
async def test_completed_usage_is_retained_when_backend_rejects_json(tmp_path):
    store = Store(tmp_path)
    def fail(payload):
        raise CodexError("invalid_decision", "Invalid selection JSON", selection={"thread_id": "t1", "turn_id": "u1",
            "usage": {"input_tokens": 22, "output_tokens": 8}, "usage_scope": "selection_total"})
    ai = OpenAI(store, backend=Backend(fail))
    with pytest.raises(ProviderError):
        await ai.decisions("test", [choice("action", "Select", [("wait", "Wait")])])
    usage = store.list("usage")[0]
    assert usage["status"] == "invalid_decision" and usage["usage"]["input_tokens"] == 22
    assert usage["usage_scope"] == "selection_total" and usage["thread_id"] == "t1"
    assert usage["estimated_cost_usd"] is None
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot_only", [False, True])
async def test_planning_validates_schema_and_keeps_frozen_model_and_usage(tmp_path, snapshot_only):
    from ashad.subgoals import PlanningDecision
    store, backend = Store(tmp_path), Backend()
    run = {"id": "run-plan", "model": "gpt-6-sol", "plan": Plan().model_dump()}
    if snapshot_only:
        run["plan"]["model"] = run.pop("model")
    store.put("runs", run)
    response = planning_answers()
    backend.plan = AsyncMock(return_value=response)
    ai = OpenAI(store, Credentials(), backend=backend)
    await ai.validate("gpt-6-luna")
    evidence = context()
    result = await ai.plan_subgoals(evidence, run["id"])
    assert type(result) is dict and result == PlanningDecision.model_validate(response["answers"]).model_dump()
    assert "usage" not in result and not backend.calls
    backend.plan.assert_awaited_once_with(json.dumps(evidence, ensure_ascii=False),
        PlanningDecision.model_json_schema(), model="gpt-6-sol")
    [usage], [request] = store.list("usage"), store.list("provider_requests")
    assert usage["purpose"] == request["purpose"] == "planning"
    assert usage["run_id"] == request["run_id"] == run["id"]
    assert usage["model"] == usage["requested_model"] == request["model"] == "gpt-6-sol"
    assert usage["status"] == request["status"] == "completed"
    assert usage["usage"] == response["usage"] and usage["usage_scope"] == "planning_total"
    assert usage["thread_id"] == "planning-thread" and usage["turn_id"] == "planning-turn"
    assert usage["billing"] == "subscription" and usage["estimated_cost_usd"] is None
    assert request["question_names"] == ["subgoals"] and "tuning_stage" not in request
    assert [event["kind"] for event in store.events(run["id"])] == [
        "provider_request_started", "inference_usage", "provider_request_finished"]
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["not_object", "missing", "completed", "status", "mode", "condition", "subgoals"])
async def test_invalid_planning_shape_is_metered_and_never_selects_actions(tmp_path, damage):
    store, backend = Store(tmp_path), Backend()
    store.put("runs", {"id": "run-plan", "model": MODEL})
    response = planning_answers()
    if damage == "not_object":
        response["answers"] = []
    elif damage == "missing":
        response["answers"].pop("goal_id")
    elif damage in {"completed", "status"}:
        response["answers"][damage] = True if damage == "completed" else "completed"
    elif damage == "mode":
        response["answers"]["mode"] = "execute"
    elif damage == "condition":
        response["answers"]["completion"]["kind"] = "tutorial_partial_checklist"
    else:
        response["answers"]["subgoals"] = [{"tool": "perform_action", "args": {"verb": "debug_grant"}}]
    backend.plan = AsyncMock(return_value=response)
    ai = OpenAI(store, Credentials(), backend=backend)
    with pytest.raises(ProviderError) as caught:
        await ai.plan_subgoals(context(), "run-plan")
    assert caught.value.status == "invalid_plan" and not backend.calls
    backend.plan.assert_awaited_once()
    [usage], [request] = store.list("usage"), store.list("provider_requests")
    assert usage["status"] == request["status"] == "invalid_plan"
    assert usage["purpose"] == request["purpose"] == "planning"
    assert usage["usage"] == response["usage"] and usage["usage_scope"] == "planning_total"
    assert store.events("run-plan")[-1]["payload"]["status"] == "invalid_plan"
    assert all(event["kind"] != "decision_selected" for event in store.events("run-plan"))
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["context_limit", "model_unavailable"])
async def test_planning_rejects_oversize_context_or_unsupported_model_before_request(tmp_path, status):
    store, backend = Store(tmp_path), Backend()
    store.put("runs", {"id": "run-plan", "model": "unavailable-model" if status == "model_unavailable" else MODEL})
    backend.plan = AsyncMock(return_value=planning_answers())
    ai = OpenAI(store, Credentials(), backend=backend)
    evidence = context()
    if status == "context_limit":
        evidence["game_controls"] = "é" * 64000
    with pytest.raises(ProviderError) as caught:
        await ai.plan_subgoals(evidence, "run-plan")
    assert caught.value.status == status
    backend.plan.assert_not_awaited()
    assert not backend.calls and not store.list("usage") and not store.list("provider_requests")
    store.close()


@pytest.mark.asyncio
async def test_planning_cancellation_reaches_backend_and_records_unknown_usage(tmp_path):
    store, backend = Store(tmp_path), Backend()
    store.put("runs", {"id": "run-plan", "model": "gpt-6-sol"})
    entered, interrupted = asyncio.Event(), asyncio.Event()
    async def slow(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            interrupted.set()
            raise
    backend.plan = AsyncMock(side_effect=slow)
    ai = OpenAI(store, Credentials(), backend=backend)
    task = asyncio.create_task(ai.plan_subgoals(context(), "run-plan"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert interrupted.is_set() and not backend.calls
    [usage], [request] = store.list("usage"), store.list("provider_requests")
    assert usage["status"] == request["status"] == "request_cancelled"
    assert usage["purpose"] == request["purpose"] == "planning"
    assert usage["model"] == request["model"] == "gpt-6-sol"
    assert usage["usage"] is None and usage["estimated_cost_usd"] is None
    assert store.events("run-plan")[-1]["kind"] == "provider_request_finished"
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["configuration_required", "invalid_authentication", "model_unavailable", "rate_limited", "invalid_plan"])
async def test_planning_backend_error_preserves_status_usage_and_never_falls_back(tmp_path, status):
    store, backend = Store(tmp_path), Backend()
    store.put("runs", {"id": "run-plan", "model": MODEL})
    response = planning_answers()
    response.pop("answers")
    backend.plan = AsyncMock(side_effect=CodexError(status, "Codex planning requires attention", selection=response))
    ai = OpenAI(store, Credentials(), backend=backend)
    with pytest.raises(ProviderError) as caught:
        await ai.plan_subgoals(context(), "run-plan")
    assert caught.value.status == status and not backend.calls
    backend.plan.assert_awaited_once()
    [usage], [request] = store.list("usage"), store.list("provider_requests")
    assert usage["status"] == request["status"] == status
    assert usage["usage"] == response["usage"] and usage["usage_scope"] == "planning_total"
    assert usage["thread_id"] == request["thread_id"] == "planning-thread"
    assert usage["purpose"] == request["purpose"] == "planning"
    store.close()


@pytest.mark.asyncio
async def test_active_leaf_and_ancestors_reach_both_action_selection_stages(tmp_path):
    store, backend = Store(tmp_path), Backend()
    store.put("runs", {"id": "run-plan", "model": MODEL})
    evidence = context()
    evidence["current_goal"] = {"id": "play-goal", "objective": "Choose the visible Play button",
                                "completion": planning_answers()["answers"]["completion"]}
    evidence["goal_path"] = [{"id": "root", "objective": evidence["goal"]},
                             {"id": "menus", "objective": "Enter the world"}]
    evidence["subgoal_plan"] = {"root_id": "root", "active_goal_id": "play-goal"}
    ai = OpenAI(store, backend=backend)
    dispatch = AsyncMock(return_value={"status": "running"})
    await ai.player_turn(evidence, dispatch, lambda: True, "run-plan")
    assert len(backend.calls) == 2
    for call in backend.calls:
        supplied = json.loads(call["input"].split("\nSelected target:")[0])
        assert all(supplied[key] == evidence[key] for key in ("goal", "current_goal", "goal_path", "subgoal_plan"))
        instructions = call["questions"][0]["instructions"]
        assert all(key in instructions for key in ("current_goal", "goal_path", "subgoal_plan", "root objective"))
        assert "Verify actual effects" in instructions and "Input completion is not proof" in instructions
    dispatch.assert_awaited_once()
    store.close()
