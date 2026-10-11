"""Crash and game_bug issue titles and example trimming; temporary data only."""
import httpx
import pytest

from ashad import repair_tasks
from ashad.repair_tasks import _size, issue_title, shrink_example
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
