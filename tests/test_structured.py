"""Structured contracts exercised with game/AI doubles; no live gameplay or API use."""
import asyncio
import copy
import json
import time

import httpx
import pytest
from fastapi import HTTPException

from ashad.ai import OpenAI
from ashad.app import create_app
from ashad.models import Plan
from ashad.structured import StructuredPlayers, meaningful_signature, prompt_observation, wait_signature
from ashad.store import Store
from worker.game_bridge import GameBridge
from worker.models import Binding, BoundCommand, GameRequest
from worker.models import GameAction
from worker.runtime import Runtime
from test_workers import DesktopDouble, ProcessDouble


class Credentials:
    def get(self, *args):
        return None


def observation(**changes):
    return {"schema_version": 1, "run_id": "run", "player_id": "p", "observation_id": "g:obs:1",
            "generation": "g", "revision": 1, "timestamp": time.time(), "ready": True,
            "state": {"player": {"inventory": [{"item_id": "wood", "count": 2}]}, "objectives": []},
            "entities": [{"id": "target", "version": 1, "label": "Harvest", "actions": ["interact"]}], **changes}


def test_structured_only_defaults_and_bounded_context():
    assert Plan().play_mode == "structured"
    for old_mode in ("screen", "hybrid"):
        with pytest.raises(ValueError):
            Plan(play_mode=old_mode)
    assert Plan(play_mode="structured").structured_version == 1
    original = observation(entities=[{"id": str(i), "label": "x"*500, "actions": ["ui_click"]} for i in range(100)])
    result = prompt_observation(original, 3000)
    assert result["context_truncated"] and not result["complete"]
    assert len(result["entities"]) < 100
    assert len(original["entities"]) == 100
    assert meaningful_signature(observation(timestamp=1)) == meaningful_signature(observation(timestamp=2))
    assert meaningful_signature(observation(ready=False)) != meaningful_signature(observation())


@pytest.mark.parametrize("parameters", ["not a mapping", {"direction": []}, {"position": [float("nan"), 0]}, {"duration_ms": 50000}, {"name": "inventory", "arbitrary_method": "save"}])
def test_malformed_action_parameters_are_rejected(parameters):
    with pytest.raises(ValueError):
        GameAction.model_validate({"action_id": "a", "observation_id": "o", "expires_at": time.time()+10,
                                   "verb": "input", "parameters": parameters})


def test_wait_ignores_moving_bystanders_but_wakes_on_inventory():
    first = observation()
    second = copy.deepcopy(first)
    second["entities"][0].update(position=[100, 200], version=12)
    second["state"]["player"]["position"] = [20, 30]
    assert wait_signature(first) == wait_signature(second)
    second["state"]["player"]["inventory"][0]["count"] = 3
    assert wait_signature(first) != wait_signature(second)


@pytest.mark.asyncio
async def test_bridge_framing_and_no_automatic_retry():
    requests = []
    async def serve(reader, writer):
        request = json.loads(await reader.readline())
        requests.append(request)
        writer.write(b'{"status":"completed"}\n')
        await writer.drain()
        writer.close()
    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    async with server:
        bridge = GameBridge(server.sockets[0].getsockname()[1])
        assert (await bridge.request("observe"))["status"] == "completed"
        assert requests[0]["token"] == bridge.token
        assert len(requests) == 1


@pytest.mark.asyncio
async def test_worker_binding_freshness_pause_and_heartbeat_recovery(tmp_path):
    worker = Runtime(tmp_path, desktop=DesktopDouble())
    await worker.bind(Binding(run_id="run", player_id="p", epoch="e", identity="i", settings={"play_mode": "structured"}))
    worker.game = ProcessDouble()
    worker.lifecycle = "running"
    calls = []
    async def bridge(op, args):
        calls.append((op, args))
        return observation() if op == "observe" else {"status": "running"}
    worker.game_bridge.request = bridge
    def req(op, **args):
        return GameRequest(run_id="run", player_id="p", epoch="e", operation=op, arguments=({"action_id":"a", "verb":"input", "parameters":{"name":"inventory"}, **args} if op == "perform_action" else args))
    with pytest.raises(HTTPException):
        await worker.game_request(req("perform_action", observation_id="g:obs:1", expires_at=time.time()+5))
    observed = await worker.game_request(req("observe"))
    await worker.game_request(req("perform_action", observation_id=observed["observation_id"], expires_at=time.time()+5))
    assert calls[-1][1]["epoch"] == worker.game_control_epoch
    worker.lifecycle = "paused"
    worker.interrupt.set()
    worker.infrastructure_error = "controller_heartbeat_lost"
    command = BoundCommand(run_id="run", player_id="p", epoch="e")
    await worker.resume(command)
    assert worker.infrastructure_error is None
    assert not worker.game_observations
    fresh = await worker.game_request(req("observe"))
    assert fresh["observation_id"] != observed["observation_id"]
    with pytest.raises(HTTPException):
        await worker.game_request(req("perform_action", observation_id=observed["observation_id"], expires_at=time.time()+5))
    worker.lifecycle = "paused"
    worker.infrastructure_error = "input_executor_did_not_quiesce"
    with pytest.raises(HTTPException):
        await worker.resume(command)
    assert worker.lifecycle == "paused"


