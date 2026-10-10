"""Contract tests use doubles for failure paths, never as gameplay evidence."""
import asyncio
import copy
import hashlib
import json
import time
from unittest.mock import AsyncMock
import pytest
from fastapi.testclient import TestClient
from ashad.app import create_app
from ashad.ai import OpenAI
from ashad.claude_backend import ClaudeError
from ashad.codex_backend import CodexError
from ashad.coordinator import Coordinator
from ashad.models import MODEL, SUPPORTED_MODELS, CLAUDE_MODELS, Plan
from ashad.store import Store
from ashad.validation import validate_plan
from ashad.watchdog import once
from ashad.workspace_environment import CONTRACT, ENGINE


class MemoryCredentials:
    def __init__(self, key="test-key"):
        self.values = {"openai": key} if key else {}
    def get(self, reference="openai"):
        return self.values.get(reference)
    def save(self, reference, value):
        self.values[reference] = value
    def remove(self, reference="openai"):
        self.values.pop(reference, None)


class FakeCodexBackend:
    """Subscription double: no local Codex process, account, or live model calls."""
    def __init__(self, ready=True):
        self.ready = ready
        self.requests = []

    async def status(self, model=MODEL):
        return {"configured": self.ready, "status": "ready" if self.ready else "login_required",
                "api": "codex", "auth_method": "chatgpt", "model": model,
                "available_models": list(SUPPORTED_MODELS),
                "message": "Subscription ready" if self.ready else "Sign in to Codex with ChatGPT"}

    async def select(self, input, questions, model=MODEL):
        if not self.ready:
            raise CodexError("login_required", "Sign in to Codex with ChatGPT")
        self.requests.append({"input": input, "questions": copy.deepcopy(questions), "model": model})
        return {"answers": {q["name"]: q["choices"][0]["value"] for q in questions},
                "usage": None, "usage_scope": "selection_total", "thread_id": "test-thread", "turn_id": "test-turn"}

    async def close(self):
        pass


class FakeClaudeBackend:
    """Claude subscription double: no CLI process, token store, or live model calls."""
    def __init__(self, ready=True):
        self.ready = ready
        self.requests = []
        self.saved = []
        self.cleared = 0

    async def status(self, model="sonnet"):
        return {"configured": self.ready, "status": "ready" if self.ready else "configuration_required",
                "api": "claude", "auth_method": "claude_oauth_token", "billing": "subscription", "model": model,
                "available_models": list(CLAUDE_MODELS),
                "message": "Subscription ready" if self.ready else "Paste a token from claude setup-token"}

    async def select(self, input, questions, *, model="sonnet"):
        if not self.ready:
            raise ClaudeError("configuration_required", "Paste a token from claude setup-token")
        self.requests.append({"input": input, "questions": copy.deepcopy(questions), "model": model})
        return {"answers": {q["name"]: q["choices"][0]["value"] for q in questions},
                "usage": None, "usage_scope": "selection_total", "thread_id": "test-thread", "turn_id": "test-turn"}

    async def tune(self, evidence, *, model="sonnet", stage="manual", repair=False):
        if not self.ready:
            raise ClaudeError("configuration_required", "Paste a token from claude setup-token")
        return {"answers": {"instructions": "x" * 300}, "usage": None, "usage_scope": "tuning_total",
                "thread_id": "test-thread", "turn_id": "test-turn"}

    def save_token(self, token):
        self.saved.append(token)
        self.ready = True

    def clear_token(self):
        self.cleared += 1
        self.ready = False

    async def close(self):
        pass


