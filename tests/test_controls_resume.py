"""Completed source reviews survive a later Get game controls failure without becoming configurations."""
import hashlib
import json
from pathlib import Path

import pytest

import ashad.game_controls as controls_module
from ashad.game_controls import GameControls
from ashad.ai import OpenAI, ProviderError
from ashad.claude_backend import ClaudeError
from ashad.store import Store
from test_game_controls import MANUAL, setup


@pytest.mark.asyncio
async def test_failed_later_batch_resumes_from_persisted_notes(setup, monkeypatch):
    store, build, backend, controls = setup
    monkeypatch.setattr(controls_module, "SOURCE_BUDGET", 400)
    backend.write_controls.side_effect = [backend.write_controls.return_value, ClaudeError("connection_failure", "Later batch failed")]
    with pytest.raises(ProviderError, match="Later batch failed"):
        await controls.gather(build["id"])
    assert len(store.list("tuning_notes")) == 1 and not store.list("configurations")
    first_batch = json.loads(backend.write_controls.call_args_list[0].args[0])
    backend.write_controls.reset_mock(side_effect=True)
    reopened = Store(store.root)
    try:
        record = await GameControls(reopened, OpenAI(reopened, backend=controls.ai.backend, claude_backend=backend)).gather(build["id"])
        reviewed = [json.loads(call.args[0])["batch"] for call in backend.write_controls.call_args_list
                    if call.kwargs["stage"] == "source_notes"]
        assert reviewed == list(range(2, first_batch["batch_count"] + 1))
        assert len(reopened.list("tuning_notes")) == first_batch["batch_count"]
        assert GameControls(reopened, None).validated(record["id"])[1]["controls"] == MANUAL
        assert json.loads(backend.write_controls.call_args.args[0])["source_notes"] == [MANUAL] * first_batch["batch_count"]
        assert len(reopened.list("usage")) == 2 + len(backend.write_controls.call_args_list)
    finally:
        reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["evidence", "prompt_version"])
async def test_reuse_requires_matching_evidence_and_prompt_version(setup, monkeypatch, change):
    store, build, claude, controls = setup
    monkeypatch.setattr(controls_module, "SOURCE_BUDGET", 400)
    record = await controls.gather(build["id"])
    batches = controls.validated(record["id"])[1]["coverage"]["source_batches"]
    claude.write_controls.reset_mock()
    await controls.gather(build["id"])
    assert claude.write_controls.await_count == 1 and claude.write_controls.call_args.kwargs["stage"] == "manual"
    claude.write_controls.reset_mock()
    if change == "prompt_version":
        monkeypatch.setattr(controls_module, "TUNING_PROMPT_VERSION", controls_module.TUNING_PROMPT_VERSION + 1)
    else:
        source = Path(build["source_root"])
        project = source / "project.godot"
        project.write_text(project.read_text(encoding="utf-8").replace("next_text", "prev_text"), encoding="utf-8")
        inventory = source.parent / "source-hashes.json"
        hashes = json.loads(inventory.read_text(encoding="utf-8"))
        hashes["project.godot"] = hashlib.sha256(project.read_bytes()).hexdigest()
        inventory.write_text(json.dumps(hashes), encoding="utf-8")
    await controls.gather(build["id"])
    reviewed = [call for call in claude.write_controls.call_args_list if call.kwargs["stage"] == "source_notes"]
    assert len(reviewed) == (1 if change == "evidence" else batches)
    assert all(call.kwargs["model"] == "sonnet" for call in claude.write_controls.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [None, "", " " * 300, "界" * 3000, {"unexpected": "object"}])
async def test_invalid_saved_notes_are_reviewed_again(setup, monkeypatch, invalid):
    store, build, backend, controls = setup
    monkeypatch.setattr(controls_module, "SOURCE_BUDGET", 400)
    await controls.gather(build["id"])
    saved = store.list("tuning_notes")[0]
    store.put("tuning_notes", {**saved, "instructions": invalid})
    backend.write_controls.reset_mock()
    await controls.gather(build["id"])
    assert [call.kwargs["stage"] for call in backend.write_controls.call_args_list] == ["source_notes", "manual"]
    assert store.get("tuning_notes", saved["id"])["instructions"] == MANUAL
