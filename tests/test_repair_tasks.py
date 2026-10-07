"""Repair inbox contracts use temporary stores and desktop/provider doubles only."""
import json
from unittest.mock import AsyncMock

import pytest

from ashad.ai import ProviderError
from ashad.coordinator import Coordinator
from ashad.models import MODEL
from ashad.repair_tasks import publish_pending, record_task, task_directory
from test_controller import environment, new_run  # noqa: F401
from test_acceptance_edges import game_context_worker, ready_run


def tasks(store):
    result = []
    for path in task_directory(store).glob("*.md"):
        text = path.read_text(encoding="utf-8")
        if "```json\n" in text:
            result.append((path, json.loads(text.split("```json\n", 1)[1].split("```", 1)[0])))
    return result


@pytest.mark.asyncio
async def test_manual_pause_export_is_not_recreated_after_external_deletion(environment):
    store, coord, _, _ = environment
    run = await ready_run(environment)
    await coord.control(run["id"], "pause", "Inspect the menu", "pause")
    [(path, snapshot)] = tasks(store)
    assert task_directory(store) == store.root / "repair-tasks"
    assert snapshot["trigger"] == "pause" and snapshot["expected_event"] is True
    assert snapshot["reason"] == "Inspect the menu"
    assert snapshot["category"] == "manual_pause"
    path.unlink()  # The user's watching agent acknowledges tasks by deleting them.
    await coord.control(run["id"], "pause", command_id="another-pause-command")
    await coord.recover()
    publish_pending(store)
    assert tasks(store) == []
    assert len(store.list("repair_task_exports")) == 1


@pytest.mark.asyncio
async def test_no_decision_exports_failure_context_once(environment, monkeypatch):
    store, coord, _, desktops = environment
    run = await ready_run(environment)
    game_context_worker(monkeypatch, desktops["worker-1"], run)
    message = "No suitable supported action for this observation"
    coord.ai.player_turn = AsyncMock(side_effect=ProviderError("decision_needs_instructions", message))
    store.event(run["id"], "diagnostic", {"api_key": "private-test-key", "message": "sk-sensitivevalue123456"})
    await coord.player_turn(run["id"], "player-1")
    await coord.investigate(run["id"], "decision_needs_instructions", message, ["player-1"])
    [(path, snapshot)] = tasks(store)
    assert snapshot["schema_version"] == 1
    assert snapshot["trigger"] == "investigation" and snapshot["expected_event"] is False
    assert snapshot["category"] == "decision_needs_instructions"
    assert snapshot["run_id"] == run["id"] and snapshot["run_state"] == "Investigating"
    assert snapshot["model"] == MODEL == "gpt-6-luna"
    assert snapshot["reason"] == message
    assert snapshot["plan"]["build_id"] == run["build"]["id"]
    assert snapshot["build"]["source_revision"] == "test-revision"
    assert snapshot["players"]["player-1"]["agent_status"]["last_error"] == message
    assert any(event["kind"] == "agent_turn_failed" for event in snapshot["recent_events"])
    assert snapshot["event_id"] in {event["id"] for event in snapshot["recent_events"]}
    assert str(store.run_dir(run["id"])) in json.dumps(snapshot["evidence"]).replace("\\\\", "\\")
    text = path.read_text(encoding="utf-8")
    assert "private-test-key" not in text and "sk-sensitivevalue123456" not in text
    assert "[REDACTED]" in text
    path.unlink()
    await coord.recover()
    publish_pending(store)
    assert tasks(store) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["user_stopped", "completed"])
async def test_expected_stops_are_logged_without_claiming_a_failure(environment, reason):
    store, coord, _, _ = environment
    run = await new_run(environment) if reason == "user_stopped" else await ready_run(environment, completed=["demo_wrapup_moar"])
    if reason == "completed":
        await coord.monitor(run)
    else:
        await coord.control(run["id"], "terminate", command_id="terminate-queued")
    [(path, snapshot)] = tasks(store)
    assert path.exists() and snapshot["trigger"] == "stop"
    assert snapshot["reason"] == reason and snapshot["expected_event"] is True
    assert store.require("runs", run["id"])["state"] == "Ended"


