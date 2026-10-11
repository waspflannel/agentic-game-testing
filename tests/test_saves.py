"""Instance saves: capture, automatic triggers, issue links, retention, loading and deletion. Fakes and temporary data only."""
import asyncio
import base64
import copy
import hashlib
import io
import itertools
import json
import zipfile
from types import SimpleNamespace

import httpx
import pytest

from ashad import repair_tasks
from ashad.app import create_app
from ashad.coordinator import InstanceConflict
from ashad.instances import Instances, SaveLoadRequest
from ashad.repair_tasks import issue_directory
from ashad.worker_client import WorkerError
from test_acceptance_edges import game_context_worker, ready_run
from test_controller import MemoryCredentials, environment  # noqa: F401
from test_instances import profile  # noqa: F401

PLAYER = "player-1"
CHEST = {"id": "chest-1", "version": 1, "kind": "chest", "label": "Chest", "actions": ["interact", "move"]}


@pytest.fixture(autouse=True)
def no_http_inference(monkeypatch):
    async def unexpected(*args, **kwargs):
        raise AssertionError("Provider tests must not make HTTP requests")
    monkeypatch.setattr(httpx.AsyncClient, "request", unexpected)


def game_zip():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("worlds.save", "w")
    return buffer.getvalue()


def save_worker(monkeypatch, desktop, game_save="confirmed", fail=False, on_capture=None, calls=None):
    """Wrap desktop.request with the worker's save routes; returns the (method, path) call list."""
    original = desktop.request
    calls = [] if calls is None else calls

    async def request(method, path, *, data=None, **kwargs):
        calls.append((method, path))
        if path == "/saves/capture":
            if on_capture:
                on_capture()
            if fail:
                raise OSError("save unavailable")
            if game_save == "none":
                return {"save_id": data["save_id"], "game_save": "none", "detail": "", "sha256": None, "bytes": 0, "archive_base64": ""}
            blob = game_zip()
            return {"save_id": data["save_id"], "game_save": game_save, "detail": "", "sha256": hashlib.sha256(blob).hexdigest(),
                    "bytes": len(blob), "archive_base64": base64.b64encode(blob).decode()}
        if path == "/saves/restore":
            return {"restored": True}
        return await original(method, path, data=data, **kwargs)

    monkeypatch.setattr(desktop, "request", request)
    return calls


async def running(environment, monkeypatch, profile=None, reject_verbs=(), **save_args):
    """A ready single-player run on worker-1 with a fake game observation and the save routes."""
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
    calls = save_worker(monkeypatch, desktop, **save_args)
    for other in desktops.values():  # a loaded run may be allocated to any free worker
        if other is not desktop:
            save_worker(monkeypatch, other, calls=calls, **save_args)
    return SimpleNamespace(store=store, coord=coord, desktop=desktop, run=run, rid=run["id"], observation=observation, calls=calls)


def paths(env, *wanted):
    return [path for _, path in env.calls if path in wanted]


def set_player(env, **values):
    run = env.store.require("runs", env.rid)
    run["players"][PLAYER].update(values)
    env.store.put("runs", run)


async def act(env, verb="interact", target="chest-1"):
    generation = env.store.require("runs", env.rid)["players"][PLAYER]["generation"]
    args = {"verb": verb, "target_id": target, "target_version": 1, "parameters": {}}
    return await env.coord.structured.dispatch(env.rid, PLAYER, "perform_action", args, env.observation, generation)


def inject_result(env, status):
    env.store.event(env.rid, "action_result", {"player_id": PLAYER, "action_id": "injected", "result": {"status": status}})


def age_saves(env, seconds=100):
    for record in env.store.records_for_run("saves", env.rid):
        record["created_at"] -= seconds
        env.store.put("saves", record)


def saves_of(env, trigger=None):
    return [s for s in env.coord.saves.records(env.rid) if trigger in (None, s["trigger"])]


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


def command_rows(store):
    with store.lock:
        return store.db.execute("SELECT scope, id, result FROM commands WHERE scope='instance' ORDER BY id").fetchall()