@pytest.mark.asyncio
async def test_decisions_structured_has_no_image_or_desktop_tool(tmp_path):
    store = Store(tmp_path)
    ai = OpenAI(store, Credentials())
    sent = []
    async def decide(input, questions, **kwargs):
        sent.append((input, questions))
        question = questions[0]
        return {question["name"]: {"choice": question["choices"][0]["value"], "confidence": 1}}
    ai.decisions = decide
    calls = []
    async def dispatch(name, args):
        calls.append(name)
        return {"status": "waiting"}
    await ai.player_turn({"play_mode": "structured", "observation": {"entities": []},
                         "player": {"id": "p", "context_policy": "instructions"}},
                         dispatch, lambda: True, "run")
    assert "input_image" not in json.dumps(sent)
    assert all(question["type"] == "choice" for _, questions in sent for question in questions)
    assert calls == ["wait_for_change"]
    store.close()


class CoordinatorDouble:
    def __init__(self, store):
        self.store = store
        self.ai = self
        self.decisions = 0
        self.ops = []
        self.result = {"status": "completed"}
        self.goal_reaches = []

    async def goal_reached(self, *args):
        self.goal_reaches.append(args)

    async def request(self, worker, method, path, body):
        self.ops.append(body)
        return observation() if body["operation"] == "observe" else self.result

    async def player_turn(self, *args):
        self.decisions += 1

    async def investigate(self, *args):
        self.investigation = args


def seed_run(store):
    plan = Plan(play_mode="structured").model_dump()
    plan["players"] = [{"id": "p", "goal": "Tutorial", "context_policy": "instructions", "cooperation": "team"}]
    store.put("runs", {"id": "run", "state": "Running", "plan": plan, "build": {},
        "players": {"p": {"id": "p", "bound": True, "launched": True, "epoch": "e", "generation": 1, "worker_id": "w"}}})


@pytest.mark.asyncio
async def test_wait_stale_decisions_and_single_control_owner(tmp_path):
    store = Store(tmp_path)
    seed_run(store)
    coordinator = CoordinatorDouble(store)
    service = StructuredPlayers(coordinator)
    coordinator.structured = service
    obs = observation()
    result = await service.dispatch("run", "p", "perform_action", {"verb": "interact", "target_id": "target", "target_version": 1}, obs, 0)
    assert result["status"] == "cancelled" and not coordinator.ops
    service.control_owners[("run", "p")] = "external"
    result = await service.dispatch("run", "p", "perform_action", {"verb": "interact"}, obs, 1)
    assert result["status"] == "rejected" and not coordinator.ops
    service.control_owners.clear()
    service.generations[("run", "p")] = 1
    await service.dispatch("run", "p", "wait_for_change", {"seconds": 30, "reason": "grow"}, obs, 1)
    await service.turn("run", "p")
    assert coordinator.decisions == 0
    coordinator.result = {"status": "unknown"}
    service.pending[("run", "p")] = "lost-action"
    await service.turn("run", "p")
    assert coordinator.investigation[1] == "unknown_execution"
    assert coordinator.decisions == 0
    store.close()


@pytest.mark.asyncio
async def test_mcp_requires_bound_auth_and_exposes_real_protocol(tmp_path):
    from ashad.game_mcp import GameMCP
    store = Store(tmp_path)
    seed_run(store)
    coordinator = CoordinatorDouble(store)
    coordinator.structured = StructuredPlayers(coordinator)
    mcp = GameMCP(coordinator)
    token = mcp.issue("run", "p")["token"]
    assert not mcp.resolve("Bearer " + token)["control"]
    with pytest.raises(ValueError):
        mcp.resolve("Bearer invalid")
    async with mcp.server.session_manager.run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mcp.app), base_url="http://localhost:8740") as client:
            response = await client.post("/", headers={"Accept": "application/json, text/event-stream", "Authorization": "Bearer " + token},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
            assert response.status_code == 200, response.text
            names = {t["name"] for t in response.json()["result"]["tools"]}
            assert {"observe", "perform_action", "read_changes"} <= names
            response = await client.post("/", headers={"Accept": "application/json, text/event-stream", "Authorization": "Bearer " + token},
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "observe", "arguments": {}}})
            assert response.status_code == 200
            assert response.json()["result"].get("structuredContent", {}).get("player_id") == "p", response.text
            assert sum(op["operation"] == "observe" for op in coordinator.ops) == 1
            response = await client.post("/", headers={"Accept": "application/json, text/event-stream", "Authorization": "Bearer " + token},
                json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "perform_action", "arguments": {"verb": "input", "parameters": {"name": "inventory"}, "reason": "test"}}})
            assert response.json()["result"]["isError"] is True
            assert not any(op["operation"] == "perform_action" for op in coordinator.ops)
            assert sum(op["operation"] == "observe" for op in coordinator.ops) == 1, "A rejected mutating call must not observe"
    live = store.require("runs", "run")
    live["players"]["p"]["generation"] += 1
    store.put("runs", live)
    with pytest.raises(ValueError):
        mcp.resolve("Bearer " + token)
    store.close()


@pytest.mark.asyncio
async def test_dashboard_mcp_boundary_and_lifespan(tmp_path):
    app = create_app(tmp_path, Credentials(), start_services=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8740") as client:
            response = await client.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert response.status_code == 401
