"""Per-instance issue history: save_before links, the run detail issues list, caps and fallbacks. Fakes and temporary data only."""
import asyncio
from pathlib import Path

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


def test_events_of_kinds_is_newest_first_and_bounded(tmp_path):
    store = Store(tmp_path / "data")
    store.event("r1", "a")
    store.event("r1", "b")
    store.event("r1", "a")
    store.event("r2", "a")
    assert [e["seq"] for e in store.events_of_kinds("r1", ("a",), 10)] == [3, 1]
    assert len(store.events_of_kinds("r1", ("a", "b"), 2)) == 2


def hit(store, run_id, kind, issue_id, **payload):
    store.event(run_id, kind, {"player_id": "player-1", "issue_id": issue_id, "example_id": "e", "source": "agent_signal",
                               "kept": True, "outcome": "kept", **payload})


def test_run_issues_groups_this_runs_hits_only(tmp_path):
    store = Store(tmp_path / "data")
    repair_tasks.record_issue(store, "unsupported_action", basis(), example())
    key = repair_tasks.issue_id("unsupported_action", basis())
    hit(store, "run1", "unsupported_action_recorded", key, save_before="s1")
    hit(store, "run1", "unsupported_action_recorded", key, save_before="s2")
    hit(store, "run2", "crash_recorded", "issue_crash")
    issues, truncated = repair_tasks.run_issues(store, "run1")
    [issue] = issues
    assert issue["issue_id"] == key and issue["hit_count"] == 2
    assert [h["save_before"] for h in issue["hits"]] == ["s2", "s1"]
    assert issue["status"] == "open" and issue["occurrences_total"] == 1
    assert issue["file"] == str(issue_directory(store) / repair_tasks.issue_filename(store.get("issues", key)))
    assert truncated is False
    assert repair_tasks.run_issues(store, "run2")[0][0]["type"] == "crash"
    assert repair_tasks.run_issues(store, "run3") == ([], False)


def test_run_issues_orders_by_latest_hit(tmp_path):
    store = Store(tmp_path / "data")
    for name in ("issue_a", "issue_b", "issue_a"):
        hit(store, "run1", "crash_recorded", name)
    assert [i["issue_id"] for i in repair_tasks.run_issues(store, "run1")[0]] == ["issue_a", "issue_b"]


def test_run_issues_caps_hits_and_flags_truncation(tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    for _ in range(25):
        hit(store, "run1", "crash_recorded", "issue_a")
    [issue], truncated = repair_tasks.run_issues(store, "run1")
    assert len(issue["hits"]) == 20 and issue["hit_count"] == 25
    assert issue["hits"][0]["at"] >= issue["hits"][-1]["at"] and truncated is False
    monkeypatch.setattr(repair_tasks, "RUN_ISSUE_EVENTS", 10)
    [issue], truncated = repair_tasks.run_issues(store, "run1")
    assert issue["hit_count"] == 10 and truncated is True


def test_run_issues_falls_back_for_old_events_and_missing_records(tmp_path):
    store = Store(tmp_path / "data")
    store.event("run1", "game_bug_recorded", {"player_id": "player-1", "issue_id": "issue_gone", "example_id": "e",
                                              "source": "agent_signal", "kept": True, "outcome": "kept"})
    [issue], _ = repair_tasks.run_issues(store, "run1")
    assert issue["hits"][0]["save_before"] is None
    assert issue["title"] == "issue_gone" and issue["status"] == "unknown"
    assert issue["occurrences_total"] is None and issue["file"] is None and issue["type"] == "game_bug"


@pytest.mark.asyncio
async def test_run_detail_lists_issues_with_save_links(environment, monkeypatch, api):
    monkeypatch.setattr(repair_tasks, "MAX_EXAMPLES", 1)
    env = await running(environment, monkeypatch, reject_verbs={"interact"})
    await act(env)
    await act(env)
    response = await call(api, "GET", f"/api/runs/{env.rid}")
    assert response.status_code == 200
    body = response.json()
    [issue] = body["issues"]
    assert issue["type"] == "unsupported_action" and issue["hit_count"] == 2
    assert issue["occurrences_total"] == 2 and issue["status"] == "open" and Path(issue["file"]).exists()
    recorded = events(env, "unsupported_action_recorded")
    save_ids = {s["id"] for s in body["saves"]}
    for hit_row, event in zip(reversed(issue["hits"]), recorded):
        assert hit_row["save_before"] == event["payload"]["save_before"] and hit_row["save_before"] in save_ids
    assert body["issues_truncated"] is False


@pytest.mark.asyncio
async def test_run_detail_without_hits_has_empty_issues(environment, monkeypatch, api):
    env = await running(environment, monkeypatch)
    body = (await call(api, "GET", f"/api/runs/{env.rid}")).json()
    assert body["issues"] == [] and body["issues_truncated"] is False


@pytest.mark.asyncio
async def test_delete_run_drops_history_keeps_issue_file(environment, monkeypatch, api):
    env = await running(environment, monkeypatch, reject_verbs={"interact"})
    await act(env)
    [issue_path] = list(issue_directory(env.store).glob("*.json"))
    await asyncio.create_task(env.coord.finish(env.rid, "user_stopped", terminate=True))
    pending = [t for key, t in env.coord.tasks.items() if key[0] == env.rid]
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    await env.coord.delete_run(env.rid)
    assert repair_tasks.run_issues(env.store, env.rid) == ([], False)
    assert issue_path.exists()