@pytest.mark.asyncio
async def test_manual_save_records_archive_and_bot_state(environment, monkeypatch, api):
    env = await running(environment, monkeypatch)
    set_player(env, agent_memory="the chest is locked")
    response = await call(api, "POST", f"/api/runs/{env.rid}/saves", command_id="save-1", label="before boss")
    assert response.status_code == 200
    body = response.json()
    assert "state" not in body and body["trigger"] == "manual" and body["trigger_detail"] == {"label": "before boss"}
    record = env.store.require("saves", body["id"])
    state = record["state"]
    run = env.store.require("runs", env.rid)
    assert state["plan"] == run["approved_plan"] and state["agent_memory"] == "the chest is locked"
    assert state["identity"] == run["players"][PLAYER]["identity"] and state["model"] == run["model"]
    assert "subgoal_plan" in state
    archive = env.coord.saves.archive_path(env.rid, record["id"])
    assert archive.is_file() and hashlib.sha256(archive.read_bytes()).hexdigest() == record["game"]["sha256"]
    [created] = events(env, "save_created")
    assert created["payload"]["trigger"] == "manual" and created["payload"]["save_id"] == record["id"]


@pytest.mark.asyncio
async def test_manual_save_repeat_command_id_returns_same_save(environment, monkeypatch, api):
    env = await running(environment, monkeypatch)
    first = await call(api, "POST", f"/api/runs/{env.rid}/saves", command_id="same")
    second = await call(api, "POST", f"/api/runs/{env.rid}/saves", command_id="same")
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert paths(env, "/saves/capture") == ["/saves/capture"]
    assert len(saves_of(env)) == 1


@pytest.mark.asyncio
async def test_manual_save_refused_without_running_game(environment, monkeypatch, api):
    store, coord, plan, desktops = environment
    calls = save_worker(monkeypatch, desktops["worker-1"])
    from test_controller import new_run
    queued = await new_run(environment, copy.deepcopy(plan))
    assert queued["state"] == "Queued"
    refused = await call(api, "POST", f"/api/runs/{queued['id']}/saves", command_id="queued")
    assert refused.status_code == 422 and "Saves need a running game" in refused.json()["detail"]
    await coord.finish(queued["id"], "user_stopped", terminate=True)
    ended = await ready_run(environment)
    await coord.finish(ended["id"], "user_stopped", terminate=True)
    assert store.require("runs", ended["id"])["state"] == "Ended"
    refused = await call(api, "POST", f"/api/runs/{ended['id']}/saves", command_id="ended")
    assert refused.status_code == 422 and "Saves need a running game" in refused.json()["detail"]
    assert not [path for _, path in calls if path == "/saves/capture"]
    assert store.list("saves") == []


@pytest.mark.asyncio
async def test_first_attempt_saves_before_action(environment, monkeypatch):
    env = await running(environment, monkeypatch)
    assert (await act(env))["status"] == "completed"
    assert paths(env, "/saves/capture", "/game") == ["/saves/capture", "/game"]
    [record] = saves_of(env)
    assert record["trigger"] == "risky_action" and record["trigger_detail"]["why"] == "first_attempt"
    assert record["trigger_detail"]["verb"] == "interact" and record["trigger_detail"]["target_kind"] == "chest"
    assert "interact:chest" not in record["state"]["attempted_actions"]  # the save is from before this attempt
    assert "interact:chest" in env.store.require("runs", env.rid)["players"][PLAYER]["attempted_actions"]


@pytest.mark.asyncio
async def test_repeat_after_success_takes_no_save(environment, monkeypatch):
    env = await running(environment, monkeypatch)
    await act(env)
    age_saves(env)
    set_player(env, observation_signature="changed")
    assert (await act(env))["status"] == "completed"
    assert paths(env, "/saves/capture") == ["/saves/capture"]
    assert len(saves_of(env)) == 1


@pytest.mark.asyncio
async def test_action_after_failure_saves(environment, monkeypatch):
    env = await running(environment, monkeypatch)
    await act(env)
    age_saves(env)
    inject_result(env, "failed")
    set_player(env, observation_signature="changed")
    await act(env)
    assert len(paths(env, "/saves/capture")) == 2
    assert saves_of(env)[0]["trigger_detail"]["why"] == "after_failure"


@pytest.mark.asyncio
async def test_unchanged_state_and_throttle_skip(environment, monkeypatch):
    env = await running(environment, monkeypatch)
    await act(env)
    inject_result(env, "failed")
    await act(env)
    assert [e["payload"]["reason"] for e in events(env, "save_skipped")] == ["unchanged"]
    inject_result(env, "failed")
    set_player(env, observation_signature="changed")
    await act(env)
    assert [e["payload"]["reason"] for e in events(env, "save_skipped")] == ["unchanged", "throttled"]
    assert len(paths(env, "/saves/capture")) == 1 and len(saves_of(env)) == 1


