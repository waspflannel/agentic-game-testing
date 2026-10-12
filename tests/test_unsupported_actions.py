"""Unsupported-action issue files use temporary stores and desktop/provider doubles only."""
import json
import typing
from unittest.mock import AsyncMock

import httpx
import pytest

from ashad import repair_tasks
from ashad.ai import GAME_VERBS, OpenAI, ProviderError, capability_summary
from ashad.codex_backend import CodexError
from ashad.repair_tasks import (_size, issue_directory, issue_title, shrink_example, issue_room, publish_issues, publish_pending, record_issue,
                                target_label)
from ashad.models import Plan
from ashad.store import Store
from ashad.worker_client import WorkerError
from test_acceptance_edges import game_context_worker, ready_run
from test_controller import environment, new_run  # noqa: F401
from test_decisions import Backend, answers, context
from worker.models import GameAction


@pytest.fixture(autouse=True)
def no_http_inference(monkeypatch):
    async def unexpected(*args, **kwargs):
        raise AssertionError("Provider tests must not make HTTP requests")
    monkeypatch.setattr(httpx.AsyncClient, "request", unexpected)


def example(**overrides):
    value = {"example_id": "uae_1", "at": "2026-10-11T00:00:00+00:00", "run_id": "run-1", "player_id": "player-1",
        "project_id": "project-1", "scenario_id": "scenario-1", "scenario_revision": 1, "model": "model-1", "controller_version": "1",
        "build": {"id": "build-1", "source_revision": "rev"}, "source": "agent_signal",
        "intent": {"primary_goal": "Plant a crop", "current_goal": None, "goal_path": [], "plan_reason": None},
        "wanted": {"tool": "perform_action", "verb": "ui_drag", "target_id": "slot-3", "target_kind": "inventory_slot",
            "target_label": "Slot 3", "parameters": None, "blocker": "action_missing", "controller": "agent",
            "follow_up_error": None},
        "outcome": None,
        "capabilities": {"structured_tools": ["observe"], "game_verbs": ["ui_drag"], "observed_inputs": ["inventory"],
            "target_advertised_actions": ["ui_click"], "candidate_actions": ["Slot 3: click"]},
        "recent_decisions": [{"event_id": "evt_1", "kind": "decision_selected", "at": "2026-10-11T00:00:00+00:00",
            "payload": {"player_id": "player-1"}}],
        "game_state": {"observation_id": "obs-1", "observation": {"observation_id": "obs-1", "entities": []},
            "telemetry": {"completed_quests": [], "diagnostics": None}, "progress": None},
        "screenshot": {"observation_id": "obs-1", "pin_id": "uae_1", "status": "unavailable", "frames": [],
            "retained_path_prefix": "runs/run-1/workers/player-1/pinned/uae_1/"},
        "evidence": {"run_dir": "/tmp/run-1", "ledger": "/tmp/run-1/ledger.jsonl", "report": "/tmp/run-1/report.md",
            "event_ids": ["evt_2"]},
        "truncated": []}
    value.update(overrides)
    return value


def basis(**overrides):
    return {"tool": "perform_action", "verb": "ui_drag", "blocker": "action_missing",
            "target_kind": "inventory_slot", "target_label": "slot #", "error_code": None, **overrides}


def files(store):
    return sorted(issue_directory(store).rglob("*.json"))


def test_repeat_groups_into_one_issue_and_caps_examples(tmp_path):
    store = Store(tmp_path / "data")
    for i in range(12):
        record_issue(store, "unsupported_action", basis(), example(example_id=f"uae_{i}"))
    publish_issues(store)
    [path] = files(store)
    issue = json.loads(path.read_text(encoding="utf-8"))
    assert issue["schema"] == "ashad.issue/2" and issue["occurrences"] == 12 and issue["status"] == "open"
    assert len(issue["examples"]) == 10 and issue["examples_kept"] == 10 and issue["examples_dropped"] == 2
    assert "published_revision" not in issue and "id" not in issue


def test_different_verb_is_a_separate_issue(tmp_path):
    store = Store(tmp_path / "data")
    record_issue(store, "unsupported_action", basis(verb="ui_drag"), example())
    record_issue(store, "unsupported_action", basis(verb="ui_click"), example())
    publish_issues(store)
    assert len(files(store)) == 2