class DesktopDouble:
    def __init__(self, worker, build):
        self.worker = worker
        self.calls = []
        self.fail_stop = False
        self.bound = None
        self.status = {"worker_id": worker["id"], "contract_version": CONTRACT, "independent_desktop": True,
            "lifecycle": "unbound", "binding": None, "game_running": False, "world_may_continue": False,
            "human_control": False, "held_keys": [], "held_buttons": [], "infrastructure_error": None,
            "steam": {"setup_active": False, "authenticated": False, "identity_verified": False,
                      "app_id_verified": False, "license_verified": False, "requires_human_setup": True}}
        self.status["build"] = {**copy.deepcopy(build), "executable_sha256": build["engine_sha256"]}
        # This fixture retains its own measured pins; it never echoes requested pins as evidence.
        self.probe = {**copy.deepcopy(self.status), "ready": True, "build": copy.deepcopy(build),
                      "desktop": {"rendered": True, "capture_ready": True, "input_ready": True}}
    async def request(self, method, path, *, data=None, **kwargs):
        self.calls.append((path, data))
        if path == "/prepare/probe":
            assert method == "POST" and set(data) == {"build"}
            return copy.deepcopy(self.probe)
        if path == "/status":
            assert method == "GET"
            status = copy.deepcopy(self.status)
            if self.bound:
                status.update(lifecycle="running", binding=self.bound, game_running=True, world_may_continue=True)
            return status
        if path == "/bind":
            self.bound = data
        if path == "/stop" and self.fail_stop:
            raise OSError("Unavailable")
        if path == "/stop":
            self.bound = None
            return {"teardown_confirmed": True}
        if path == "/game":
            return {"state": "completed"}
        return {"ok": True}


@pytest.fixture
def environment(tmp_path):
    store = Store(tmp_path / "data")
    store.put("settings", {"id": "main", "concurrent_sessions": 1, "storage_reserve_bytes": 1})
    source = tmp_path / "source"
    source.mkdir()
    (source / "quest.gd").write_text("# terminal demo_wrapup_moar\n", encoding="utf-8")
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"goals": [{"id": "demo_wrapup_moar"}, {"id": "first_quota"}], "unavailable_goals": [{"id": "forest"}], "items": [{"id": "flour"}]}))
    pinned = tmp_path / "pinned-build"
    pinned.mkdir()
    engine, pack = pinned / "fixture-engine", pinned / "fixture.pck"
    engine.write_bytes(b"FIXTURE ONLY engine bytes; never executed")
    pack.write_bytes(b"FIXTURE ONLY pack bytes; never gameplay")
    build = {"id": "build-1", "project_id": "project-1", "name": "test fixture build", "source_root": str(source),
             "source_revision": "test-revision", "catalog_path": str(catalog),
             "command": ["/game/fixture-engine", "--main-pack", "/game/fixture.pck"], "artifacts": [],
             "steam_app_id": 3753020, "structured_version": 1, "integration_version": 2, "engine_version": ENGINE,
             "engine_sha256": hashlib.sha256(engine.read_bytes()).hexdigest(),
             "artifact_sha256": hashlib.sha256(pack.read_bytes()).hexdigest()}
    manifest = pinned / "manifest.json"
    manifest.write_text(json.dumps(build), encoding="utf-8")
    build.update(manifest_path=str(manifest), manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest())
    store.put("projects", {"id": "project-1", "name": "Test game"})
    store.put("builds", build)
    desktops = {}
    for i in range(1, 5):
        worker = {"id": f"worker-{i}", "name": f"Worker {i}", "url": "http://127.0.0.1:8800", "credential_ref": f"worker:{i}", "host_address": f"ashad-worker-{i}", "enabled": True}
        store.put("workers", worker)
        desktops[worker["id"]] = DesktopDouble(worker, build)
    credentials = MemoryCredentials(None)
    backend = FakeCodexBackend()
    store.put("settings", {"configured": True, "status": "ready", "api": "codex",
                          "auth_method": "chatgpt", "model": MODEL,
                          "available_models": list(SUPPORTED_MODELS)}, "ai_status")
    coord = Coordinator(store, credentials, OpenAI(store, backend=backend), lambda worker: desktops[worker["id"]])
    plan = Plan().model_dump()
    plan["build_id"] = "build-1"
    plan["players"][0]["worker_id"] = "worker-1"
    yield store, coord, plan, desktops
    store.close()


async def new_run(env, plan=None):
    store, coord, original, _ = env
    scenario = coord.save_scenario(plan or original)
    run = await coord.launch(scenario["id"], scenario["revision"], "launch-" + scenario["id"])
    return run