@pytest.mark.asyncio
async def test_capture_failure_never_blocks_action(environment, monkeypatch):
    env = await running(environment, monkeypatch, fail=True)
    assert (await act(env))["status"] == "completed"
    assert paths(env, "/saves/capture", "/game") == ["/saves/capture", "/game"]
    [failed] = events(env, "save_failed")
    assert failed["payload"]["error"] == "OSError" and failed["payload"]["trigger"] == "risky_action"
    assert saves_of(env) == []


@pytest.mark.asyncio
async def test_capture_waits_longer_than_the_game_save_budget(environment, monkeypatch):
    env = await running(environment, monkeypatch)
    timeouts = []
    inner = env.desktop.request

    async def request(method, path, *, data=None, **kwargs):
        if path == "/saves/capture":
            timeouts.append(kwargs.get("timeout"))
        return await inner(method, path, data=data, **kwargs)

    monkeypatch.setattr(env.desktop, "request", request)
    assert await env.coord.saves.capture(env.rid, PLAYER, "manual", {})
    # The worker spends up to 2 + 2 + 4 s on the game bridge before zipping the save tree.
    assert timeouts and timeouts[0] > 8


@pytest.mark.asyncio
async def test_control_change_during_save_cancels_action(environment, monkeypatch):
    store = environment[0]

    def pause():
        run = store.require("runs", env.rid)
        run["state"] = "Paused"
        store.put("runs", run)

    env = await running(environment, monkeypatch, on_capture=pause)
    result = await act(env)
    assert result["status"] == "cancelled"
    assert paths(env, "/saves/capture") == ["/saves/capture"] and paths(env, "/game") == []
    assert not events(env, "structured_action_intent")


@pytest.mark.asyncio
async def test_issue_example_links_before_and_at_issue_saves(environment, monkeypatch):
    env = await running(environment, monkeypatch, reject_verbs={"interact"})
    result = await act(env)
    assert result["status"] == "rejected" and result["http_status"] == 422
    [risky] = saves_of(env, "risky_action")
    [at_issue] = saves_of(env, "issue")
    [issue] = issue_files(env.store)
    assert issue["type"] == "unsupported_action"
    assert issue["examples"][0]["saves"] == {"before": risky["id"], "at_issue": at_issue["id"]}
    assert env.store.require("saves", risky["id"])["pinned"] is True
    assert env.store.require("saves", at_issue["id"])["pinned"] is True
    assert at_issue["trigger_detail"]["example_id"] == issue["examples"][0]["example_id"]


@pytest.mark.asyncio
async def test_counted_issue_hit_takes_no_save(environment, monkeypatch):
    monkeypatch.setattr(repair_tasks, "MAX_EXAMPLES", 1)
    seen = []
    real = repair_tasks.record_issue

    def spy(store, issue_type, basis, example):
        outcome = real(store, issue_type, basis, example)
        seen.append((outcome, example))
        return outcome

    monkeypatch.setattr(repair_tasks, "record_issue", spy)
    env = await running(environment, monkeypatch, reject_verbs={"interact"})
    await act(env)
    await act(env)
    assert [outcome for outcome, _ in seen] == ["kept", "counted"]
    assert "saves" in seen[0][1] and "saves" not in seen[1][1]
    assert len(saves_of(env, "issue")) == 1


