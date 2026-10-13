"""Small public launch contract; the coordinator retains durable run recovery."""
import asyncio
from typing import Literal

from pydantic import Field

from .game_controls import GameControls
from .models import ACTIVE_STATES, CLAUDE_MODEL, GROK_MODEL, MODEL, Contract, Plan
from .projects import Projects


def default_model(ai):
    """Configurations own no model: prefer the connected Codex subscription, then Claude, then Grok."""
    if ai.status.get("status") == "ready":
        return MODEL
    if ai.claude_status.get("status") == "ready":
        return CLAUDE_MODEL
    if ai.grok_status.get("status") == "ready":
        return GROK_MODEL
    return MODEL


class InstanceRequest(Contract):
    configuration_id: str = Field(min_length=1, max_length=100)
    project_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    primary_goal: str = Field(min_length=1, max_length=4000)
    transport: Literal["enet", "steam"] = "enet"
    request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    model: str | None = Field(default=None, max_length=100)


class Instances:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.store = coordinator.store
        self.lock = asyncio.Lock()

    async def start(self, request: InstanceRequest):
        payload = request.model_dump(exclude_none=True)
        payload["primary_goal"] = request.primary_goal.strip()
        if not payload["primary_goal"]:
            raise ValueError("Enter a primary objective.")
        async with self.lock:
            previous = self.store.command("instance", request.request_id)
            if previous:
                if previous.get("request") != payload:
                    raise ValueError("This start request already belongs to a different instance. Start a new request.")
                return await self._launch(previous["scenario_id"], previous["revision"], request.request_id)

            if self.coordinator.draining or self.coordinator.stopping:
                raise ValueError("The controller is draining; new instances are blocked.")
            self.coordinator.require_instance_slot()
            record, _ = GameControls(self.store, self.coordinator.ai).validated(request.configuration_id)
            Projects(self.store).project_for_configuration(request.configuration_id, request.project_id)
            model = request.model or default_model(self.coordinator.ai)
            build = self.coordinator.validated_build(record["build_id"])
            worker, accounts = await self._allocate_worker(build, request.transport)
            plan = Plan(configuration_id=record["id"], build_id=record["build_id"], model=model,
                        title=payload["primary_goal"][:100], intent=payload["primary_goal"], primary_goal=payload["primary_goal"]).model_dump()
            plan["players"][0].update(name="Player 1", goal=payload["primary_goal"], goal_id="", worker_id=worker["id"],
                                     account_id=accounts[worker["id"]]["id"] if request.transport == "steam" else None)
            plan["transport"].update(mode=request.transport, host_address=worker.get("host_address"),
                                      app_id=build.get("steam_app_id") if request.transport == "steam" else None)
            plan["success_criteria"].update(predicate="manual", target="")
            plan["limits"]["duration_seconds"] = self.store.require("settings", "main").get("duration_seconds")
            scenario = self.coordinator.save_scenario(plan)
            self.store.command("instance", request.request_id, {"request": payload, "scenario_id": scenario["id"], "revision": scenario["revision"]})
            return await self._launch(scenario["id"], scenario["revision"], request.request_id)

    async def _allocate_worker(self, build, transport):
        """First unoccupied desktop whose fresh probes pass; returns it with the build's licensed accounts by worker."""
        occupied = {lease["resource_id"] for lease in self.store.leases() if lease["kind"] == "worker"}
        for run in self.store.list("runs"):
            if run["state"] in ACTIVE_STATES | {"Queued"}:
                occupied.update(p["worker_id"] for p in run["plan"]["players"])
        accounts = {a["worker_id"]: a for a in self.store.list("accounts")
                    if a.get("steam_id") and a.get("app_id") == build.get("steam_app_id")}
        workers = [w for w in self.store.list("workers") if w.get("enabled", True) and w["id"] not in occupied
                   and (w["id"] in accounts if transport == "steam" else w.get("host_address"))]
        for candidate in workers:
            environment = await self.coordinator.inspect_worker_environment(build, candidate["id"], account=accounts.get(candidate["id"]))
            if environment["ready"] and (transport != "steam" or environment["steam"]["ready"]):
                return candidate, accounts
        if transport == "steam":
            raise ValueError("No available desktop with a fresh verified, licensed Steam identity for this build. Steam readiness is unknown or unverified; cached account readiness is insufficient. No ENet fallback is allowed.")
        raise ValueError("No available desktop with fresh positive build, rendering, capture, input and idle probes. Check Settings and the mounted build, or stop an existing instance.")

    async def _launch(self, scenario_id, revision, request_id):
        try:
            return await self.coordinator.launch(scenario_id, revision, "instance_" + request_id)
        except BaseException:
            # Keep retries bound to any committed run, but let a rejected start be corrected.
            if not self.store.command("launch", "instance_" + request_id):
                self.store.forget_command("instance", request_id)
            raise
