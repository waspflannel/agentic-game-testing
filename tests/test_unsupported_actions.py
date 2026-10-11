"""Unsupported-action issue files use temporary stores and desktop/provider doubles only."""
import json

import httpx
import pytest

from ashad import repair_tasks
from ashad.repair_tasks import (_size, issue_directory, issue_room, publish_issues, publish_pending, record_issue,
                                target_label)
from ashad.store import Store


@pytest.fixture(autouse=True)
def no_http_inference(monkeypatch):
    async def unexpected(*args, **kwargs):
        raise AssertionError("Provider tests must not make HTTP requests")
    monkeypatch.setattr(httpx.AsyncClient, "request", unexpected)


def example(**overrides):
    value = {"example_id": "uae_1", "at": "2026-10-11T00:00:00+00:00", "run_id": "run-1", "player_id": "player-1",
        "scenario_id": "scenario-1", "scenario_revision": 1, "model": "model-1", "controller_version": "1",
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
    return {"source": "agent_signal", "tool": "perform_action", "verb": "ui_drag", "blocker": "action_missing",
            "target_kind": "inventory_slot", "target_label": "slot #", "error_code": None, **overrides}


def files(store):
    return sorted(issue_directory(store).glob("*.json"))


def test_repeat_groups_into_one_issue_and_caps_examples(tmp_path):
    store = Store(tmp_path / "data")
    for i in range(12):
        record_issue(store, "unsupported_action", basis(), example(example_id=f"uae_{i}"))
    publish_issues(store)
    [path] = files(store)
    issue = json.loads(path.read_text(encoding="utf-8"))
    assert issue["schema"] == "ashad.issue/1" and issue["occurrences"] == 12 and issue["status"] == "open"
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
    assert issue_room(store, "unsupported_action", basis(verb="ui_click")) == "full"


def test_publish_pending_also_publishes_issues(tmp_path):
    store = Store(tmp_path / "data")
    record_issue(store, "unsupported_action", basis(), example())
    publish_pending(store)
    assert len(files(store)) == 1