def test_schema_rejects_duplicates_hosts_cycles_and_no_late_join(environment):
    store, _, plan, _ = environment
    for mutation in (lambda p: p["players"].append(copy.deepcopy(p["players"][0])), lambda p: p["players"][0].update(role="joiner"), lambda p: p["players"].append({**p["players"][0], "id": "p2", "role": "joiner", "worker_id": "worker-2", "initial": False})):
        changed = copy.deepcopy(plan)
        mutation(changed)
        assert not validate_plan(changed, store)["valid"]
    plan["events"] = [{"id": "e1", "actor": "player-1", "action": "disconnect", "trigger": "event", "value": "e2"}, {"id": "e2", "actor": "player-1", "action": "rejoin", "trigger": "event", "value": "e1"}]
    assert not validate_plan(plan, store)["valid"]


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_all_player_counts_are_launchable(environment, count):
    store, _, plan, _ = environment
    host = plan["players"][0]
    plan["players"] = [{**host, "id": f"p{i}", "name": f"P{i}", "role": "host" if i == 1 else "joiner", "worker_id": f"worker-{i}"} for i in range(1, count + 1)]
    assert validate_plan(plan, store)["valid"]


def test_unreachable_source_mechanic_rejected(environment):
    store, _, plan, _ = environment
    plan["players"][0]["goal_id"] = "forest"
    assert not validate_plan(plan, store)["valid"]


@pytest.mark.asyncio
async def test_subscription_login_controls_launch_without_an_api_key(environment):
    store, coord, plan, _ = environment
    scenario = coord.save_scenario(plan)
    assert scenario["revision"] == 1
    assert coord.credentials.get() is None
    assert validate_plan(plan, store, coord.credentials, True)["valid"]
    coord.ai.backend.ready = False
    await coord.ai.validate()
    assert validate_plan(plan, store)["valid"], "Drafts remain editable while signed out"
    assert not validate_plan(plan, store, coord.credentials, True)["valid"]
    with pytest.raises(ValueError):
        await coord.launch(scenario["id"], scenario["revision"], "signed-out-launch")
    assert store.list("runs") == []


@pytest.mark.asyncio
async def test_launch_idempotency_and_immutable_revisions(environment):
    store, coord, plan, desktops = environment
    scenario = coord.save_scenario(plan)
    original = copy.deepcopy(scenario)
    plan["title"] = "Edited"
    edited = coord.save_scenario(plan, scenario["id"])
    assert edited["revision"] == 2
    assert store.require("scenarios", f"{scenario['id']}:1") == original
    run = await coord.launch(scenario["id"], 1, "same")
    again = await coord.launch(scenario["id"], 1, "same")
    assert run["id"] == again["id"]
    assert not any(path in {"/bind", "/launch"} for d in desktops.values() for path, _ in d.calls)  # Read-only preflight is allowed; draft never launches.
    assert run["approved_plan"]["title"] != "Edited"


@pytest.mark.asyncio
async def test_capacity_reserves_legacy_late_players_but_rejects_second_instance(environment):
    store, coord, plan, desktops = environment
    plan["players"].append({**plan["players"][0], "id": "p2", "role": "joiner", "worker_id": "worker-2", "initial": False})
    plan["events"] = [{"id": "late", "actor": "p2", "action": "join", "trigger": "elapsed", "value": 60}]
    first = await new_run(environment, plan)
    await coord.admit(first)
    assert {l["resource_id"] for l in store.leases(first["id"])} >= {"worker-1", "worker-2"}
    assert not any(path in {"/bind", "/launch", "/action"} for path, _ in desktops["worker-2"].calls)
    with pytest.raises(ValueError, match="One active instance"):
        await new_run(environment)
    assert len(store.list("runs")) == 1


@pytest.mark.asyncio
async def test_investigation_repeated_alert_deadline_and_session_scope(environment):
    store, coord, _, desktops = environment
    run = await new_run(environment)
    await coord.admit(run)
    await coord.investigate(run["id"], "progression_blocker", "valid action failed")
    deadline = store.require("runs", run["id"])["investigation_deadline"]
    assert 1790 < deadline - time.time() <= 1800
    await coord.investigate(run["id"], "progression_blocker", "same alert")
    assert store.require("runs", run["id"])["investigation_deadline"] == deadline
    assert not desktops["worker-2"].calls
    await coord.control(run["id"], "extend", "More evidence", "extend-1", seconds=60)
    assert store.require("runs", run["id"])["investigation_deadline"] == deadline + 60
    await coord.control(run["id"], "extend", "retry", "extend-1", seconds=60)
    assert store.require("runs", run["id"])["investigation_deadline"] == deadline + 60


