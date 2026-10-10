"""Tuning prepares pinned prompts through provider doubles; never runs a game or real inference."""
import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ashad.agent_profiles import AgentProfiles, source_evidence
import ashad.agent_profiles as profiles_module
from ashad.ai import OpenAI, ProviderError
from ashad.codex_backend import ApprovalMode, Sandbox, CodexBackend, CodexError
from ashad.prompts import SOURCE_NOTES_LIMIT, TUNING_MANUAL_LIMIT
from ashad.models import MODEL
from ashad.store import Store
from test_codex_backend import Client
from test_decisions import Backend, context


MANUAL = ("Use next_text to reveal or advance cutscene text; inspect the next observation after each press. "
          "Use exit_menu only when the observed UI needs closing, then wait for its transition. "
          "Source: scripts/ui/player_tablet.gd:2 and project.godot:1. Source analysis is not gameplay verification.")


@pytest.fixture
def setup(tmp_path):
    source = tmp_path / "build" / "source"
    files = {"project.godot": '[input]\nnext_text={\n"events": []\n}\n',
             "scripts/ui/player_tablet.gd": 'extends Control\nfunc _input(event):\n    if event.is_action_pressed("exit_menu"):\n        hide()\n'}
    for relative, content in files.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    hashes = {relative: hashlib.sha256((source / relative).read_bytes()).hexdigest() for relative in files}
    (source.parent / "source-hashes.json").write_text(json.dumps(hashes), encoding="utf-8")
    store = Store(tmp_path / "store")
    build = {"id": "build-1", "name": "Test game", "source_root": str(source), "source_revision": "revision-1"}
    store.put("projects", {"id": "project-test", "name": "Test game"})
    build["project_id"] = "project-test"
    store.put("builds", build)
    backend = Backend()
    backend.tune = AsyncMock(return_value={"answers": {"instructions": MANUAL}, "thread_id": "tuning-thread",
        "turn_id": "tuning-turn", "usage": {"input_tokens": 90, "output_tokens": 60}, "usage_scope": "tuning_total"})
    ai = OpenAI(store, backend=backend)
    yield store, build, backend, AgentProfiles(store, ai)
    store.close()


@pytest.mark.asyncio
async def test_tuning_creates_reusable_immutable_manual_with_source_and_usage(setup):
    store, build, backend, profiles = setup
    first = await profiles.tune(build["id"], model="gpt-6-sol")
    second = await profiles.tune(build["id"], model="gpt-6-sol")
    assert first["id"] != second["id"] and profiles.validated(first["id"]) == first
    assert first["build_id"] == build["id"] and first["source_revision"] == "revision-1"
    assert first["instructions"] == MANUAL and first["status"] == "ready"
    evidence = json.loads(backend.tune.call_args.args[0])
    assert evidence["sources"][1]["path"] == "scripts/ui/player_tablet.gd"
    assert 'event.is_action_pressed("exit_menu")' in evidence["sources"][1]["text"]
    assert len(first["source_files"]) == 2 and "text" not in first["source_files"][0]
    assert backend.tune.call_args.kwargs["model"] == "gpt-6-sol" and not backend.calls
    usage = store.list("usage")[0]
    assert usage["purpose"] == "tuning" and usage["billing"] == "subscription" and usage["run_id"] is None
    assert usage["usage"]["output_tokens"] == 60 and usage["usage_scope"] == "tuning_total"
    assert not store.list("runs")


@pytest.mark.asyncio
async def test_complete_manual_above_old_limit_is_saved_without_compression(setup):
    _, build, backend, profiles = setup
    manual = MANUAL * 80
    assert 16000 < len(manual.encode("utf-8")) <= TUNING_MANUAL_LIMIT
    backend.tune.return_value["answers"]["instructions"] = manual
    profile = await profiles.tune(build["id"])
    assert profiles.validated(profile["id"])["instructions"] == manual
    assert backend.tune.await_count == 1


@pytest.mark.asyncio
async def test_all_source_batches_are_analyzed_before_synthesis(setup, monkeypatch):
    store, build, backend, profiles = setup
    monkeypatch.setattr(profiles_module, "SOURCE_BUDGET", 400)
    profile = await profiles.tune(build["id"])
    calls = backend.tune.call_args_list
    assert len(calls) >= 3 and all(call.kwargs["stage"] == "source_notes" for call in calls[:-1])
    final = json.loads(calls[-1].args[0])
    assert len(final["source_notes"]) == len(calls) - 1
    assert final["coverage"]["omitted_files"] == [] and profile["source_coverage"]["source_batches"] == len(calls) - 1
    parts = [source for call in calls[:-1] for source in json.loads(call.args[0])["sources"]]
    expected, _ = source_evidence(build)
    assert parts == expected["sources"]
    assert len(store.list("usage")) == len(calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["source", "revision", "inventory", "manual"])
async def test_stale_or_modified_knowledge_cannot_launch(setup, change):
    store, build, _, profiles = setup
    profile = await profiles.tune(build["id"])
    source = Path(build["source_root"])
    if change == "source":
        (source / "scripts/ui/player_tablet.gd").write_text("changed", encoding="utf-8")
    elif change == "revision":
        store.put("builds", {**build, "source_revision": "revision-2"})
    elif change == "inventory":
        (source.parent / "source-hashes.json").write_text("{}", encoding="utf-8")
    else:
        store.put("agents", {**profile, "instructions": MANUAL + " Changed after tuning."})
    with pytest.raises(ValueError, match="changed|matches|inventory"):
        profiles.validated(profile["id"])


