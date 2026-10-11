"""Crash and game_bug issue titles and example trimming; temporary data only."""
import asyncio
import json
import time
from unittest.mock import AsyncMock

import httpx
import pytest

from ashad import repair_tasks
from ashad.ai import OpenAI, action_groups
from ashad.coordinator import crash_of
from ashad.models import Plan
from ashad.repair_tasks import _size, issue_directory, issue_title, record_issue, shrink_example, target_label
from ashad.store import Store
from ashad.worker_client import WorkerError
from test_acceptance_edges import game_context_worker, ready_run
from test_controller import environment  # noqa: F401
from test_decisions import Backend, answers, context
from test_unsupported_actions import example


@pytest.fixture(autouse=True)
def no_http_inference(monkeypatch):
    async def unexpected(*args, **kwargs):
        raise AssertionError("Provider tests must not make HTTP requests")
    monkeypatch.setattr(httpx.AsyncClient, "request", unexpected)


def bug_basis(**overrides):
    return {"symptom": "wrong_result", "expected": "state_change", "observed": "no_change", "action_verb": "interact",
            "target_kind": "chest", "target_label": "chest #", **overrides}


def test_issue_titles_per_type():
    assert issue_title("crash", {"kind": "game_exit", "detector": "worker_status", "exit": "signal:SIGSEGV"}) == \
        "Game process exited (signal:SIGSEGV)"
    assert issue_title("crash", {"kind": "worker_lost", "detector": "worker_unreachable", "exit": None}) == \
        "Game worker was lost (worker_unreachable)"
    assert issue_title("game_bug", bug_basis()) == \
        "Game bug (wrong_result): expected state_change, observed no_change on chest # after interact"
    assert issue_title("game_bug", bug_basis(target_kind="none", target_label="", action_verb=None)) == \
        "Game bug (wrong_result): expected state_change, observed no_change"


def test_crash_and_bug_examples_shrink_under_limit():
    base = example()
    for key in ("wanted", "outcome", "capabilities"):
        del base[key]
    crash = shrink_example({**base, "crash": {"log_tail": "x" * 70000}})
    assert _size(crash) <= repair_tasks.MAX_EXAMPLE_BYTES
    assert "crash.log_tail" in crash["truncated"] and "wanted.parameters" not in crash["truncated"]

    state = {**base["game_state"], "observation": {"observation_id": "obs-1",
        "entities": [{"id": f"e{i}", "label": "y" * 190} for i in range(400)]}}
    bug = shrink_example({**base, "report": {}, "game_state": state})
    assert _size(bug) <= repair_tasks.MAX_EXAMPLE_BYTES
    assert "game_state.observation.entities" in bug["truncated"]


def issue_files(store):
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(issue_directory(store).glob("*.json"))]


async def crash_setup(environment, monkeypatch, status=None, fail=None, telemetry=None):
    store, coord, _, desktops = environment
    run = await ready_run(environment)
    run["players"]["player-1"]["launched_at"] -= 10
    store.put("runs", run)
    original = desktops["worker-1"].request

    async def request(method, path, **kwargs):
        if fail and path == fail[0]:
            raise fail[1]
        result = await original(method, path, **kwargs)
        if path == "/status":
            return {**result, **(status or {})}
        if path == "/telemetry" and telemetry:
            return {**result, **telemetry}
        return result

    monkeypatch.setattr(desktops["worker-1"], "request", request)
    return store, coord, desktops["worker-1"], run


def prior_action(store, run):
    store.event(run["id"], "structured_action_intent", {"player_id": "player-1", "action_id": "game_1", "reason": "open",
        "input": {"verb": "interact", "target_id": "chest-3", "parameters": {}}})
    store.event(run["id"], "action_result", {"player_id": "player-1", "action_id": "game_1", "result": {"status": "completed"}})


EXIT = {"game_running": False, "game_exit": {"exit_code": -11, "signal": "SIGSEGV", "log_tail": "boom sk-sensitivevalue123456"}}


async def tick(store, coord, run):
    await coord.tick(store.require("runs", run["id"]))


