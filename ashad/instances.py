"""Small public launch contract; the coordinator retains durable run recovery."""
import asyncio
from typing import Literal

from pydantic import Field

from .agent_profiles import AgentProfiles
from .models import ACTIVE_STATES, Contract, Plan


class InstanceRequest(Contract):
    agent_id: str = Field(min_length=1, max_length=100)
    primary_goal: str = Field(min_length=1, max_length=4000)
    transport: Literal["enet", "steam"] = "enet"
    request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")


class Instances:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.store = coordinator.store
        self.lock = asyncio.Lock()

    async def start(self, request: InstanceRequest):
        payload = request.model_dump()
        payload["primary_goal"] = request.primary_goal.strip()
        if not payload["primary_goal"]:
            raise ValueError("Enter a primary objective.")
        async with self.lock:
            previous = self.store.command("instance", request.request_id)
            if previous:
                if previous["request"] != payload:
                    raise ValueError("This start request already belongs to a different instance. Start a new request.")
                return await self._launch(previous["scenario_id"], previous["revision"], request.request_id)

            profile = AgentProfiles(self.store, self.coordinator.ai).validated(request.agent_id)
            build = self.store.require("builds", profile["build_id"])
            occupied = {lease["resource_id"] for lease in self.store.leases() if lease["kind"] == "worker"}
            for run in self.store.list("runs"):
                if run["state"] in ACTIVE_STATES | {"Queued"}:
                    occupied.update(p["worker_id"] for p in run["plan"]["players"])
            accounts = {a["worker_id"]: a for a in self.store.list("accounts")
                        if a.get("readiness") == "ready" and a.get("steam_id") and a.get("app_id") == build.get("steam_app_id")}
            workers = [w for w in self.store.list("workers") if w.get("enabled", True) and w["id"] not in occupied
                       and (w["id"] in accounts if request.transport == "steam" else w.get("host_address"))]
            worker = None
            for candidate in workers:
                try:
                    health = await self.coordinator.client(candidate["id"]).request("GET", "/health")
                except Exception:
                    continue
                if health.get("independent_desktop") is False or health.get("steam", {}).get("setup_active"):
                    continue
                measured = health.get("build") or {}
                if any(build.get(expected) and measured.get(actual) and measured[actual] != build[expected]
                       for actual, expected in (("executable_sha256", "engine_sha256"), ("artifact_sha256", "artifact_sha256"))):
                    continue
                if health.get("lifecycle", "unbound") in {"unbound", "ended"}:
                    worker = candidate
                    break
            if worker is None:
                requirement = " with a verified, licensed Steam account" if request.transport == "steam" else ""
                raise ValueError(f"No available desktop for this build{requirement}. Check Settings and the mounted build, or stop an existing instance.")
            plan = Plan(agent_id=profile["id"], build_id=profile["build_id"], model=profile["model"],
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

    async def _launch(self, scenario_id, revision, request_id):
        try:
            return await self.coordinator.launch(scenario_id, revision, "instance_" + request_id)
        except BaseException:
            # Keep retries bound to any committed run, but let a rejected start be corrected.
            if not self.store.command("launch", "instance_" + request_id):
                with self.store.transaction() as db:
                    db.execute("DELETE FROM commands WHERE scope=? AND id=?", ("instance", request_id))
            raise