@pytest.mark.asyncio
async def test_source_tampering_is_rejected_before_provider_request(setup):
    store, build, backend, profiles = setup
    (Path(build["source_root"]) / "scripts/ui/player_tablet.gd").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="Pinned source changed"):
        await profiles.tune(build["id"])
    backend.tune.assert_not_awaited()
    assert not store.list("agents")


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", "x" * (TUNING_MANUAL_LIMIT + 1), {"unexpected": "object"}])
async def test_invalid_manual_is_not_saved_and_usage_is_retained(setup, value):
    store, build, backend, profiles = setup
    backend.tune.return_value["answers"]["instructions"] = value
    with pytest.raises(ProviderError, match="controls manual"):
        await profiles.tune(build["id"])
    assert not store.list("agents")
    assert store.list("usage")[0]["status"] == "invalid_tuning"
    assert store.list("usage")[0]["usage"]["output_tokens"] == 60
    assert backend.tune.await_count == (2 if isinstance(value, str) and len(value) > TUNING_MANUAL_LIMIT else 1)
    if backend.tune.await_count == 2:
        request = store.list("provider_requests")[0]
        assert request["tuning_repair"] is True and request["output_bytes"] == TUNING_MANUAL_LIMIT + 1
        assert "after one compression retry" in request["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage,limit", [("source_notes", SOURCE_NOTES_LIMIT), ("manual", TUNING_MANUAL_LIMIT)])
async def test_oversized_utf8_tuning_is_compressed_once_and_both_requests_are_metered(setup, stage, limit):
    store, _, backend, profiles = setup
    draft = "é" * (limit // 2 + 1)
    response = backend.tune.return_value
    backend.tune.side_effect = [{**response, "answers": {"instructions": draft}}, response]
    result = await profiles.ai.tune_agent({"sources": ["original source"]}, stage=stage)
    assert result == MANUAL
    initial, repair = backend.tune.call_args_list
    assert initial.kwargs["repair"] is False and repair.kwargs["repair"] is True
    assert repair.kwargs["stage"] == stage
    assert json.loads(repair.args[0]) == {"draft": draft}
    requests = store.list("provider_requests")
    assert [r["status"] for r in requests] == ["completed", "invalid_tuning"]
    assert requests[1]["output_bytes"] == limit + 2 and requests[1]["output_limit_bytes"] == limit
    assert all(r["tuning_stage"] == stage for r in requests)
    assert len(store.list("usage")) == 2
    assert sum(r["usage"]["output_tokens"] for r in store.list("usage")) == 120


@pytest.mark.asyncio
async def test_valid_size_tuning_with_wrong_schema_is_not_repaired(setup):
    store, _, backend, profiles = setup
    backend.tune.return_value["answers"]["unexpected"] = "extra"
    with pytest.raises(ProviderError, match="expected one instructions string"):
        await profiles.ai.tune_agent({"sources": []}, stage="source_notes")
    assert backend.tune.await_count == 1 and not store.list("agents")


@pytest.mark.asyncio
async def test_tuning_is_fail_fast_when_busy_and_releases_lock_on_cancellation(setup):
    store, build, backend, profiles = setup
    entered = asyncio.Event()
    async def pending(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    backend.tune.side_effect = pending
    task = asyncio.create_task(profiles.tune(build["id"]))
    await entered.wait()
    with pytest.raises(ValueError, match="already being tuned"):
        await profiles.tune(build["id"])
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not profiles.lock.locked() and not store.list("agents")
    assert store.list("usage")[0]["status"] == "request_cancelled"


@pytest.mark.asyncio
async def test_instance_manual_reaches_every_action_selection(setup):
    store, _, backend, profiles = setup
    store.put("runs", {"id": "run-1", "model": MODEL})
    state = context()
    state["game_controls"] = MANUAL
    await profiles.ai.player_turn(state, AsyncMock(return_value={"status": "running"}), lambda: True, "run-1")
    assert len(backend.calls) == 2
    assert all(json.loads(call["input"].split("\nSelected target:")[0])["game_controls"] == MANUAL for call in backend.calls)


@pytest.mark.asyncio
async def test_backend_tuning_uses_existing_subscription_and_sandbox_contract(tmp_path):
    store = Store(tmp_path)
    client = Client()
    result = await client.handle.run()
    result.final_response = json.dumps({"instructions": MANUAL})
    client.handle.run = AsyncMock(return_value=result)
    backend = CodexBackend(store, client=client)
    tuned = await backend.tune("pinned source evidence")
    assert tuned["answers"]["instructions"] == MANUAL and tuned["usage_scope"] == "tuning_total"
    assert client.thread_start.call_args.kwargs["approval_mode"] == ApprovalMode.deny_all
    assert client.thread_start.call_args.kwargs["sandbox"] == Sandbox.read_only
    assert client.thread.turn.call_args.kwargs["output_schema"]["required"] == ["instructions"]
    assert "Never use tools" in client.thread_start.call_args.kwargs["base_instructions"]
    await backend.tune("oversized draft", stage="source_notes", repair=True)
    prompt = client.thread_start.call_args.kwargs["base_instructions"]
    assert "Aim for 5000" in prompt and "hard maximum 8000" in prompt and "Compress it once" in prompt
    client = Client(auth="apiKey")
    with pytest.raises(CodexError, match="subscription"):
        await CodexBackend(store, client=client).tune("evidence")
    client.thread_start.assert_not_awaited()
    store.close()
