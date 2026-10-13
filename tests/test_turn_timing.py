"""Turn timing rows from fake turns; no provider, token, network, Docker or game."""
import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from ashad.ai import ProviderError
from ashad.structured import meaningful_signature, wait_signature
from test_subgoals import decision, runtime


def timings(store):
    return [e["payload"] for e in store.events("run", limit=1000) if e["kind"] == "agent_turn_timing"]


@pytest.mark.asyncio
async def test_decided_turn_records_one_timing_row_with_steps_sizes_and_store_cost(tmp_path):
    store, _, service = runtime(tmp_path)
    await service.turn("run", "p")
    [row] = timings(store)
    assert row["outcome"] == "decided"
    for step in ("observe", "decision_context", "decide", "tool:perform_action"):
        assert row["seconds"][step] >= 0
    assert row["total_seconds"] >= row["seconds"]["decide"]
    assert row["bytes"]["observation"] > 0
    assert row["bytes"]["prompt_observation"] > 0
    assert row["bytes"]["decision_context"] > 0
    assert row["context_truncated"] is False
    assert row["store"]["seconds"] > 0
    assert row["store"]["bytes"] > 0
    assert row["store"]["writes"] > 0
    assert row["gap_seconds"] is None
    assert row["tick_seconds"] is None
    assert row["capture_seconds"] is None
    assert "observation" not in row and "context" not in row
    store.close()


@pytest.mark.asyncio
async def test_second_turn_records_gap_and_generation_change_resets_it(tmp_path):
    store, _, service = runtime(tmp_path)
    await service.turn("run", "p")
    await service.turn("run", "p")
    gap = timings(store)[1]["gap_seconds"]
    assert isinstance(gap, float) and gap >= 0
    run = store.require("runs", "run")
    run["players"]["p"]["generation"] += 1
    store.put("runs", run)
    await service.turn("run", "p")
    assert timings(store)[2]["gap_seconds"] is None
    store.close()


@pytest.mark.asyncio
async def test_planning_turn_counts_attempts_and_validator_rejections(tmp_path):
    store, coordinator, service = runtime(tmp_path)
    coordinator.player_turn = AsyncMock(side_effect=ProviderError("decision_needs_instructions", "Need a prerequisite"))
    await service.turn("run", "p")

    async def respond(context):
        response = decision(store.require("runs", "run")["subgoal_plan"])
        if len(coordinator.planning_contexts) == 1:
            response["evidence"][0]["quote"] = "invented recipe"
        return response

    coordinator.plan_response = respond
    await service.turn("run", "p")
    rows = timings(store)
    assert rows[0]["outcome"] == "provider_error"
    assert rows[1]["outcome"] == "planning"
    assert rows[1]["calls"]["plan_model"] == 2
    assert rows[1]["counts"]["plan_rejected"] == 1
    for step in ("plan_validate", "plan_observe", "plan_rejection_report"):
        assert step in rows[1]["seconds"]
    store.close()


@pytest.mark.asyncio
async def test_poll_exits_record_waiting_pending_and_unchanged_outcomes(tmp_path):
    store, coordinator, service = runtime(tmp_path)
    service.generations[("run", "p")] = 1
    service.waits[("run", "p")] = (time.time() + 30, wait_signature(coordinator.snapshot), 1)
    await service.turn("run", "p")
    assert timings(store)[-1]["outcome"] == "waiting"
    service.waits.clear()
    service.pending[("run", "p")] = "act-1"
    coordinator.result = {"status": "running"}
    await service.turn("run", "p")
    pending = timings(store)[-1]
    assert pending["outcome"] == "pending_action"
    assert "reconcile_pending" in pending["seconds"]
    service.pending.clear()
    coordinator.result = {"status": "completed"}
    service.last_decisions[("run", "p")] = (meaningful_signature(coordinator.snapshot), time.time(), 1)
    await service.turn("run", "p")
    assert timings(store)[-1]["outcome"] == "unchanged"
    store.close()


@pytest.mark.asyncio
async def test_failing_turn_still_records_timing_with_error_outcome(tmp_path):
    store, coordinator, service = runtime(tmp_path)
    coordinator.request = AsyncMock(side_effect=RuntimeError("bridge down"))
    await service.turn("run", "p")
    [row] = timings(store)
    assert row["outcome"] == "error"
    assert coordinator.investigation[1] == "structured_interface_failure"
    store.close()


@pytest.mark.asyncio
async def test_cancelled_turn_records_interrupted_and_still_raises(tmp_path):
    store, coordinator, service = runtime(tmp_path)
    coordinator.player_turn = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await service.turn("run", "p")
    [row] = timings(store)
    assert row["outcome"] == "interrupted"
    store.close()


@pytest.mark.asyncio
async def test_tick_seconds_and_capture_seconds_reach_the_timing_row(tmp_path):
    store, _, service = runtime(tmp_path)
    run = store.require("runs", "run")
    run["players"]["p"]["worker_status"] = {"capture": {"last_seconds": 0.12}}
    store.put("runs", run)
    await service.turn("run", "p", tick_seconds=0.25)
    [row] = timings(store)
    assert row["tick_seconds"] == 0.25
    assert row["capture_seconds"] == 0.12
    store.close()


@pytest.mark.asyncio
async def test_timing_write_failure_never_fails_the_turn(tmp_path, monkeypatch):
    store, coordinator, service = runtime(tmp_path)
    original = store.event

    def failing_event(run_id, kind, payload=None, **fields):
        if kind == "agent_turn_timing":
            raise OSError("disk full")
        return original(run_id, kind, payload, **fields)

    monkeypatch.setattr(store, "event", failing_event)
    await service.turn("run", "p")
    assert len(coordinator.action_contexts) == 1
    store.close()