def test_target_label_normalizes_digits_and_case():
    assert target_label("  Slot 12\n  Bag ") == "slot ## bag"


def test_oversized_example_is_truncated(tmp_path):
    store = Store(tmp_path / "data")
    big = example()
    big["game_state"]["telemetry"]["diagnostics"] = "x" * 200000
    record_issue(store, "unsupported_action", basis(), big)
    [issue] = store.list("issues")
    stored = issue["examples"][0]
    assert _size(stored) <= 65536 and "game_state.telemetry.diagnostics" in stored["truncated"]


def test_deleted_file_is_rewritten_on_next_change(tmp_path):
    store = Store(tmp_path / "data")
    record_issue(store, "unsupported_action", basis(), example())
    publish_issues(store)
    [path] = files(store)
    path.unlink()
    publish_issues(store)
    assert not path.exists()
    record_issue(store, "unsupported_action", basis(), example(example_id="uae_2"))
    publish_issues(store)
    assert json.loads(path.read_text(encoding="utf-8"))["occurrences"] == 2


def test_issue_cap_drops_new_signatures(tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    monkeypatch.setattr(repair_tasks, "MAX_ISSUES", 1)
    assert record_issue(store, "unsupported_action", basis(), example()) == "kept"
    assert record_issue(store, "unsupported_action", basis(verb="ui_click"), example()) == "full"
    assert len(store.list("issues")) == 1
    assert issue_room(store, "project-1", "unsupported_action", basis(verb="ui_click")) == "full"


def test_publish_pending_also_publishes_issues(tmp_path):
    store = Store(tmp_path / "data")
    record_issue(store, "unsupported_action", basis(), example())
    publish_pending(store)
    assert len(files(store)) == 1


def test_game_verbs_match_worker_contract():
    assert set(GAME_VERBS) == set(typing.get_args(GameAction.model_fields["verb"].annotation))


@pytest.mark.asyncio
async def test_needs_instructions_asks_one_follow_up(tmp_path):
    def respond(payload):
        names = [q["name"] for q in payload["questions"]]
        if names == ["target"]:
            return answers(payload, {"target": payload["questions"][0]["choices"][-1]["value"]})
        return answers(payload, {"wanted_verb": "ui_drag", "wanted_target": "crop", "blocker": "action_missing"})
    backend, dispatch = Backend(respond), AsyncMock()
    store = Store(tmp_path)
    store.put("runs", {"id": "run-1", "plan": Plan().model_dump()})
    with pytest.raises(ProviderError) as caught:
        await OpenAI(store, backend=backend).player_turn(context(), dispatch, lambda: True, "run-1")
    store.close()
    assert caught.value.status == "decision_needs_instructions"
    assert caught.value.report == {"source": "agent_signal", "wanted_verb": "ui_drag", "wanted_target_id": "crop", "blocker": "action_missing"}
    assert len(backend.calls) == 2
    assert [q["name"] for q in backend.calls[1]["questions"]] == ["wanted_verb", "wanted_target", "blocker"]
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_follow_up_failure_keeps_needs_instructions(tmp_path):
    def respond(payload):
        if payload["questions"][0]["name"] == "wanted_verb":
            raise CodexError("rate_limited", "fake")
        return answers(payload, {"target": payload["questions"][0]["choices"][-1]["value"]})
    store = Store(tmp_path)
    store.put("runs", {"id": "run-1", "plan": Plan().model_dump()})
    with pytest.raises(ProviderError) as caught:
        await OpenAI(store, backend=Backend(respond)).player_turn(context(), AsyncMock(), lambda: True, "run-1")
    store.close()
    assert caught.value.status == "decision_needs_instructions"
    assert caught.value.report == {"source": "agent_signal", "follow_up_error": "rate_limited"}


@pytest.mark.asyncio
async def test_invalid_follow_up_keeps_needs_instructions(tmp_path):
    turn = context()
    turn["observation"]["entities"].append({"id": "none", "version": 1, "label": "Unnamed", "actions": []})
    backend = Backend(lambda payload: answers(payload, {"target": payload["questions"][0]["choices"][-1]["value"]}))
    store = Store(tmp_path)
    store.put("runs", {"id": "run-1", "plan": Plan().model_dump()})
    with pytest.raises(ProviderError) as caught:
        await OpenAI(store, backend=backend).player_turn(turn, AsyncMock(), lambda: True, "run-1")
    store.close()
    assert caught.value.status == "decision_needs_instructions"
    assert caught.value.report == {"source": "agent_signal", "follow_up_error": "invalid_follow_up"}

def test_capability_summary_lists_tools_and_candidates():
    summary = capability_summary(context()["observation"])
    assert summary["game_verbs"] == list(GAME_VERBS)
    assert summary["observed_inputs"] == ["use_item", "inventory", "slot_1"]
    assert summary["candidate_actions"] and all(len(a) <= 300 for a in summary["candidate_actions"])


# Integration: the four sources through a real coordinator turn with desktop/provider doubles.

def slot(actions=("ui_click",), label="Slot 3"):
    return {"id": "slot-3", "version": 1, "kind": "inventory_slot", "label": label, "actions": list(actions)}


async def stuck_setup(environment, monkeypatch, entity=None):
    store, coord, _, desktops = environment
    run = await ready_run(environment)
    observation = game_context_worker(monkeypatch, desktops["worker-1"], run)
    observation["entities"] = [entity or slot()]
    observation["inputs"] = ["inventory"]
    return store, coord, desktops["worker-1"], run, observation


def stuck(payload):
    names = [q["name"] for q in payload["questions"]]
    if names == ["target"]:
        return answers(payload, {"target": payload["questions"][0]["choices"][-1]["value"]})
    if "wanted_verb" in names:
        return answers(payload, {"wanted_verb": "ui_drag", "wanted_target": "slot-3", "blocker": "action_missing"})
    return answers(payload)


def issue_files(store):
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(issue_directory(store).rglob("*.json"))]