@pytest.mark.asyncio
async def test_game_exit_records_crash_with_exit_signal_log_tail_and_frames(environment, monkeypatch):
    store, coord, _, run = await crash_setup(environment, monkeypatch, EXIT)
    prior_action(store, run)
    await tick(store, coord, run)
    [issue] = issue_files(store)
    assert issue["type"] == "crash"
    assert issue["signature_basis"] == {"kind": "game_exit", "detector": "worker_status", "exit": "signal:SIGSEGV"}
    [ex] = issue["examples"]
    assert ex["crash"]["exit_code"] == -11
    assert ex["last_action"]["verb"] == "interact" and ex["last_action"]["status"] == "completed"
    assert set(ex["timing"]) == {"detected_at", "launched_at", "telemetry_received_at", "feed_captured_at", "simulation_advanced_at"}
    assert ex["screenshot"]["status"] == "pinned" and ex["screenshot"]["pin_id"] == ex["example_id"]
    assert "sk-sensitivevalue123456" not in (next(issue_directory(store).glob('*.json'))).read_text(encoding="utf-8")
    actual = store.require("runs", run["id"])
    assert actual["state"] == "Investigating" and actual["validity"] == "invalid"
    [recorded] = [e for e in store.events(run["id"], limit=500) if e["kind"] == "crash_recorded"]
    assert recorded["payload"]["outcome"] == "kept"


@pytest.mark.asyncio
async def test_repeated_ticks_add_one_crash_hit(environment, monkeypatch):
    store, coord, _, run = await crash_setup(environment, monkeypatch, EXIT)
    await tick(store, coord, run)
    await tick(store, coord, run)
    [issue] = issue_files(store)
    assert issue["occurrences"] == 1


@pytest.mark.asyncio
async def test_crash_after_resume_counts_again(environment, monkeypatch):
    store, coord, _, run = await crash_setup(environment, monkeypatch, EXIT)
    await tick(store, coord, run)
    actual = store.require("runs", run["id"])
    actual["players"]["player-1"]["generation"] += 1
    store.put("runs", actual)
    await tick(store, coord, run)
    [issue] = issue_files(store)
    assert issue["occurrences"] == 2


@pytest.mark.asyncio
async def test_same_crash_in_two_runs_groups(environment):
    store, coord, _, _ = environment

    async def crash(run_id):
        run = store.require("runs", run_id)
        run["players"]["player-1"]["worker_status"] = {"game_running": False}
        store.put("runs", run)
        await coord.structured.report_crash(run_id, "player-1", "game_exit", "worker_status", "exit")

    run1 = await ready_run(environment)
    await crash(run1["id"])
    await coord.finish(run1["id"], "user_stopped", terminate=True)
    run2 = await ready_run(environment)
    await crash(run2["id"])
    [issue] = issue_files(store)
    assert issue["occurrences"] == 2 and len(issue["runs"]) == 2


@pytest.mark.asyncio
async def test_unreachable_worker_records_worker_lost_without_pin(environment, monkeypatch):
    error = WorkerError("Desktop worker unavailable: ConnectError", method="POST", path="/heartbeat")
    store, coord, desktop, run = await crash_setup(environment, monkeypatch, fail=("/heartbeat", error))
    await tick(store, coord, run)
    [issue] = issue_files(store)
    assert issue["signature_basis"]["kind"] == "worker_lost" and issue["signature_basis"]["detector"] == "worker_unreachable"
    assert issue["examples"][0]["screenshot"]["status"] == "unavailable"
    assert not any(path == "/pin" and data["incident_id"].startswith("crash_") for path, data in desktop.calls)
    assert store.require("runs", run["id"])["state"] == "Investigating"


@pytest.mark.asyncio
async def test_capture_unhealthy_records_desktop_lost(environment, monkeypatch):
    store, coord, _, run = await crash_setup(environment, monkeypatch, {"game_running": True, "infrastructure_error": "capture_unhealthy"})
    await tick(store, coord, run)
    [issue] = issue_files(store)
    assert issue["signature_basis"]["kind"] == "desktop_lost"


@pytest.mark.asyncio
async def test_heartbeat_freeze_records_game_hang(environment, monkeypatch):
    store, coord, _, run = await crash_setup(environment, monkeypatch, {"game_running": True},
        telemetry={"sequence": 999, "world_simulating": False})
    clock = [time.time()]
    monkeypatch.setattr("ashad.coordinator.time.time", lambda: clock[0])
    monkeypatch.setattr(coord, "player_turn", AsyncMock())
    try:
        await tick(store, coord, run)
        await asyncio.sleep(0)
        clock[0] += run["plan"]["stall_policy"]["simulation_timeout_seconds"] + 1
        await tick(store, coord, run)
        await asyncio.sleep(0)
        [issue] = issue_files(store)
        assert issue["signature_basis"] == {"kind": "game_hang", "detector": "heartbeat_stalled", "exit": None}
        assert any(i["category"] == "suspected_game_freeze" for i in store.list("incidents"))
        assert store.require("runs", run["id"])["validity"] != "invalid"
    finally:
        for task in coord.tasks.values():
            task.cancel()
        await asyncio.gather(*coord.tasks.values(), return_exceptions=True)