@pytest.mark.asyncio
async def test_retention_caps(environment, monkeypatch):
    clock = itertools.count(1_000_000, 100)
    monkeypatch.setattr("ashad.saves.time", SimpleNamespace(time=lambda: float(next(clock))))
    env = await running(environment, monkeypatch, reject_verbs={"interact"})
    saves = env.coord.saves
    folder = env.store.run_dir(env.rid) / "saves"

    async def take(trigger, count):
        return [(await saves.capture(env.rid, PLAYER, trigger, {}))["id"] for _ in range(count)]

    risky = await take("risky_action", 3)
    saves.pin(risky[0])
    risky += await take("risky_action", 22)
    kept = [s["id"] for s in saves_of(env, "risky_action")]
    assert len(kept) == 21 and risky[0] in kept and not set(risky[1:5]) & set(kept)
    assert set(kept) - {risky[0]} == set(risky[5:])
    assert not any((folder / old).exists() for old in risky[1:5]) and (folder / risky[0]).is_dir()
    manual = await take("manual", 12)
    assert [s["id"] for s in saves_of(env, "manual")] == manual[:1:-1]
    assert len(list(folder.iterdir())) == len(env.store.records_for_run("saves", env.rid)) == 21 + 10

    await take("issue", 30)
    assert len(saves_of(env, "risky_action")) == 21 and saves.pinned_full(env.rid)
    set_player(env, observation_signature="fresh")
    await act(env)
    [latest_before_issue] = [s for s in saves_of(env, "risky_action") if s["trigger_detail"].get("verb") == "interact"]
    [issue] = issue_files(env.store)
    assert issue["examples"][0]["saves"] == {"before": latest_before_issue["id"], "at_issue": None}
    assert env.store.require("saves", latest_before_issue["id"])["pinned"] is False
    assert [e["payload"]["reason"] for e in events(env, "save_skipped")] == ["pinned_cap"]
    assert len(saves_of(env, "issue")) == 30


async def finished_source(env, **kwargs):
    await env.coord.finish(env.rid, "user_stopped", terminate=True)


@pytest.mark.asyncio
async def test_load_creates_linked_run_with_saved_state(environment, monkeypatch, profile):
    env = await running(environment, monkeypatch, profile)
    await act(env)
    set_player(env, agent_memory="remember the chest")
    saved = await env.coord.saves.capture(env.rid, PLAYER, "manual", {})
    source = env.store.require("runs", env.rid)
    profile["artifact"]["controls"] = "fixed controls"
    await finished_source(env)
    run = await Instances(env.coord).load_save(saved["id"], SaveLoadRequest(request_id="load-1"))
    run = env.store.require("runs", run["id"])
    assert run["rerun_kind"] == "save_load" and run["source_run_id"] == env.rid
    assert run["loaded_save"]["save_id"] == saved["id"] and run["loaded_save"]["source_run_id"] == env.rid
    ps = run["players"][PLAYER]
    assert ps["identity"] == source["players"][PLAYER]["identity"] and run["model"] == source["model"]
    assert run["subgoal_plan"]["run_id"] == run["id"] and run["subgoal_plan"]["run_id"] != env.rid
    assert ps["agent_memory"] == "remember the chest" and "interact:chest" in ps["attempted_actions"]
    assert ps["restored_actions"] == saved["state"]["recent_actions"] and ps["restored_actions"]
    assert "continues from a save" in run["recovery_instruction"]
    assert run["game_controls"]["controls"] == "fixed controls"
    assert saved["state"]["plan"]["configuration_id"] == profile["id"]


async def load_and_admit(env, save_id, request_id):
    run = await Instances(env.coord).load_save(save_id, SaveLoadRequest(request_id=request_id))
    await env.coord.admit(run)
    return env.store.require("runs", run["id"])


@pytest.mark.asyncio
async def test_loaded_run_restores_between_bind_and_launch(environment, monkeypatch, profile):
    env = await running(environment, monkeypatch, profile)
    saved = await env.coord.saves.capture(env.rid, PLAYER, "manual", {})
    await finished_source(env)
    env.calls.clear()
    run = await load_and_admit(env, saved["id"], "load-1")
    order = paths(env, "/bind", "/saves/restore", "/launch")
    assert order == ["/bind", "/saves/restore", "/launch"]
    assert run["state"] == "Running" or run["state"] == "Starting"
    assert run["players"][PLAYER]["restored"] is True
    host = run["plan"]["players"][0]
    await env.coord.start_player(run, host)
    assert paths(env, "/saves/restore") == ["/saves/restore"]

    await env.coord.finish(run["id"], "user_stopped", terminate=True)
    second = await Instances(env.coord).load_save(saved["id"], SaveLoadRequest(request_id="load-2"))
    env.coord.saves.archive_path(env.rid, saved["id"]).write_bytes(b"changed")
    await env.coord.admit(second)
    ended = env.store.require("runs", second["id"])
    assert ended["state"] == "Ended" and ended["ending_reason"] == "infrastructure_failure"
    assert any("Saved game archive changed or is missing" in text for text in ended["limitations"])
    assert paths(env, "/saves/restore") == ["/saves/restore"]