@pytest.mark.asyncio
async def test_agent_signal_writes_detailed_issue_file(environment, monkeypatch):
    store, coord, desktop, run, observation = await stuck_setup(environment, monkeypatch)
    coord.ai = OpenAI(store, backend=Backend(stuck))
    await coord.player_turn(run["id"], "player-1")
    [issue] = issue_files(store)
    assert issue["type"] == "unsupported_action" and issue["occurrences"] == 1
    [ex] = issue["examples"]
    assert ex["source"] == "agent_signal"
    assert (ex["wanted"]["verb"], ex["wanted"]["target_id"], ex["wanted"]["target_kind"], ex["wanted"]["blocker"]) == \
        ("ui_drag", "slot-3", "inventory_slot", "action_missing")
    assert ex["intent"]["primary_goal"]
    assert ex["capabilities"]["game_verbs"] == list(GAME_VERBS)
    assert ex["capabilities"]["target_advertised_actions"] == ["ui_click"]
    assert ex["recent_decisions"]
    assert ex["game_state"]["observation_id"] == observation["observation_id"]
    assert ex["screenshot"]["pin_id"] == ex["example_id"] and ex["screenshot"]["status"] == "pinned"
    assert any(path == "/pin" and data["incident_id"] == ex["example_id"] for path, data in desktop.calls)
    recorded = [e for e in store.events(run["id"], limit=500) if e["kind"] == "unsupported_action_recorded"]
    assert len(recorded) == 1
    assert recorded[0]["payload"]["outcome"] == "kept"
    assert recorded[0]["id"] not in ex["evidence"]["event_ids"]
    assert recorded[0]["payload"]["issue_id"] == issue["issue_id"]
    desktop.screenshot.assert_not_called()
    assert store.require("runs", run["id"])["state"] == "Investigating"


@pytest.mark.asyncio
async def test_follow_up_failure_still_records_and_keeps_existing_flow(environment, monkeypatch):
    store, coord, _, run, _ = await stuck_setup(environment, monkeypatch)
    def respond(payload):
        if payload["questions"][0]["name"] == "wanted_verb":
            raise CodexError("rate_limited", "fake")
        return stuck(payload)
    coord.ai = OpenAI(store, backend=Backend(respond))
    await coord.player_turn(run["id"], "player-1")
    [issue] = issue_files(store)
    assert issue["examples"][0]["wanted"]["follow_up_error"] == "rate_limited"
    assert store.require("runs", run["id"])["state"] == "Investigating"
    assert any(e["kind"] == "agent_turn_failed" and e["payload"]["category"] == "decision_needs_instructions"
               for e in store.events(run["id"], limit=500))