@pytest.mark.asyncio
async def test_restart_logs_only_a_new_hold_of_running_bots(environment):
    store, coord, _, _ = environment
    run = await ready_run(environment)
    await coord.recover()
    [(path, snapshot)] = tasks(store)
    assert snapshot["trigger"] == "restart" and snapshot["expected_event"] is False
    assert snapshot["run_id"] == run["id"]
    assert store.require("runs", run["id"])["state"] == "Paused"
    path.unlink()
    await coord.recover()
    assert tasks(store) == []


@pytest.mark.asyncio
async def test_failed_resume_has_its_own_actionable_task(environment, monkeypatch):
    store, coord, _, desktops = environment
    run = await ready_run(environment)
    await coord.control(run["id"], "pause", command_id="pause")
    original = desktops["worker-1"].request

    async def request(method, path, **kwargs):
        if path == "/resume":
            raise OSError("Desktop unreachable")
        return await original(method, path, **kwargs)

    monkeypatch.setattr(desktops["worker-1"], "request", request)
    with pytest.raises(ValueError, match="bots remain paused"):
        await coord.control(run["id"], "resume", command_id="resume")
    snapshots = [snapshot for _, snapshot in tasks(store)]
    assert len(snapshots) == 2
    failure = next(snapshot for snapshot in snapshots if snapshot["trigger"] == "resume_failed")
    assert failure["expected_event"] is False and "Desktop unreachable" in failure["reason"]
    assert store.require("runs", run["id"])["state"] == "Paused"


@pytest.mark.asyncio
async def test_agent_crash_exports_the_exception_and_affected_player(environment, monkeypatch):
    store, coord, _, _ = environment
    run = await ready_run(environment)
    monkeypatch.setattr(coord.structured, "turn", AsyncMock(side_effect=RuntimeError("Broken turn")))
    await coord.player_turn(run["id"], "player-1")
    [(_, snapshot)] = tasks(store)
    assert snapshot["category"] == "controller_failure" and snapshot["expected_event"] is False
    assert "RuntimeError: Broken turn" in snapshot["reason"]
    assert "Broken turn" in snapshot["players"]["player-1"]["agent_status"]["last_error"]
    assert any(event["kind"] == "agent_task_failed" for event in snapshot["recent_events"])


@pytest.mark.asyncio
async def test_new_crash_during_investigation_is_logged_once_without_extending_deadline(environment, monkeypatch):
    store, coord, _, desktops = environment
    run = await ready_run(environment)
    run["players"]["player-1"]["launched_at"] -= 10
    store.put("runs", run)
    await coord.investigate(run["id"], "decision_needs_instructions", "No supported action", ["player-1"])
    deadline = store.require("runs", run["id"])["investigation_deadline"]
    original = desktops["worker-1"].request

    async def request(method, path, **kwargs):
        if path == "/status":
            return {"game_running": False}
        return await original(method, path, **kwargs)

    monkeypatch.setattr(desktops["worker-1"], "request", request)
    await coord.tick(store.require("runs", run["id"]))
    assert len(tasks(store)) == 2
    crash_path, crash = next((path, snapshot) for path, snapshot in tasks(store)
                            if snapshot["category"] == "infrastructure_failure")
    assert "Game process exited" in crash["reason"]
    crash_path.unlink()
    await coord.tick(store.require("runs", run["id"]))
    assert not crash_path.exists() and len(tasks(store)) == 1
    assert len(store.list("repair_task_exports")) == 2
    assert store.require("runs", run["id"])["investigation_deadline"] == deadline


