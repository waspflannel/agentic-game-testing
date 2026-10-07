"""Completed source reviews survive a later tuning failure without becoming agents."""
import hashlib
import json
from pathlib import Path

import pytest

import ashad.agent_profiles as profiles_module
from ashad.agent_profiles import AgentProfiles
from ashad.ai import OpenAI, ProviderError
from ashad.codex_backend import CodexError
from ashad.models import MODEL
from ashad.store import Store
from test_agent_profiles import MANUAL, setup


@pytest.mark.asyncio
async def test_failed_later_batch_resumes_from_persisted_notes(setup, monkeypatch):
    store, build, backend, profiles = setup
    monkeypatch.setattr(profiles_module, "SOURCE_BUDGET", 400)
    backend.tune.side_effect = [backend.tune.return_value, CodexError("connection_failure", "Later batch failed")]
    with pytest.raises(ProviderError, match="Later batch failed"):
        await profiles.tune(build["id"])
    assert len(store.list("tuning_notes")) == 1 and not store.list("agents")
    first_batch = json.loads(backend.tune.call_args_list[0].args[0])
    backend.tune.reset_mock(side_effect=True)
    reopened = Store(store.root)
    try:
        profile = await AgentProfiles(reopened, OpenAI(reopened, backend=backend)).tune(build["id"])
        reviewed = [json.loads(call.args[0])["batch"] for call in backend.tune.call_args_list
                    if call.kwargs["stage"] == "source_notes"]
        assert reviewed == list(range(2, first_batch["batch_count"] + 1))
        assert len(reopened.list("tuning_notes")) == first_batch["batch_count"]
        assert profile["instructions"] == MANUAL
        assert json.loads(backend.tune.call_args.args[0])["source_notes"] == [MANUAL] * first_batch["batch_count"]
        assert len(reopened.list("usage")) == 2 + len(backend.tune.call_args_list)
    finally:
        reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["model", "evidence", "prompt_version"])
async def test_reuse_requires_matching_model_evidence_and_prompt_version(setup, monkeypatch, change):
    store, build, backend, profiles = setup
    monkeypatch.setattr(profiles_module, "SOURCE_BUDGET", 400)
    profile = await profiles.tune(build["id"])
    backend.tune.reset_mock()
    await profiles.tune(build["id"])
    assert backend.tune.await_count == 1 and backend.tune.call_args.kwargs["stage"] == "manual"
    backend.tune.reset_mock()
    model = MODEL
    if change == "model":
        model = "gpt-6-sol"
    elif change == "prompt_version":
        monkeypatch.setattr(profiles_module, "TUNING_PROMPT_VERSION", profiles_module.TUNING_PROMPT_VERSION + 1)
    else:
        source = Path(build["source_root"])
        project = source / "project.godot"
        project.write_text(project.read_text(encoding="utf-8").replace("next_text", "prev_text"), encoding="utf-8")
        inventory = source.parent / "source-hashes.json"
        hashes = json.loads(inventory.read_text(encoding="utf-8"))
        hashes["project.godot"] = hashlib.sha256(project.read_bytes()).hexdigest()
        inventory.write_text(json.dumps(hashes), encoding="utf-8")
    await profiles.tune(build["id"], model=model)
    reviewed = [call for call in backend.tune.call_args_list if call.kwargs["stage"] == "source_notes"]
    assert len(reviewed) == (1 if change == "evidence" else profile["source_coverage"]["source_batches"])
    assert all(call.kwargs["model"] == model for call in backend.tune.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [None, "", " " * 300, "界" * 3000, {"unexpected": "object"}])
async def test_invalid_saved_notes_are_reviewed_again(setup, monkeypatch, invalid):
    store, build, backend, profiles = setup
    monkeypatch.setattr(profiles_module, "SOURCE_BUDGET", 400)
    await profiles.tune(build["id"])
    saved = store.list("tuning_notes")[0]
    store.put("tuning_notes", {**saved, "instructions": invalid})
    backend.tune.reset_mock()
    await profiles.tune(build["id"])
    assert [call.kwargs["stage"] for call in backend.tune.call_args_list] == ["source_notes", "manual"]
    assert store.get("tuning_notes", saved["id"])["instructions"] == MANUAL