@pytest.mark.asyncio
async def test_no_candidates_records_drag_source(environment, monkeypatch):
    store, coord, _, run, _ = await stuck_setup(environment, monkeypatch, slot(("ui_drag",)))
    def respond(payload):
        question = payload["questions"][0]
        pick = next(c["value"] for c in question["choices"] if c["description"].startswith("Slot 3"))
        return answers(payload, {"target": pick})
    coord.ai = OpenAI(store, backend=Backend(respond))
    await coord.player_turn(run["id"], "player-1")
    [issue] = issue_files(store)
    ex = issue["examples"][0]
    assert ex["source"] == "no_candidates"
    assert ex["wanted"]["verb"] == "ui_drag" and ex["wanted"]["blocker"] == "target_missing"


@pytest.mark.asyncio
async def test_same_missing_action_from_two_sources_is_one_issue(environment, monkeypatch):
    store, coord, _, run, _ = await stuck_setup(environment, monkeypatch, slot(("ui_drag",)))
    def first(payload):
        question = payload["questions"][0]
        pick = next(c["value"] for c in question["choices"] if c["description"].startswith("Slot 3"))
        return answers(payload, {"target": pick})
    coord.ai = OpenAI(store, backend=Backend(first))
    await coord.player_turn(run["id"], "player-1")
    coord.structured.last_decisions.clear()
    store.put("runs", {**store.require("runs", run["id"]), "state": "Running"})  # The first hit parked the run.
    def second(payload):
        names = [q["name"] for q in payload["questions"]]
        if names == ["target"]:
            return answers(payload, {"target": payload["questions"][0]["choices"][-1]["value"]})
        if "wanted_verb" in names:
            return answers(payload, {"wanted_verb": "ui_drag", "wanted_target": "slot-3", "blocker": "target_missing"})
        return answers(payload)
    coord.ai = OpenAI(store, backend=Backend(second))
    await coord.player_turn(run["id"], "player-1")
    [issue] = issue_files(store)
    assert issue["occurrences"] == 2 and len(issue["examples"]) == 2
    assert {ex["source"] for ex in issue["examples"]} == {"no_candidates", "agent_signal"}


@pytest.mark.asyncio
async def test_failed_record_writes_no_recorded_event(environment, monkeypatch):
    store, coord, _, run, _ = await stuck_setup(environment, monkeypatch)
    monkeypatch.setattr(repair_tasks, "record_issue", lambda *a, **k: None)
    coord.ai = OpenAI(store, backend=Backend(stuck))
    await coord.player_turn(run["id"], "player-1")
    assert not [e for e in store.events(run["id"], limit=500) if e["kind"] == "unsupported_action_recorded"]
    assert store.require("runs", run["id"])["state"] == "Investigating"


@pytest.mark.asyncio
async def test_finish_clears_per_run_counters(environment, monkeypatch):
    store, coord, _, run, _ = await stuck_setup(environment, monkeypatch)
    structured = coord.structured
    structured.loop_counts[(run["id"], "player-1")] = (0, "sig", 3)
    structured.bug_reports[(run["id"], "player-1")] = "sig"
    structured.crashes_recorded.add((run["id"], "player-1", 0, "game_exit"))
    await coord.finish(run["id"], "user_stopped", terminate=True)
    for container in (structured.loop_counts, structured.bug_reports, structured.crashes_recorded):
        assert not [k for k in container if k[0] == run["id"]]


@pytest.mark.asyncio
async def test_worker_rejection_records_but_stale_409_does_not(environment, monkeypatch):
    store, coord, desktop, run, observation = await stuck_setup(environment, monkeypatch)
    inner = desktop.request
    failure = {"status": 422}
    async def request(method, path, *, data=None, **kwargs):
        if path == "/game" and data["operation"] == "perform_action":
            raise WorkerError("bad target", failure["status"], ambiguous=False, code="invalid_target")
        return await inner(method, path, data=data, **kwargs)
    monkeypatch.setattr(desktop, "request", request)
    args = {"verb": "ui_click", "target_id": "slot-3", "target_version": 1, "parameters": {}}
    generation = run["players"]["player-1"]["generation"]
    await coord.structured.dispatch(run["id"], "player-1", "perform_action", args, observation, generation)
    [issue] = issue_files(store)
    ex = issue["examples"][0]
    assert ex["source"] == "worker_rejected" and ex["outcome"]["http_status"] == 422
    assert ex["outcome"]["error_code"] == "invalid_target" and ex["wanted"]["controller"] == "agent"
    failure["status"] = 409
    await coord.structured.dispatch(run["id"], "player-1", "perform_action", args, observation, generation)
    [issue] = issue_files(store)
    assert issue["occurrences"] == 1