@pytest.mark.asyncio
async def test_unconfirmed_teardown_is_a_failure_even_when_stop_was_requested(environment):
    store, coord, _, desktops = environment
    run = await ready_run(environment)
    desktops["worker-1"].fail_stop = True
    await coord.control(run["id"], "terminate", command_id="terminate")
    snapshots = [snapshot for _, snapshot in tasks(store)]
    assert len(snapshots) == 2
    assert next(snapshot for snapshot in snapshots if snapshot["trigger"] == "stop")["expected_event"] is True
    failure = next(snapshot for snapshot in snapshots if snapshot["trigger"] == "teardown_failed")
    assert failure["expected_event"] is False
    assert failure["players"]["player-1"]["teardown_confirmed"] is False
    assert any(event["kind"] == "teardown_result" for event in failure["recent_events"])
    assert store.leases(run["id"]), "Unconfirmed teardown still retains its worker lease"


@pytest.mark.asyncio
async def test_stop_task_key_survives_another_coordinator_finishing(environment):
    store, coord, _, _ = environment
    run = await ready_run(environment)
    record_task(store, run["id"], "stop", "host_lost", key="stop", category="host_lost")
    publish_pending(store)
    [(path, _)] = tasks(store)
    path.unlink()
    second = Coordinator(store, coord.credentials, coord.ai, coord.client_factory)
    await second.finish(run["id"], "host_lost", terminate=True)
    publish_pending(store)
    assert tasks(store) == [] and len(store.list("repair_task_exports")) == 1
    assert store.require("runs", run["id"])["state"] == "Ended"


@pytest.mark.asyncio
async def test_unwritable_inbox_does_not_prevent_termination_and_retries(environment):
    store, coord, _, desktops = environment
    run = await ready_run(environment)
    directory = task_directory(store)
    directory.write_text("A file currently blocks the inbox directory", encoding="utf-8")
    await coord.control(run["id"], "terminate", command_id="terminate")
    assert store.require("runs", run["id"])["state"] == "Ended"
    assert any(path == "/stop" for path, _ in desktops["worker-1"].calls)
    assert len(store.list("repair_task_exports")) == 1
    publish_pending(store)  # Failed retries are equally harmless to the coordinator.
    directory.unlink()
    publish_pending(store)
    [(path, snapshot)] = tasks(store)
    assert snapshot["trigger"] == "stop" and snapshot["run_id"] == run["id"]
    path.unlink()
    publish_pending(store)
    assert tasks(store) == []


@pytest.mark.asyncio
async def test_deleted_task_stays_deleted_after_publication_receipt_failure(environment, monkeypatch):
    store, _, _, _ = environment
    run = await ready_run(environment)
    record_task(store, run["id"], "stop", "host_lost", key="stop", category="host_lost")
    assert tasks(store) == [], "Queueing a snapshot must not publish before lifecycle IO"
    original = store.put

    def fail_receipt(kind, data, key=None):
        if kind == "repair_task_exports" and data["status"] == "published":
            raise OSError("Publication receipt unavailable")
        return original(kind, data, key)

    with monkeypatch.context() as patch:
        patch.setattr(store, "put", fail_receipt)
        publish_pending(store)
    [(path, _)] = tasks(store)
    [record] = store.list("repair_task_exports")
    assert record["status"] == "prepared"
    assert not (task_directory(store) / record["temporary"]).exists()
    path.unlink()  # Watcher finished after rename but before the receipt was committed.
    publish_pending(store)
    assert tasks(store) == []
    assert store.list("repair_task_exports")[0]["status"] == "published"


@pytest.mark.asyncio
@pytest.mark.parametrize("command,worker_path", [("pause", "/pause"), ("investigate", "/pause"), ("terminate", "/stop")])
async def test_tasks_publish_after_all_affected_workers_receive_control(environment, monkeypatch, command, worker_path):
    store, coord, _, desktops = environment
    run = await ready_run(environment, count=2)
    publications = []

    def publish_after_workers(actual_store):
        for worker_id in ("worker-1", "worker-2"):
            assert any(path == worker_path for path, _ in desktops[worker_id].calls)
        publications.append(command)
        publish_pending(actual_store)

    monkeypatch.setattr("ashad.coordinator.publish_pending", publish_after_workers)
    if command == "investigate":
        await coord.investigate(run["id"], "provider_failure", "No decision")
    else:
        await coord.control(run["id"], command, command_id=command)
    assert publications == [command] and len(tasks(store)) == 1
