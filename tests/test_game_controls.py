"""Get game controls prepares pinned, versioned controls files through provider doubles; never runs a game or real inference."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import ashad.game_controls as controls_module
from ashad.ai import OpenAI, ProviderError
from ashad.claude_backend import ClaudeError
from ashad.game_controls import (CONFIGURATION_DELETED, CONFIGURATION_RETIRED, CONTROLS_FILE_LIMIT, CONTROLS_KEYS,
    RETIRED_REASON, GameControls, controls_file, migrate_tuned_configurations, source_evidence)
from ashad.prompts import SOURCE_NOTES_LIMIT, TUNING_MANUAL_LIMIT
from ashad.models import MODEL
from ashad.store import Store
from test_claude_backend import backend, invocation
from test_controller import FakeClaudeBackend
from test_decisions import Backend, context


MANUAL = ("Use next_text to reveal or advance cutscene text; inspect the next observation after each press. "
          "Use exit_menu only when the observed UI needs closing, then wait for its transition. "
          "Source: scripts/ui/player_tablet.gd:2 and project.godot:1. Source analysis is not gameplay verification.")
RESPONSE = {"answers": {"instructions": MANUAL}, "usage": {"input_tokens": 90, "output_tokens": 60},
            "usage_scope": "controls_total", "thread_id": None, "turn_id": None}


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
    claude = FakeClaudeBackend()
    claude.write_controls = AsyncMock(return_value=json.loads(json.dumps(RESPONSE)))
    ai = OpenAI(store, backend=Backend(), claude_backend=claude)
    yield store, build, claude, GameControls(store, ai)
    store.close()


def stage_of(call):
    return call.kwargs["stage"]


@pytest.mark.asyncio
async def test_get_controls_writes_versioned_file_and_modelless_configuration(setup):
    store, build, claude, controls = setup
    record = await controls.gather(build["id"])
    path = store.root / "controls" / f"{record['id']}.json"
    data = path.read_bytes()
    artifact = json.loads(data)
    assert set(artifact) == CONTROLS_KEYS and artifact["format"] == "ashad.game-controls" and artifact["version"] == 1
    assert artifact["generated_by"] == {"method": "swarm", "provider": "claude", "model": "sonnet", "prompt_version": 2}
    assert artifact["controls"] == MANUAL and artifact["configuration_id"] == record["id"]
    assert len(artifact["source_files"]) == 2 and "text" not in artifact["source_files"][0]
    assert "model" not in record and "instructions" not in record
    assert record["controls_sha256"] == hashlib.sha256(data).hexdigest() and record["status"] == "ready"
    assert store.get("agents", record["id"]) == record and controls.validated(record["id"]) == (record, artifact)
    evidence = json.loads(claude.write_controls.call_args.args[0])
    assert evidence["sources"][1]["path"] == "scripts/ui/player_tablet.gd"
    assert 'event.is_action_pressed("exit_menu")' in evidence["sources"][1]["text"]
    assert claude.write_controls.call_args.kwargs["model"] == "sonnet"
    assert not controls.ai.backend.calls
    usage = store.list("usage")[0]
    assert usage["purpose"] == "tuning" and usage["billing"] == "subscription" and usage["model"] == "sonnet"
    assert usage["usage"]["output_tokens"] == 60 and usage["usage_scope"] == "controls_total"
    assert not store.list("runs")


@pytest.mark.asyncio
async def test_swarm_runs_batches_in_parallel_up_to_four_then_synthesizes(setup, monkeypatch):
    store, build, claude, controls = setup
    monkeypatch.setattr(controls_module, "SOURCE_BUDGET", 400)
    source = Path(build["source_root"])
    hashes = json.loads((source.parent / "source-hashes.json").read_text())
    for index in range(5):  # Each extra script fills its own batch at this budget.
        relative = f"scripts/ui/extra{index}.gd"
        (source / relative).write_text("extends Control\n" + f"func _input_{index}(event):\n    Input.is_action_pressed(\"exit_menu\")  # padding\n" * 3, encoding="utf-8")
        hashes[relative] = hashlib.sha256((source / relative).read_bytes()).hexdigest()
    (source.parent / "source-hashes.json").write_text(json.dumps(hashes), encoding="utf-8")
    expected, _ = source_evidence(build)
    batches = len(controls._batches(expected["sources"]))
    assert batches >= 6
    flight = {"now": 0, "max": 0, "started": 0}
    gate = asyncio.Event()
    response = claude.write_controls.return_value

    async def work(evidence, *, model, stage, repair):
        if stage == "manual":
            assert flight["started"] == batches
            return response
        flight["started"] += 1
        flight["now"] += 1
        flight["max"] = max(flight["max"], flight["now"])
        if flight["now"] == 4:
            gate.set()
        await gate.wait()
        await asyncio.sleep(0)
        flight["now"] -= 1
        return response

    claude.write_controls.side_effect = work
    record = await controls.gather(build["id"])
    calls = claude.write_controls.call_args_list
    assert flight["max"] == 4 and [stage_of(c) for c in calls] == ["source_notes"] * batches + ["manual"]
    assert all(call.kwargs["model"] == "sonnet" for call in calls)
    artifact = controls.validated(record["id"])[1]
    assert artifact["coverage"]["source_batches"] == batches and artifact["coverage"]["workers"] == 4
    assert json.loads(calls[-1].args[0])["source_notes"] == [MANUAL] * batches


@pytest.mark.asyncio
async def test_one_failed_worker_cancels_siblings_saves_nothing_and_keeps_finished_notes(setup, monkeypatch):
    store, build, claude, controls = setup
    monkeypatch.setattr(controls_module, "SOURCE_BUDGET", 400)
    response = claude.write_controls.return_value

    async def work(evidence, *, model, stage, repair):
        batch = json.loads(evidence)["batch"]
        if batch == 1:
            return response
        if batch == 2:
            await asyncio.sleep(0.02)
            raise ClaudeError("rate_limited", "Claude is rate limited")
        await asyncio.sleep(5)
        return response

    claude.write_controls.side_effect = work
    with pytest.raises(ProviderError) as caught:
        await controls.gather(build["id"], request_id="r1")
    assert caught.value.status == "rate_limited"
    assert not store.list("agents") and store.command("tuning", "r1") is None and not (store.root / "controls").exists()
    saved = len(store.list("tuning_notes"))
    assert saved == 1
    claude.write_controls.reset_mock(side_effect=True)
    await controls.gather(build["id"])
    reviewed = [c for c in claude.write_controls.call_args_list if stage_of(c) == "source_notes"]
    assert len(reviewed) == len(controls._batches(source_evidence(build)[0]["sources"])) - saved


@pytest.mark.asyncio
async def test_claude_not_ready_refuses_before_any_request(setup):
    store, build, claude, controls = setup
    claude.ready = False
    with pytest.raises(ValueError, match="uses Claude Sonnet"):
        await controls.gather(build["id"])
    claude.write_controls.assert_not_awaited()
    assert not store.list("agents")


@pytest.mark.asyncio
async def test_complete_manual_above_old_limit_is_saved_without_compression(setup):
    _, build, claude, controls = setup
    manual = MANUAL * 80
    assert 16000 < len(manual.encode("utf-8")) <= TUNING_MANUAL_LIMIT
    claude.write_controls.return_value["answers"]["instructions"] = manual
    record = await controls.gather(build["id"])
    assert controls.validated(record["id"])[1]["controls"] == manual
    assert claude.write_controls.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["source", "revision", "inventory"])
async def test_stale_source_cannot_launch(setup, change):
    store, build, _, controls = setup
    record = await controls.gather(build["id"])
    source = Path(build["source_root"])
    if change == "source":
        (source / "scripts/ui/player_tablet.gd").write_text("changed", encoding="utf-8")
    elif change == "revision":
        store.put("builds", {**build, "source_revision": "revision-2"})
    else:
        (source.parent / "source-hashes.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="Rebuild and get game controls again"):
        controls.validated(record["id"])


def rehash(store, record, artifact=None, **changes):
    path = controls_file(store, record["id"])
    artifact = {**json.loads(path.read_bytes()), **changes} if artifact is None else artifact
    data = json.dumps(artifact, ensure_ascii=False, sort_keys=True, indent=1).encode()
    path.write_bytes(data)
    store.put("agents", {**record, "controls_sha256": hashlib.sha256(data).hexdigest()})


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["flip", "delete", "oversize", "version", "format", "extra key", "short controls"])
async def test_tampered_missing_oversized_or_unknown_file_cannot_launch(setup, damage):
    store, build, _, controls = setup
    record = await controls.gather(build["id"])
    path = controls_file(store, record["id"])
    if damage == "flip":
        data = bytearray(path.read_bytes())
        data[-5] ^= 1
        path.write_bytes(bytes(data))
    elif damage == "delete":
        path.unlink()
    elif damage == "oversize":
        path.write_bytes(path.read_bytes() + b" " * (CONTROLS_FILE_LIMIT + 1 - path.stat().st_size))
    elif damage == "version":
        rehash(store, record, version=2)
    elif damage == "format":
        rehash(store, record, format="x")
    elif damage == "extra key":
        rehash(store, record, extra=1)
    else:
        rehash(store, record, controls="short")
    with pytest.raises(ValueError, match="Get game controls again"):
        controls.validated(record["id"])


def test_deleted_or_retired_configuration_is_refused_with_reconfigure_message(setup):
    store, _, _, controls = setup
    with pytest.raises(ValueError) as caught:
        controls.validated("agent_gone")
    assert str(caught.value) == CONFIGURATION_DELETED
    store.put("agents", {"id": "agent_old", "build_id": "build-1", "status": "retired"})
    with pytest.raises(ValueError) as caught:
        controls.validated("agent_old")
    assert str(caught.value) == CONFIGURATION_RETIRED


@pytest.mark.asyncio
async def test_source_tampering_is_rejected_before_provider_request(setup):
    store, build, claude, controls = setup
    (Path(build["source_root"]) / "scripts/ui/player_tablet.gd").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="Pinned source changed"):
        await controls.gather(build["id"])
    claude.write_controls.assert_not_awaited()
    assert not store.list("agents")


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", "x" * (TUNING_MANUAL_LIMIT + 1), {"unexpected": "object"}])
async def test_invalid_manual_is_not_saved_and_usage_is_retained(setup, value):
    store, build, claude, controls = setup
    claude.write_controls.return_value["answers"]["instructions"] = value
    with pytest.raises(ProviderError, match="controls manual"):
        await controls.gather(build["id"])
    assert not store.list("agents") and not (store.root / "controls").exists()
    assert store.list("usage")[0]["status"] == "invalid_tuning"
    assert store.list("usage")[0]["usage"]["output_tokens"] == 60
    assert claude.write_controls.await_count == (2 if isinstance(value, str) and len(value) > TUNING_MANUAL_LIMIT else 1)
    if claude.write_controls.await_count == 2:
        request = store.list("provider_requests")[0]
        assert request["tuning_repair"] is True and request["output_bytes"] == TUNING_MANUAL_LIMIT + 1
        assert "after one compression retry" in request["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage,limit", [("source_notes", SOURCE_NOTES_LIMIT), ("manual", TUNING_MANUAL_LIMIT)])
async def test_oversized_utf8_controls_are_compressed_once_and_both_requests_are_metered(setup, stage, limit):
    store, _, claude, controls = setup
    draft = "é" * (limit // 2 + 1)
    response = claude.write_controls.return_value
    claude.write_controls.side_effect = [{**response, "answers": {"instructions": draft}}, response]
    result = await controls.ai.write_controls({"sources": ["original source"]}, stage=stage)
    assert result == MANUAL
    initial, repair = claude.write_controls.call_args_list
    assert initial.kwargs["repair"] is False and repair.kwargs["repair"] is True
    assert repair.kwargs["stage"] == stage and repair.kwargs["model"] == "sonnet"
    assert json.loads(repair.args[0]) == {"draft": draft}
    requests = store.list("provider_requests")
    assert [r["status"] for r in requests] == ["completed", "invalid_tuning"]
    assert requests[1]["output_bytes"] == limit + 2 and requests[1]["output_limit_bytes"] == limit
    assert all(r["tuning_stage"] == stage for r in requests)
    assert len(store.list("usage")) == 2
    assert sum(r["usage"]["output_tokens"] for r in store.list("usage")) == 120


@pytest.mark.asyncio
async def test_valid_size_controls_with_wrong_schema_are_not_repaired(setup):
    store, _, claude, controls = setup
    claude.write_controls.return_value["answers"]["unexpected"] = "extra"
    with pytest.raises(ProviderError, match="expected one instructions string"):
        await controls.ai.write_controls({"sources": []}, stage="source_notes")
    assert claude.write_controls.await_count == 1 and not store.list("agents")


@pytest.mark.asyncio
async def test_get_controls_is_fail_fast_when_busy_and_releases_lock_on_cancellation(setup):
    store, build, claude, controls = setup
    entered = asyncio.Event()
    async def pending(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    claude.write_controls.side_effect = pending
    task = asyncio.create_task(controls.gather(build["id"]))
    await entered.wait()
    with pytest.raises(ValueError, match="already being gathered"):
        await controls.gather(build["id"])
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not controls.lock.locked() and not store.list("agents") and not (store.root / "controls").exists()
    assert store.list("usage")[0]["status"] == "request_cancelled"


@pytest.mark.asyncio
async def test_instance_manual_reaches_every_action_selection(setup):
    store, _, _, controls = setup
    backend = controls.ai.backend
    store.put("runs", {"id": "run-1", "model": MODEL})
    state = context()
    state["game_controls"] = MANUAL
    await controls.ai.player_turn(state, AsyncMock(return_value={"status": "running"}), lambda: True, "run-1")
    assert len(backend.calls) == 2
    assert all(json.loads(call["input"].split("\nSelected target:")[0])["game_controls"] == MANUAL for call in backend.calls)


@pytest.mark.asyncio
async def test_claude_write_controls_keeps_tool_free_sandbox_flags(backend):
    await backend.write_controls("pinned source evidence", model="sonnet", stage="source_notes", repair=True)
    recorded = invocation(backend)
    argv = recorded["argv"]
    assert argv[argv.index("--tools") + 1] == "" and argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--permission-prompts") + 1] == "none" and "--strict-mcp-config" in argv
    schema = json.loads(argv[argv.index("--json-schema") + 1])
    assert schema["required"] == ["instructions"]
    prompt = argv[argv.index("--system-prompt") + 1]
    assert "Never use tools" in prompt and "Aim for 5000" in prompt and "Compress it once" in prompt
    assert json.loads(recorded["stdin"])


def test_migration_converts_valid_tuned_configurations_and_retires_invalid_ones(setup):
    store, build, _, controls = setup
    inventory = hashlib.sha256((Path(build["source_root"]).parent / "source-hashes.json").read_bytes()).hexdigest()
    hashes = json.loads((Path(build["source_root"]).parent / "source-hashes.json").read_text())
    files = [{"path": path, "sha256": digest, "total_lines": 4} for path, digest in sorted(hashes.items())]
    legacy = {"id": "agent_valid", "name": "Old", "project_id": "project-test", "build_id": build["id"],
        "source_revision": "revision-1", "model": "gpt-6-sol", "instructions": MANUAL,
        "instructions_sha256": hashlib.sha256(MANUAL.encode()).hexdigest(), "source_files": files,
        "source_inventory_sha256": inventory, "source_coverage": {"scanned_files": 2}, "limitations": ["kept"],
        "created_at": 12.5, "status": "ready"}
    store.put("agents", legacy)
    store.put("agents", {**legacy, "id": "agent_bad", "instructions_sha256": "0" * 64})
    store.put("tuning_notes", {"id": "n1", "build_id": build["id"], "model": "gpt-6-sol", "instructions": MANUAL})
    store.put("tuning_notes", {"id": "n2", "build_id": build["id"], "model": "sonnet", "instructions": MANUAL})
    run = {"id": "run-1", "agent_id": "agent_valid", "agent_profile": dict(legacy)}
    store.put("runs", run)
    migrate_tuned_configurations(store)
    record, artifact = controls.validated("agent_valid")
    assert artifact["generated_by"] == {"method": "legacy_tuning", "provider": "codex", "model": "gpt-6-sol", "prompt_version": None}
    assert artifact["controls"] == MANUAL and artifact["limitations"] == ["kept"] and artifact["created_at"] == 12.5
    assert not {"model", "instructions", "instructions_sha256", "source_files"} & set(record) and record["controls_version"] == 1
    bad = store.get("agents", "agent_bad")
    assert bad["status"] == "retired" and bad["retired_reason"] == RETIRED_REASON and "instructions" not in bad
    assert not controls_file(store, "agent_bad").exists()
    assert [n["id"] for n in store.list("tuning_notes")] == ["n2"]
    assert store.get("runs", "run-1") == run
    agents, stamp = store.list("agents"), controls_file(store, "agent_valid").stat().st_mtime_ns
    migrate_tuned_configurations(store)
    assert store.list("agents") == agents and controls_file(store, "agent_valid").stat().st_mtime_ns == stamp