@pytest.mark.parametrize("case", ["hash", "heartbeat_lost", "agent"])
@pytest.mark.asyncio
async def test_non_crash_failures_write_no_issue(environment, monkeypatch, case):
    status = {"hash": {"build": {"executable_sha256": "bad"}},
              "heartbeat_lost": {"infrastructure_error": "controller_heartbeat_lost"}}.get(case)
    store, coord, _, run = await crash_setup(environment, monkeypatch, status)
    if case == "agent":
        monkeypatch.setattr(coord.structured, "turn", AsyncMock(side_effect=RuntimeError("boom")))
        await coord.player_turn(run["id"], "player-1")
    else:
        await tick(store, coord, run)
    assert issue_files(store) == []
    assert store.list("repair_task_exports")


@pytest.mark.asyncio
async def test_old_worker_without_game_exit_still_records(environment, monkeypatch):
    store, coord, _, run = await crash_setup(environment, monkeypatch, {"game_running": False})
    await tick(store, coord, run)
    [issue] = issue_files(store)
    assert issue["signature_basis"]["exit"] is None
    assert [issue["examples"][0]["crash"][k] for k in ("exit_code", "signal", "log_tail")] == [None, None, None]


@pytest.mark.asyncio
async def test_crash_record_failure_still_investigates(environment, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("disk full")
    monkeypatch.setattr(repair_tasks, "record_issue", boom)
    store, coord, _, run = await crash_setup(environment, monkeypatch, EXIT)
    await tick(store, coord, run)
    assert store.require("runs", run["id"])["state"] == "Investigating"
    assert issue_files(store) == []


def test_crash_of_classifies_monitor_failures():
    def failure(cls, step, *args, **kwargs):
        exc = cls(*args, **kwargs)
        exc.monitoring_step = step
        return exc
    lost = failure(WorkerError, "sending worker heartbeat", "down", method="POST", path="/heartbeat")
    assert crash_of(lost, {}) == ("worker_lost", "worker_unreachable")
    assert crash_of(failure(WorkerError, "reading worker status", "down", method="GET", path="/status"), {}) == \
        ("worker_lost", "worker_unreachable")
    assert crash_of(failure(ValueError, "sending worker heartbeat", "x"), {"game_running": False}) is None
    assert crash_of(failure(ValueError, "reading worker status", "x"), {"game_running": False}) == ("game_exit", "worker_status")
    assert crash_of(failure(ValueError, "reading worker status", "x"), {"infrastructure_error": "worker_restarted: 2"}) == \
        ("worker_lost", "worker_restarted")
    assert crash_of(failure(ValueError, "reading worker status", "x"), {"infrastructure_error": "capture_unhealthy"}) == \
        ("desktop_lost", "capture_unhealthy")
    assert crash_of(failure(ValueError, "reading game telemetry", "x"), {"game_running": True}) is None


REPORT = {"symptom": "wrong_result", "expected": "state_change", "observed": "no_change",
          "related_action_id": "game_1", "target_id": "chest-3"}


async def bug_turn(environment, monkeypatch, report, setup=prior_action):
    store, coord, _, desktops = environment
    run = await ready_run(environment)
    observation = game_context_worker(monkeypatch, desktops["worker-1"], run)
    observation["entities"] = [{"id": "chest-3", "version": 1, "kind": "chest", "label": "Chest 3", "actions": ["interact"], "state": {"open": False}}]
    setup(store, run)
    coord.ai.player_turn = AsyncMock(return_value={"status": "bug_reported", "action": {"tool": "report_game_bug", "args": {}}, "report": report})
    await coord.player_turn(run["id"], "player-1")
    return store, coord, run


@pytest.mark.asyncio
async def test_bug_choice_asks_one_structured_follow_up(tmp_path):
    def respond(payload):
        if [q["name"] for q in payload["questions"]] == ["target"]:
            return answers(payload, {"target": payload["questions"][0]["choices"][-2]["value"]})
        return answers(payload)
    backend, dispatch = Backend(respond), AsyncMock()
    store = Store(tmp_path)
    store.put("runs", {"id": "run-1", "plan": Plan().model_dump()})
    result = await OpenAI(store, backend=backend).player_turn(context(), dispatch, lambda: True, "run-1")
    store.close()
    assert result["status"] == "bug_reported"
    assert set(result["report"]) == {"symptom", "expected", "observed", "related_action_id", "target_id"}
    assert len(backend.calls) == 2
    assert [q["name"] for q in backend.calls[1]["questions"]] == ["symptom", "expected", "observed", "related_action", "target"]
    dispatch.assert_not_awaited()


def test_bug_group_offered_before_needs_instructions_and_hidden_after_report():
    groups = action_groups(context())
    assert groups[-1]["actions"][0]["tool"] == "needs_instructions"
    assert groups[-2]["actions"][0]["tool"] == "report_game_bug"
    hidden = action_groups({**context(), "bug_already_reported": True})
    assert not any(a["tool"] == "report_game_bug" for g in hidden for a in g["actions"])


@pytest.mark.asyncio
async def test_wrong_result_after_completed_action_writes_bug_issue(environment, monkeypatch):
    store, coord, run = await bug_turn(environment, monkeypatch, REPORT)
    [issue] = issue_files(store)
    assert issue["type"] == "game_bug"
    assert issue["signature_basis"]["action_verb"] == "interact"
    assert issue["signature_basis"]["target_kind"] == "chest"
    assert issue["signature_basis"]["target_label"] == "chest #"
    [ex] = issue["examples"]
    assert ex["report"]["related_action"]["status"] == "completed"
    assert ex["report"]["target_state"] == {"open": False}
    assert ex["recent_decisions"] and ex["screenshot"]["status"] == "pinned"
    assert store.require("runs", run["id"])["state"] == "Running"
    [recorded] = [e for e in store.events(run["id"], limit=500) if e["kind"] == "game_bug_recorded"]
    assert recorded["payload"]["outcome"] == "kept"
    assert coord.structured.bug_reports[(run["id"], "player-1")]


def rejected_action(store, run):
    store.event(run["id"], "structured_action_intent", {"player_id": "player-1", "action_id": "game_2", "reason": "open",
        "input": {"verb": "interact", "target_id": "chest-3", "parameters": {}}})
    store.event(run["id"], "action_result", {"player_id": "player-1", "action_id": "game_2", "result": {"status": "rejected"}})


@pytest.mark.parametrize("related", [None, "game_2"])
@pytest.mark.asyncio
async def test_wrong_result_without_completed_action_is_rejected(environment, monkeypatch, related):
    store, _, run = await bug_turn(environment, monkeypatch, {**REPORT, "related_action_id": related}, rejected_action)
    assert issue_files(store) == []
    [event] = [e for e in store.events(run["id"], limit=500) if e["kind"] == "game_bug_report_rejected"]
    assert event["payload"]["reason"] == "no_completed_action"


@pytest.mark.parametrize("report, reason", [({**REPORT, "symptom": "not_a_bug"}, "not_a_bug"),
                                            ({"follow_up_error": "invalid_decision"}, "follow_up_failed")])
@pytest.mark.asyncio
async def test_not_a_bug_and_follow_up_failure_write_no_issue(environment, monkeypatch, report, reason):
    store, _, run = await bug_turn(environment, monkeypatch, report)
    assert issue_files(store) == []
    [event] = [e for e in store.events(run["id"], limit=500) if e["kind"] == "game_bug_report_rejected"]
    assert event["payload"]["reason"] == reason
    assert store.require("runs", run["id"])["state"] == "Running"


def test_bug_signature_groups_across_runs_and_digit_labels(tmp_path):
    store = Store(tmp_path / "data")
    record_issue(store, "game_bug", bug_basis(target_label=target_label("Chest 3")), example(run_id="run-1"))
    record_issue(store, "game_bug", bug_basis(target_label=target_label("chest 7")), example(run_id="run-2", example_id="bug_2"))
    repair_tasks.publish_pending(store)
    [issue] = issue_files(store)
    assert issue["occurrences"] == 2 and issue["runs"] == ["run-1", "run-2"]
