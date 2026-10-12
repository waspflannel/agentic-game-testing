"""Per-instance issue history: the run detail issues list, caps and fallbacks. Fakes and temporary data only."""
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from ashad import repair_tasks
from ashad.app import create_app
from ashad.repair_tasks import issue_directory
from ashad.store import Store
from ashad.worker_client import WorkerError
from test_acceptance_edges import game_context_worker, ready_run
from test_controller import MemoryCredentials, environment  # noqa: F401
from test_unsupported_actions import basis, example

PLAYER = "player-1"
CHEST = {"id": "chest-1", "version": 1, "kind": "chest", "label": "Chest", "actions": ["interact", "move"]}


@pytest.fixture(autouse=True)
def no_http_inference(monkeypatch):
    async def unexpected(*args, **kwargs):
        raise AssertionError("Provider tests must not make HTTP requests")
    monkeypatch.setattr(httpx.AsyncClient, "request", unexpected)


async def running(environment, monkeypatch, profile=None, reject_verbs=()):
    """A ready single-player run on worker-1 with a fake game observation."""
    store, coord, plan, desktops = environment
    plan = copy.deepcopy(plan)
    if profile:
        plan["configuration_id"] = profile["id"]
    run = await ready_run(environment, plan)
    desktop = desktops["worker-1"]
    observation = game_context_worker(monkeypatch, desktop, run)
    observation["entities"] = [copy.deepcopy(CHEST)]
    inner = desktop.request

    async def request(method, path, *, data=None, **kwargs):
        if path == "/game" and data["operation"] == "perform_action" and data["arguments"]["verb"] in reject_verbs:
            raise WorkerError("bad target", 422, ambiguous=False, code="invalid_target")
        return await inner(method, path, data=data, **kwargs)

    monkeypatch.setattr(desktop, "request", request)
    return SimpleNamespace(store=store, coord=coord, desktop=desktop, run=run, rid=run["id"], observation=observation)


async def act(env, verb="interact", target="chest-1"):
    generation = env.store.require("runs", env.rid)["players"][PLAYER]["generation"]
    args = {"verb": verb, "target_id": target, "target_version": 1, "parameters": {}}
    return await env.coord.structured.dispatch(env.rid, PLAYER, "perform_action", args, env.observation, generation)


def events(env, kind, run_id=None):
    return [e for e in env.store.events(run_id or env.rid, limit=2000) if e["kind"] == kind]


def issue_files(store):
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(issue_directory(store).glob("*.json"))]


@pytest.fixture
def api(environment):
    store, coord, _, _ = environment
    app = create_app(store.root, MemoryCredentials(None), start_services=False)
    app.state.coordinator.client_factory = coord.client_factory
    yield app
    app.state.store.close()


async def call(app, method, path, **body):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        request = client.build_request(method, path, json=body or None, headers={"X-ASHAD-Request": "1"})
        return await client.send(request)


@pytest.mark.asyncio
async def test_action_and_issue_take_no_save(environment, monkeypatch):
    monkeypatch.setattr(repair_tasks, "MAX_EXAMPLES", 1)
    env = await running(environment, monkeypatch, reject_verbs={"interact"})
    await act(env)
    await act(env)
    assert not any(path.startswith("/saves/") for path, _ in env.desktop.calls)
    assert any(path == "/pin" for path, _ in env.desktop.calls)
    [record] = issue_files(env.store)
    assert "saves" not in record["examples"][0]
    assert "save_before" not in record["first_seen"] and "save_before" not in record["last_seen"]
    recorded = events(env, "unsupported_action_recorded")
    assert all("save_before" not in e["payload"] for e in recorded)
    assert [e["payload"]["outcome"] for e in recorded] == ["kept", "counted"]


@pytest.mark.asyncio
async def test_decision_context_ignores_old_restored_actions(environment, monkeypatch):
    env = await running(environment, monkeypatch)
    run = env.store.require("runs", env.rid)
    run["players"][PLAYER]["restored_actions"] = [{"kind": "action_result", "payload": {"player_id": PLAYER, "marker": "old"}}]
    env.store.put("runs", run)
    context = env.coord.structured.decision_context(run, run["plan"]["players"][0], env.observation)
    assert "old" not in json.dumps(context["recent_own_actions"])


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
    # Ledger events written before save/load was removed still carry save_before; hits no longer expose it.
    hit(store, "run1", "unsupported_action_recorded", key, save_before="s1")
    hit(store, "run1", "unsupported_action_recorded", key, save_before=None)
    hit(store, "run2", "crash_recorded", "issue_crash")
    issues, truncated = repair_tasks.run_issues(store, "run1")
    [issue] = issues
    assert issue["issue_id"] == key and issue["hit_count"] == 2
    assert len(issue["hits"]) == 2 and all("save_before" not in h for h in issue["hits"])
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
    assert "save_before" not in issue["hits"][0]
    assert issue["title"] == "issue_gone" and issue["status"] == "unknown"
    assert issue["occurrences_total"] is None and issue["file"] is None and issue["type"] == "game_bug"


@pytest.mark.asyncio
async def test_run_detail_lists_issues(environment, monkeypatch, api):
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
    assert [h["kept"] for h in issue["hits"]] == [False, True]
    assert all("save_before" not in h for h in issue["hits"])
    assert "saves" not in body
    assert body["issues_truncated"] is False


@pytest.mark.asyncio
async def test_run_detail_without_hits_has_empty_issues(environment, monkeypatch, api):
    env = await running(environment, monkeypatch)
    body = (await call(api, "GET", f"/api/runs/{env.rid}")).json()
    assert body["issues"] == [] and body["issues_truncated"] is False


@pytest.mark.asyncio
async def test_delete_run_drops_history_keeps_issue_file(environment, monkeypatch):
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