@pytest.mark.asyncio
async def test_pause_has_no_new_deadline_resume_cancels_investigation(environment):
    store, coord, _, _ = environment
    run = await new_run(environment)
    await coord.admit(run)
    await coord.control(run["id"], "pause", "inspect", "pause")
    assert store.require("runs", run["id"])["investigation_deadline"] is None
    await coord.investigate(run["id"], "capture_or_controller_failure", "capture lost")
    await coord.control(run["id"], "resume", "fixed", "resume")
    resumed = store.require("runs", run["id"])
    assert resumed["investigation_deadline"] is None
    assert resumed["players"]["player-1"]["last_observation"] is None


@pytest.mark.asyncio
async def test_watchdog_expiry_ends_only_affected_run_and_reports(environment):
    store, coord, _, _ = environment
    run = await new_run(environment)
    await coord.admit(run)
    # Imported legacy queues stay readable; the new launch API cannot create one.
    other = copy.deepcopy(run)
    other["id"] = "legacy-queued"
    store.put("runs", other)
    await coord.investigate(run["id"], "suspected_game_freeze", "suspected")
    deadline = store.require("runs", run["id"])["investigation_deadline"]
    await once(store, coord, now=deadline + 1)
    ended = store.require("runs", run["id"])
    assert ended["state"] == "Ended" and ended["ending_reason"] == "investigation_expired"
    assert store.require("runs", other["id"])["state"] == "Queued"
    assert (store.run_dir(run["id"]) / "report.md").exists()


@pytest.mark.asyncio
async def test_failed_teardown_retains_leases(environment):
    store, coord, _, desktops = environment
    run = await new_run(environment)
    await coord.admit(run)
    desktops["worker-1"].fail_stop = True
    await coord.finish(run["id"], "user_stopped", terminate=True)
    assert store.leases(run["id"])
    assert store.require("runs", run["id"])["evidence_status"] == "incomplete"


@pytest.mark.asyncio
async def test_manual_flag_does_not_pause(environment):
    store, coord, _, _ = environment
    run = await new_run(environment)
    await coord.admit(run)
    await coord.flag(run["id"], "Check the visible prompt")
    assert store.require("runs", run["id"])["state"] == "Starting"


@pytest.mark.asyncio
async def test_stale_generation_never_delivers_action(environment):
    store, coord, _, desktops = environment
    run = await new_run(environment)
    await coord.admit(run)
    await coord.control(run["id"], "pause", "hold", "pause")
    result = await coord.structured.dispatch(run["id"], "player-1", "perform_action", {"verb": "input"}, {}, 0)
    assert result["status"] == "cancelled"
    assert not any(path == "/action" for path, _ in desktops["worker-1"].calls)


@pytest.mark.asyncio
async def test_handler_revealed_does_not_complete_demo(environment):
    store, coord, _, _ = environment
    run = await new_run(environment)
    await coord.admit(run)
    run = store.require("runs", run["id"])
    run["players"]["player-1"].update(phase="ready", telemetry={"completed_quests": ["rocketship_tutorial_quest"], "handler": "MOAR", "demo_phase": "REVEALED"}, last_observation={"timestamp": time.time()})
    run["state"] = "Running"
    store.put("runs", run)
    await coord.monitor(run)
    assert not store.require("runs", run["id"])["demo_complete"]


@pytest.mark.asyncio
async def test_full_demo_requires_terminal_and_corroboration(environment):
    store, coord, plan, _ = environment
    plan["success_criteria"]["corroboration_seconds"] = 0
    run = await new_run(environment)
    await coord.admit(run)
    run = store.require("runs", run["id"])
    run["players"]["player-1"].update(phase="ready", telemetry={"completed_quests": ["demo_wrapup_moar"]}, last_observation={"timestamp": time.time()})
    run["state"] = "Running"
    store.put("runs", run)
    await coord.monitor(run)
    assert store.require("runs", run["id"])["demo_complete"]


def test_ledger_recovery_redaction_hash_and_full_appendix(environment):
    store, _, _, _ = environment
    e = store.event("run_test", "action", {"api_key": "secret", "message": "sk-secretKeyShouldNeverAppear12345"})
    content = (store.run_dir("run_test") / "ledger.jsonl").read_text()
    assert "secretKey" not in content and '"api_key": "secret"' not in content
    store.db.execute("DELETE FROM events WHERE run_id='run_test'")
    assert not store.recover_ledger("run_test")
    assert store.events("run_test")[0]["id"] == e["id"]