@pytest.mark.asyncio
async def test_loop_backstop_records_after_unchanged_decisions(environment, monkeypatch):
    store, coord, _, run, _ = await stuck_setup(environment, monkeypatch)
    coord.ai = OpenAI(store, backend=Backend(answers))
    for _ in range(5):
        await coord.player_turn(run["id"], "player-1")
    assert issue_files(store) == []
    await coord.player_turn(run["id"], "player-1")
    [issue] = issue_files(store)
    ex = issue["examples"][0]
    assert ex["source"] == "loop_backstop" and ex["wanted"]["verb"] == "ui_click"
    assert store.require("runs", run["id"])["state"] == "Investigating"


@pytest.mark.asyncio
async def test_secrets_never_reach_issue_file(environment, monkeypatch):
    entity = {**slot(label="Slot sk-sensitivevalue123456"), "state": {"api_key": "private-test-key", "note": "Bearer abc.def.ghi"}}
    store, coord, _, run, _ = await stuck_setup(environment, monkeypatch, entity)
    store.event(run["id"], "action_result", {"player_id": "player-1", "result": {"token": "private-token-value"}})
    coord.ai = OpenAI(store, backend=Backend(stuck))
    await coord.player_turn(run["id"], "player-1")
    [path] = issue_directory(store).rglob("*.json")
    text = path.read_text(encoding="utf-8")
    for secret in ("sk-sensitivevalue123456", "private-test-key", "abc.def.ghi", "private-token-value"):
        assert secret not in text
    assert "[REDACTED]" in text
    for forbidden in ('"transport"', '"game_controls"', '"agent_memory"', "fixture-engine"):
        assert forbidden not in text


@pytest.mark.asyncio
async def test_pin_failure_marks_screenshot_unavailable(environment, monkeypatch):
    store, coord, desktop, run, _ = await stuck_setup(environment, monkeypatch)
    inner = desktop.request
    async def request(method, path, *, data=None, **kwargs):
        if path == "/pin":
            raise OSError("pin unavailable")
        return await inner(method, path, data=data, **kwargs)
    monkeypatch.setattr(desktop, "request", request)
    coord.ai = OpenAI(store, backend=Backend(stuck))
    await coord.player_turn(run["id"], "player-1")
    [issue] = issue_files(store)
    assert issue["examples"][0]["screenshot"]["status"] == "unavailable"
    assert store.require("runs", run["id"])["state"] == "Investigating"


@pytest.mark.asyncio
async def test_issues_survive_run_deletion(environment, monkeypatch):
    store, coord, _, run, _ = await stuck_setup(environment, monkeypatch)
    coord.ai = OpenAI(store, backend=Backend(stuck))
    await coord.player_turn(run["id"], "player-1")
    with store.transaction():
        coord._forget_run_records(store.require("runs", run["id"]))
    assert len(store.list("issues")) == 1
    assert len(list(issue_directory(store).rglob("*.json"))) == 1


def test_issue_title_reads_for_other_verb_and_missing_target():
    assert issue_title("unsupported_action", basis()) == "Agent could not ui_drag on slot #"
    assert issue_title("unsupported_action", basis(verb="other", target_kind="none", target_label="")) == \
        "Agent could not perform an action this interface does not offer"


def test_oversized_wanted_parameters_are_trimmed():
    big = example(wanted={**example()["wanted"], "parameters": {"text": "x" * 70000}})
    small = shrink_example(big)
    assert _size(small) <= repair_tasks.MAX_EXAMPLE_BYTES and small["wanted"]["parameters"] is None
    assert "wanted.parameters" in small["truncated"]
