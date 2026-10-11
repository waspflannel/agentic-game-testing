"""Per-instance issue history: save_before links, the run detail issues list, caps and fallbacks. Fakes and temporary data only."""
import pytest

from ashad import repair_tasks
from ashad.repair_tasks import issue_directory
from ashad.store import Store
from test_controller import environment  # noqa: F401
from test_saves import PLAYER, act, api, call, events, issue_files, no_http_inference, running, saves_of  # noqa: F401
from test_unsupported_actions import basis, example


def spy_latest(env, monkeypatch):
    read = []
    real = env.coord.saves.latest

    def spy(run_id):
        found = real(run_id)
        read.append(found and found["id"])
        return found

    monkeypatch.setattr(env.coord.saves, "latest", spy)
    return read


@pytest.mark.asyncio
async def test_kept_and_counted_hits_record_save_before(environment, monkeypatch):
    monkeypatch.setattr(repair_tasks, "MAX_EXAMPLES", 1)
    env = await running(environment, monkeypatch, reject_verbs={"interact"})
    read = spy_latest(env, monkeypatch)
    await act(env)
    await act(env)
    assert len(read) == 2 and None not in read
    recorded = events(env, "unsupported_action_recorded")
    assert [e["payload"]["save_before"] for e in recorded] == read
    assert [e["payload"]["outcome"] for e in recorded] == ["kept", "counted"]
    assert issue_files(env.store)[0]["examples"][0]["saves"]["before"] == read[0]


@pytest.mark.asyncio
async def test_issue_file_seen_carries_save_before(environment, monkeypatch):
    monkeypatch.setattr(repair_tasks, "MAX_EXAMPLES", 1)
    env = await running(environment, monkeypatch, reject_verbs={"interact"})
    read = spy_latest(env, monkeypatch)
    await act(env)
    await act(env)
    [record] = issue_files(env.store)
    assert record["occurrences"] == 2
    assert record["first_seen"]["save_before"] == read[0]
    assert record["last_seen"]["save_before"] == read[1]


def test_hit_without_saves_records_null(tmp_path):
    store = Store(tmp_path / "data")
    repair_tasks.record_issue(store, "unsupported_action", basis(), example())
    key = "issue_" + repair_tasks.issue_signature("unsupported_action", basis())[:16]
    assert store.get("issues", key)["last_seen"]["save_before"] is None


@pytest.mark.asyncio
async def test_full_hit_writes_no_recorded_event(environment, monkeypatch):
    monkeypatch.setattr(repair_tasks, "MAX_ISSUES", 0)
    env = await running(environment, monkeypatch, reject_verbs={"interact"})
    await act(env)
    assert len(events(env, "issue_cap_reached")) == 1
    assert events(env, "unsupported_action_recorded") == []