@pytest.mark.parametrize("expected", ["login_required", "model_unavailable", "rate_limited", "connection_failure"])
@pytest.mark.asyncio
async def test_codex_status_failures_are_exposed_without_api_fallback(environment, expected):
    store, _, _, _ = environment
    backend = FakeCodexBackend()
    backend.status = AsyncMock(side_effect=CodexError(expected, "Codex status unavailable"))
    ai = OpenAI(store, backend=backend)
    status = await ai.validate()
    assert status["status"] == expected
    assert status["api"] == "codex" and status["configured"] is False
    assert not backend.requests and not store.list("usage")


@pytest.mark.asyncio
async def test_codex_selection_uses_luna_and_reports_subscription_usage(environment):
    store, coord, _, _ = environment
    assert (await coord.ai.validate())["model"] == "gpt-6-luna"
    assert not store.list("usage"), "Checking sign-in does not make an inference request"
    questions = [{"type": "choice", "name": "next", "choices": [{"value": "wait", "description": "Wait"}]}]
    result = await coord.ai.decisions("The game is loading", questions)
    assert result == {"next": {"choice": "wait"}}
    assert coord.ai.backend.requests == [{"input": "The game is loading", "questions": questions, "model": MODEL}]
    [usage] = store.list("usage")
    assert usage["purpose"] == "player" and usage["model"] == "gpt-6-luna"
    assert usage["billing"] == "subscription" and usage["estimated_cost_usd"] is None
    assert usage["thread_id"] == "test-thread" and usage["turn_id"] == "test-turn"


def test_http_signed_out_instances_and_cross_origin_guard(tmp_path):
    app = create_app(tmp_path, MemoryCredentials(None), start_services=False)
    app.state.ai.backend = FakeCodexBackend(ready=False)
    with TestClient(app) as client:
        bootstrap = client.get("/api/bootstrap").json()
        assert bootstrap["ai"]["configured"] is False
        assert client.post("/api/instances", json={}).status_code == 403
        response = client.post("/api/instances", json={}, headers={"X-ASHAD-Request":"1"})
        assert response.status_code == 422
        assert client.post("/api/settings", json={}, headers={"X-ASHAD-Request":"1", "Origin":"https://evil.example"}).status_code == 403
        assert "api_key" not in client.get("/api/bootstrap").text


def test_repository_traversal_and_full_pagination(tmp_path):
    from ashad.repository import Repository
    for i in range(610):
        (tmp_path / f"{i:04}.gd").write_text("needle" if i == 609 else "other")
    repo = Repository({"source_root":str(tmp_path)})
    assert repo.search("needle")["total"] == 1
    assert repo.files(offset=500)["total"] == 610
    assert len(repo.files(offset=500)["files"]) == 110
    assert repo.files(query="0609")["files"][0]["path"] == "0609.gd"
    with pytest.raises(ValueError):
        repo.read("../../personal.txt")


@pytest.mark.asyncio
async def test_legacy_active_sessions_do_not_share_leases_or_deadlines(environment):
    store, coord, plan, desktops = environment
    store.put("settings", {"id":"main", "concurrent_sessions":2, "storage_reserve_bytes":1})
    first = await new_run(environment)
    await coord.admit(first)
    # Preserve recovery semantics for already-existing concurrent history, not
    # permission to author concurrent work through today's API.
    second = copy.deepcopy(store.require("runs", first["id"]))
    second["id"] = "legacy-active"
    second["plan"]["players"][0]["worker_id"] = "worker-2"
    second["players"]["player-1"].update(worker_id="worker-2", epoch="legacy-epoch")
    store.put("runs", second)
    assert store.reserve(second["id"], [("worker", "worker-2")])
    with pytest.raises(ValueError, match="One active instance"):
        await new_run(environment)
    assert store.require("runs", first["id"])["state"] == "Starting"
    assert store.require("runs", second["id"])["state"] == "Starting"
    await coord.investigate(first["id"], "progression_blocker", "first session only")
    deadline = store.require("runs", first["id"])["investigation_deadline"]
    await once(store, coord, now=deadline+1)
    assert store.require("runs", second["id"])["state"] == "Starting"
    assert not any(path in {"/pause", "/stop"} for path, _ in desktops["worker-2"].calls)
    assert store.leases(second["id"]) and not store.leases(first["id"])