@pytest.mark.asyncio
async def test_load_none_game_save_skips_restore(environment, monkeypatch, profile):
    env = await running(environment, monkeypatch, profile, game_save="none")
    saved = await env.coord.saves.capture(env.rid, PLAYER, "manual", {})
    assert saved["game"]["status"] == "none"
    assert not env.coord.saves.archive_path(env.rid, saved["id"]).exists()
    await finished_source(env)
    env.calls.clear()
    run = await load_and_admit(env, saved["id"], "load-none")
    assert paths(env, "/saves/restore") == []
    assert paths(env, "/bind", "/launch") == ["/bind", "/launch"]
    [restored] = events(env, "save_restored", run["id"])
    assert restored["payload"]["skipped"] is True and restored["payload"]["save_id"] == saved["id"]


@pytest.mark.asyncio
async def test_load_refusals_and_idempotency(environment, monkeypatch, profile):
    env = await running(environment, monkeypatch, profile)
    store = env.store
    saved = await env.coord.saves.capture(env.rid, PLAYER, "manual", {})
    instances = Instances(env.coord)

    def snapshot():
        return len(store.list("runs")), command_rows(store)

    async def refused(error, pattern=None):
        before = snapshot()
        with pytest.raises(error, match=pattern):
            await instances.load_save(saved["id"], SaveLoadRequest(request_id="refused"))
        assert snapshot() == before

    await refused(InstanceConflict)
    await finished_source(env)

    profile["build_id"] = "build-other"
    await refused(ValueError, "build no longer matches")
    profile["build_id"] = "build-1"

    original = store.require("saves", saved["id"])
    for change, pattern in ((lambda s: s["state"].update(transport="steam"), "Steam"),
                            (lambda s: s["state"]["plan"]["players"].append(copy.deepcopy(s["state"]["plan"]["players"][0])), "single-player")):
        broken = copy.deepcopy(original)
        change(broken)
        store.put("saves", broken)
        await refused(ValueError, pattern)
        store.put("saves", original)

    archive = env.coord.saves.archive_path(env.rid, saved["id"])
    good = archive.read_bytes()
    archive.write_bytes(b"changed")
    await refused(ValueError, "Saved game archive changed or is missing")
    archive.write_bytes(good)

    first = await instances.load_save(saved["id"], SaveLoadRequest(request_id="same-request"))
    again = await instances.load_save(saved["id"], SaveLoadRequest(request_id="same-request"))
    assert first["id"] == again["id"]
    assert len([r for r in store.list("runs") if r.get("rerun_kind") == "save_load"]) == 1


@pytest.mark.asyncio
async def test_delete_run_removes_saves(environment, monkeypatch, api):
    env = await running(environment, monkeypatch, reject_verbs={"interact"})
    await act(env)
    manual = await call(api, "POST", f"/api/runs/{env.rid}/saves", command_id="gone")
    assert manual.status_code == 200 and saves_of(env)
    [issue_path] = list(issue_directory(env.store).glob("*.json"))
    issue_bytes = issue_path.read_bytes()
    folder = env.store.run_dir(env.rid) / "saves"
    assert folder.is_dir() and env.store.command("save:" + env.rid, "gone")
    await asyncio.create_task(env.coord.finish(env.rid, "user_stopped", terminate=True))  # finishing tracks its own task
    pending = [t for key, t in env.coord.tasks.items() if key[0] == env.rid]
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    await env.coord.delete_run(env.rid)
    assert env.store.records_for_run("saves", env.rid) == [] and env.store.list("saves") == []
    assert not folder.exists()
    assert env.store.command("save:" + env.rid, "gone") is None
    assert issue_path.read_bytes() == issue_bytes


@pytest.mark.asyncio
async def test_decision_context_prepends_restored_actions(environment, monkeypatch):
    env = await running(environment, monkeypatch)
    player = env.run["plan"]["players"][0]
    restored = [{"id": f"old-{i}", "kind": "decision_selected", "payload": {"player_id": PLAYER, "n": i}} for i in range(15)]
    for i in range(10):
        env.store.event(env.rid, "decision_selected", {"player_id": PLAYER, "n": 100 + i})
    set_player(env, restored_actions=restored)
    context = env.coord.structured.decision_context(env.store.require("runs", env.rid), player, env.observation)
    own = context["recent_own_actions"]
    assert len(own) == 20 and own[:10] == restored[5:] and [e["payload"]["n"] for e in own[10:]] == list(range(100, 110))

    set_player(env, restored_actions=restored[:3])
    own = env.coord.structured.decision_context(env.store.require("runs", env.rid), player, env.observation)["recent_own_actions"]
    assert own[:3] == restored[:3] and len(own) == 13


@pytest.mark.asyncio
async def test_end_to_end_risky_save_issue_link_and_load(environment, monkeypatch, profile):
    env = await running(environment, monkeypatch, profile, reject_verbs={"interact"})
    await act(env, verb="move")
    age_saves(env)
    set_player(env, agent_memory="found a chest", observation_signature="later")
    env.calls.clear()
    result = await act(env)  # first attempt of interact:chest; the worker answers 422
    assert result["status"] == "rejected"
    # 1. the first-attempt action is preceded by a capture.
    assert paths(env, "/saves/capture", "/game")[:2] == ["/saves/capture", "/game"]
    [risky] = [s for s in saves_of(env, "risky_action") if s["trigger_detail"]["verb"] == "interact"]
    assert risky["trigger_detail"]["why"] == "first_attempt"
    # 2. the 422 produced an unsupported_action example whose saves.before is that save.
    [issue] = issue_files(env.store)
    assert issue["type"] == "unsupported_action"
    assert issue["examples"][0]["saves"]["before"] == risky["id"]
    # 3. load_save creates a save_load run.
    await finished_source(env)
    env.calls.clear()
    run = await load_and_admit(env, risky["id"], "e2e")
    assert run["rerun_kind"] == "save_load" and run["source_run_id"] == env.rid
    # 4. start_player calls /bind, /saves/restore, /launch in that order.
    assert paths(env, "/bind", "/saves/restore", "/launch") == ["/bind", "/saves/restore", "/launch"]
    # 5. decision_context carries the saved plan, memory and recent actions first.
    saved = env.store.require("saves", risky["id"])["state"]
    context = env.coord.structured.decision_context(run, run["plan"]["players"][0], env.observation)
    assert saved["subgoal_plan"] and context["subgoal_plan"]["goals"]
    assert [g["objective"] for g in context["subgoal_plan"]["goals"]] == [g["objective"] for g in saved["subgoal_plan"]["goals"]]
    assert context["memory"] == "found a chest"
    assert saved["recent_actions"] and context["recent_own_actions"][:len(saved["recent_actions"])] == saved["recent_actions"]


@pytest.mark.asyncio
async def test_run_detail_lists_save_summaries(environment, monkeypatch, api):
    env = await running(environment, monkeypatch)
    first = await env.coord.saves.capture(env.rid, PLAYER, "manual", {"label": "a"})
    second = await env.coord.saves.capture(env.rid, PLAYER, "manual", {"label": "b"})
    response = await call(api, "GET", f"/api/runs/{env.rid}")
    assert response.status_code == 200
    listed = response.json()["saves"]
    assert [s["id"] for s in listed] == [second["id"], first["id"]]
    assert all("state" not in s and s["model"] == env.run["model"] for s in listed)


@pytest.mark.asyncio
async def test_risky_save_holds_bot_state_from_before_the_attempt(environment, monkeypatch, profile):
    env = await running(environment, monkeypatch, profile)
    await act(env)  # first interact on the chest: saved first, then recorded as attempted
    [risky] = saves_of(env, "risky_action")
    assert "interact:chest" not in risky["state"]["attempted_actions"]
    assert "interact:chest" in env.store.require("runs", env.rid)["players"][PLAYER]["attempted_actions"]
    await finished_source(env)
    run = await Instances(env.coord).load_save(risky["id"], SaveLoadRequest(request_id="load-pre"))
    loaded = env.store.require("runs", run["id"])["players"][PLAYER]
    assert "interact:chest" not in loaded["attempted_actions"]  # the retry is a first attempt again, so it saves first


@pytest.mark.asyncio
async def test_restore_waits_longer_than_the_default_timeout(environment, monkeypatch, profile):
    env = await running(environment, monkeypatch, profile)
    saved = await env.coord.saves.capture(env.rid, PLAYER, "manual", {})
    await finished_source(env)
    timeouts = []
    for desktop in environment[3].values():
        inner = desktop.request

        async def request(method, path, *, data=None, _inner=inner, **kwargs):
            if path == "/saves/restore":
                timeouts.append(kwargs.get("timeout"))
            return await _inner(method, path, data=data, **kwargs)

        monkeypatch.setattr(desktop, "request", request)
    await load_and_admit(env, saved["id"], "load-timeout")
    assert timeouts and timeouts[0] > 8