@pytest.mark.asyncio
async def test_investigation_deadline_survives_recovery(environment):
    store, coord, _, _ = environment
    run = await new_run(environment)
    await coord.admit(run)
    await coord.investigate(run["id"], "progression_blocker", "preserve")
    deadline = store.require("runs", run["id"])["investigation_deadline"]
    await coord.recover()
    recovered = store.require("runs", run["id"])
    assert recovered["state"] == "Investigating"
    assert recovered["investigation_deadline"] == deadline


@pytest.mark.parametrize("trigger,value,state,expected", [
    ("elapsed", 10, {}, True), ("elapsed", 11, {}, False),
    ("milestone", "first_quota", {}, True), ("milestone", "missing", {}, False),
    ("event", "joined", {}, True), ("event", "missing", {}, False),
    ("manual", None, {"manual_triggered": True}, True), ("manual", None, {}, False),
])
def test_event_trigger_timing(trigger, value, state, expected):
    from ashad.coordinator import event_due
    run = {"started_at": 90, "world_ready_at": 100, "progress": {"completed_quests": ["first_quota"]},
           "events": {"joined": {"status": "complete"}}}
    event = {"trigger": trigger, "value": value, "time_basis": "world_ready"}
    assert bool(event_due(run, event, state, 110)) is expected
    if trigger == "elapsed":
        event["time_basis"] = "run"
        assert event_due(run, event, state, 110)
        run.pop("started_at")
        assert not event_due(run, event, state, 110)


@pytest.mark.asyncio
@pytest.mark.parametrize("command,options", [
    ("unknown", {}), ("resume", {}), ("extend", {"seconds": -1}),
    ("manual_event", {"event_id": "missing"}), ("goal", {"player_id": "missing", "goal": "new"}),
])
async def test_rejected_control_does_not_record_an_intervention(environment, command, options):
    store, coordinator, _, _ = environment
    run = await new_run(environment)
    before = store.require("runs", run["id"])
    events = store.count_events(run["id"])
    with pytest.raises(ValueError):
        await coordinator.control(run["id"], command, "invalid request", "rejected-command", **options)
    assert store.require("runs", run["id"]) == before
    assert store.count_events(run["id"]) == events
    assert store.command(run["id"], "rejected-command") is None


@pytest.mark.asyncio
async def test_cancelled_subscription_call_does_not_create_unknown_dollar_charges(environment):
    store, coordinator, plan, _ = environment
    plan["limits"]["spend_usd"] = 1
    run = await new_run(environment, plan)
    request_started = asyncio.Event()

    async def provider(input, questions, model=MODEL):
        request_started.set()
        await asyncio.Event().wait()

    backend = FakeCodexBackend()
    normal_select = backend.select
    backend.select = provider
    ai = OpenAI(store, backend=backend)
    questions = [{"type": "choice", "name": "test", "instructions": "Choose yes", "choices": [{"value": "yes", "description": "Yes"}, {"value": "no", "description": "No"}]}]
    pending = asyncio.create_task(ai.decisions("test", questions, run_id=run["id"]))
    await asyncio.wait_for(request_started.wait(), 1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    usage = store.list("usage")
    assert len(usage) == 1 and usage[0]["estimated_cost_usd"] is None
    assert usage[0]["status"] == "request_cancelled" and usage[0]["billing"] == "subscription"
    assert not await coordinator.enforce_limits(store.require("runs", run["id"]))
    backend.select = normal_select
    assert (await ai.decisions("test", questions, run_id=run["id"]))["test"]["choice"] == "yes"
    assert all(u["estimated_cost_usd"] is None and u["billing"] == "subscription" for u in store.list("usage"))


@pytest.mark.asyncio
@pytest.mark.parametrize("cost", [None, 1.0])
async def test_dollar_ceiling_still_checks_historical_metered_usage(environment, cost):
    store, coordinator, plan, _ = environment
    plan["limits"]["spend_usd"] = 1
    run = await new_run(environment, plan)
    store.put("usage", {"id": "legacy-api-request", "run_id": run["id"],
                        "api": "decisions", "estimated_cost_usd": cost})
    assert await coordinator.enforce_limits(run)
    assert store.require("runs", run["id"])["ending_reason"] == "limit_reached"


def test_long_acyclic_event_schedule_is_valid():
    events = [{
        "id": f"event-{index}", "actor": "player-1", "action": "disconnect",
        "trigger": "event" if index else "manual",
        "value": f"event-{index - 1}" if index else None,
    } for index in range(1100)]
    assert len(Plan(events=events).events) == len(events)
