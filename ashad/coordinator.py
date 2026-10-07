"""Durable session coordination. Gameplay always goes through desktop input."""
from __future__ import annotations
import asyncio
import base64
import copy
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import shutil
import time
import uuid
from .models import Plan, MODEL, CONTROLLER_VERSION, ACTIVE_STATES, TERMINAL_QUEST
from .repository import Repository
from .reports import generate_report
from .repair_tasks import record_task, publish_pending
from .store import uid
from . import subgoals
from .subgoals import new_plan
from .validation import validate_plan
from .workspace_environment import inspect_environment, validate_build


def event_due(run, event, state, now):
    trigger = event["trigger"]
    if trigger == "elapsed":
        basis = run.get("world_ready_at") if event["time_basis"] == "world_ready" else run.get("started_at")
        return basis is not None and now >= basis + float(event["value"])
    if trigger == "milestone":
        return event["value"] in run["progress"].get("completed_quests", [])
    if trigger == "event":
        return run["events"].get(str(event["value"]), {}).get("status") == "complete"
    return trigger == "manual" and bool(state.get("manual_triggered"))


class InstanceConflict(ValueError):
    def __init__(self, owners):
        self.owners = owners
        labels = ", ".join(f"{owner['run_id']} ({owner['state']}: {owner['reason']})" for owner in owners)
        super().__init__(f"One active instance is allowed. Stop or reconcile the owning instance first: {labels}")


class Coordinator:
    def __init__(self, store, credentials, ai, client_factory=None):
        self.store, self.credentials, self.ai = store, credentials, ai
        self.client_factory = client_factory
        self.lock = asyncio.Lock()
        self.tasks = {}
        self.ticks = {}
        self.command_locks = {}
        self.clients = {}
        self.stopping = False
        self.draining = False
        from .structured import StructuredPlayers
        self.structured = StructuredPlayers(self)

    def occupied_instances(self):
        """Lifecycle labels cannot release unresolved desktop/account ownership."""
        leases = {lease["run_id"] for lease in self.store.leases()}
        owners = []
        for run in self.store.list("runs"):
            unresolved = any(ps.get("teardown_confirmed") is False or
                             (ps.get("teardown_confirmed") is not True and
                              (ps.get("bound") or ps.get("launched") or ps.get("phase") == "teardown_unknown"))
                             for ps in run.get("players", {}).values())
            terminal = str(run.get("state", "Unknown")).lower() in {"ended", "completed", "failed", "terminated", "cancelled"}
            if not terminal or run["id"] in leases or unresolved or run.get("human_control") or run.get("external_control"):
                owners.append({"run_id": run["id"], "state": run["state"],
                               "reason": "teardown_unconfirmed" if run["state"] == "Ended" else
                                         "human_control" if run.get("human_control") else run["state"].lower()})
            leases.discard(run["id"])
        owners.extend({"run_id": run_id, "state": "Unknown", "reason": "orphaned_lease"}
                      for run_id in sorted(leases))
        return owners

    def import_interrupted(self):
        return any((self.store.root / name).exists() for name in (".workspace-import-in-progress.json", ".workspace-import-runs"))

    def require_instance_slot(self, run_id=None):
        if self.import_interrupted():
            raise ValueError("Workspace import is incomplete; admission is blocked until the journal and verified backup are reconciled")
        others = [owner for owner in self.occupied_instances() if owner["run_id"] != run_id]
        if others:
            raise InstanceConflict(others)

    async def reconcile(self, run_id):
        async with self.lock:
            run = self.store.require("runs", run_id)
            if run["state"] != "Ended":
                raise ValueError("Reconciliation is for ended runs with unresolved teardown")
            results = []
            for pid, ps in run["players"].items():
                if ps["teardown_confirmed"]:
                    continue
                try:
                    result = await self.request(ps["worker_id"], "POST", "/stop", {
                        "epoch": ps["epoch"], "terminate": True, "reason": "human_reconciliation"})
                    ps["teardown_confirmed"] = result.get("teardown_confirmed") is True
                    if ps["teardown_confirmed"]:
                        ps.update(phase="ended", launched=False)
                    results.append({"player_id": pid, "confirmed": ps["teardown_confirmed"]})
                except Exception as exc:
                    results.append({"player_id": pid, "confirmed": False, "reason": type(exc).__name__})
            if all(p["teardown_confirmed"] for p in run["players"].values()):
                self.store.release(run_id)
                run.pop("human_control", None)
            self.save_run(run)
            self.store.event(run_id, "teardown_reconciled", {"results": results, "actor": "human"})
            await self.collect_evidence(run_id)
            return self.store.require("runs", run_id)

    def save_run(self, run):
        if self.ticks.get(run["id"]) is asyncio.current_task():
            latest = self.store.require("runs", run["id"])
            # The player task owns planning; a monitoring snapshot must not erase its updates.
            for field in ("subgoal_plan", "goal_recalculation"):
                if field in latest:
                    run[field] = copy.deepcopy(latest[field])
            for pid, ps in run["players"].items():
                for key in ("last_observation", "activity", "invalid_actions", "structured_context", "agent_memory", "agent_status", "pending_action", "observation_signature", "last_state_change_at"):
                    if key == "agent_status" and run["state"] not in {"Starting", "Running"}:
                        continue
                    ps[key] = latest["players"][pid].get(key, ps.get(key))
        return self.store.put("runs", run)

    def run_lock(self, run_id):
        return self.command_locks.setdefault(run_id, asyncio.Lock())

    async def cancel_tick(self, run_id):
        task = self.ticks.get(run_id)
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def cancel_players(self, run_id):
        tasks = [task for (rid, _), task in self.tasks.items()
                 if rid == run_id and task is not asyncio.current_task() and not task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def pause_details(category, reason, players, started_at=None):
        recovery = {
            "provider_failure": "Check AI Settings and the provider error, then Resume to retry with fresh game state.",
            "decision_needs_instructions": "Inspect the current game state and decision candidates. Give a concrete next step before Resume; if no supported action can achieve it, terminate and adjust the scenario or action support.",
            "infrastructure_failure": "Check the affected desktop, game process and worker connection. Resolve the failure before Resume; terminate and rerun if the process exited.",
            "structured_interface_failure": "Check the game bridge and worker logs. Resume after the game interface is reachable again.",
            "unknown_execution": "Inspect the last action and current game state before Resume; do not blindly repeat an action with an unknown outcome.",
            "controller_failure": "Inspect the last rejected action and visible controls. Correct the input problem, then Resume.",
            "suspected_game_freeze": "Inspect the live view and game logs. Resume if the game is responsive; otherwise terminate and rerun.",
            "client_disagreement": "Check that all players are connected to the host world, then Resume after synchronization recovers.",
            "coordinator_restarted": "Review the current game state, then Resume to continue with fresh observations.",
            "manual_pause": "Choose Resume when ready. Recovery instructions are optional.",
            "planner_blocked": "If the focused goal is obsolete, choose Recalculate goals to plan from current game state. Review the new goal, then Resume.",
            "goal_recalculation": "Review the recalculated goal, then Resume. If recalculation failed, correct the reported problem and retry Recalculate goals; no bot input has resumed.",
            "human_control": "Use the remote desktop to play, then choose Hand back to bots. Closing the viewer leaves bots paused.",
        }.get(category, "Inspect the latest observation and action result. Add optional recovery instructions, then Resume, or terminate the run.")
        return {"category": category, "reason": reason, "players": players,
                "started_at": started_at or time.time(), "recovery": recovery}

    async def tick_runner(self, run_id):
        try:
            run = self.store.require("runs", run_id)
            await self.tick(run)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.investigate(run_id, "infrastructure_failure", f"Coordinator monitoring failed: {type(exc).__name__}: {exc}")

    def client(self, worker_id):
        worker = self.store.require("workers", worker_id)
        if self.client_factory:
            return self.client_factory(worker)
        if worker_id in self.clients:
            return self.clients[worker_id]
        from .worker_client import WorkerClient
        token = self.credentials.get(worker["credential_ref"])
        if not token:
            raise ValueError(f"Worker {worker_id} credential is unavailable")
        self.clients[worker_id] = WorkerClient(worker["url"], token, timeout=8)
        return self.clients[worker_id]

    async def request(self, worker_id, method, path, body=None):
        client = self.client(worker_id)
        # The wire contract is stable and deliberately separate from deployment.
        if body is not None and path != "/bind" and not path.startswith("/setup/"):
            body = dict(body)
            if "run_id" not in body:
                for run in self.store.list("runs"):
                    ps = next((p for p in run.get("players", {}).values() if p["worker_id"] == worker_id and p["epoch"] == body.get("epoch")), None)
                    if ps:
                        body.update(run_id=run["id"], player_id=ps["id"])
                        if path == "/heartbeat":
                            body["investigation_deadline"] = body.pop("deadline", run.get("investigation_deadline"))
                            duration = run["plan"]["limits"]["duration_seconds"]
                            body["run_deadline"] = run["started_at"] + duration if duration and run.get("started_at") else None
                        break
        return await client.request(method, path, data=body)

    def save_scenario(self, raw, scenario_id=None):
        plan = Plan.model_validate(raw).model_dump()
        scenario_id = scenario_id or uid("scenario_")
        existing = [s for s in self.store.list("scenarios") if s["id"] == scenario_id]
        revision = max((s["revision"] for s in existing), default=0) + 1
        record = {"id": scenario_id, "revision": revision, "title": plan["title"], "created_at": time.time(), "plan": plan}
        self.store.put("scenarios", record, f"{scenario_id}:{revision}")
        return record

    def validated_build(self, build_id):
        build = copy.deepcopy(self.store.require("builds", build_id))
        try:
            validate_build(build)
        except (KeyError, TypeError, OSError):
            raise ValueError("Pinned build cannot be verified; re-register a supported build with available artifacts.") from None
        return build

    async def inspect_worker_environment(self, build, worker_id, *, account=None):
        """Fresh passive measurements through the same authenticated client as launch."""
        worker = self.store.require("workers", worker_id)
        probe = status = None
        try:
            client = self.client(worker_id)
            probe = await client.request("POST", "/prepare/probe", data={"build": build})
            status = await client.request("GET", "/status")
        except Exception:
            # Worker exception bodies can contain credentials; report authored unknowns only.
            probe = None
        occupied = any(lease["kind"] == "worker" and lease["resource_id"] == worker_id for lease in self.store.leases())
        return inspect_environment(build, worker, probe, status, occupied=occupied, account=account)

    async def launch(self, scenario_id, revision, idempotency_key):
        if not idempotency_key:
            raise ValueError("Launch needs an idempotency key")
        async with self.lock:
            cached = self.store.command("launch", idempotency_key)
            if cached:
                previous = self.store.require("runs", cached["run_id"])
                if previous["scenario_id"] != scenario_id or previous["scenario_revision"] != revision:
                    raise ValueError("Launch request ID already belongs to different frozen inputs")
                return previous
            if self.draining or self.stopping:
                raise ValueError("The controller is draining; new instances are blocked.")
            self.require_instance_slot()
            scenario = self.store.require("scenarios", f"{scenario_id}:{revision}")
            status = await self.ai.validate(Plan.model_validate(scenario["plan"]).model)
            if status.get("status") != "ready" or status.get("configured") is not True:
                raise ValueError(status.get("message") or "Verify ChatGPT sign-in and this model before starting an instance")
            checked = validate_plan(scenario["plan"], self.store, self.credentials, check_credentials=True)
            if not checked["valid"]:
                raise ValueError("; ".join(checked["errors"]))
            plan = checked["plan"]
            build = self.validated_build(plan["build_id"])
            agent_profile = None
            if plan.get("agent_id"):
                from .agent_profiles import AgentProfiles
                agent_profile = AgentProfiles(self.store, self.ai).validated(plan["agent_id"])
                from .projects import Projects
                Projects(self.store).project_for_agent(plan["agent_id"])
                if agent_profile["build_id"] != plan["build_id"] or agent_profile["model"] != plan["model"]:
                    raise ValueError("Instance build and model must match its tuned agent.")
            for player in plan["players"]:
                account = self.store.get("accounts", player["account_id"]) if player["account_id"] else None
                environment = await self.inspect_worker_environment(build, player["worker_id"], account=account)
                if not environment["ready"]:
                    raise ValueError(f"{player['name']}: fresh desktop readiness is not verified. {environment['reason']}")
                if plan["transport"]["mode"] == "steam" and not environment["steam"]["ready"]:
                    raise ValueError(f"{player['name']}: Steam readiness is unknown or unverified. {environment['steam']['reason']}. No ENet fallback is allowed.")
            run_id = uid("run_")
            players = {}
            for p in plan["players"]:
                account = self.store.get("accounts", p["account_id"], {}) if p["account_id"] else {}
                identity = account.get("steam_id") if plan["transport"]["mode"] == "steam" else p["identity"]
                if not identity and plan["world_setup"]["mode"] != "fresh" and plan["world_setup"]["character_policy"] == "restore":
                    fixture = self.store.require("fixtures", plan["world_setup"]["fixture_id"])
                    identity = fixture["players"][plan["world_setup"]["identity_mapping"][p["id"]]]["identity"]
                identity = identity or str(uuid.uuid4())
                players[p["id"]] = {"id": p["id"], "worker_id": p["worker_id"], "identity": str(identity), "epoch": uid("epoch_"), "generation": 0, "phase": "waiting", "activity": "Waiting for launch" if p["initial"] else "Scheduled", "launched": False, "bound": False, "teardown_confirmed": False, "invalid_actions": 0, "last_observation": None, "telemetry": None}
            run = {"id": run_id, "session_id": run_id, "scenario_id": scenario_id, "scenario_revision": revision, "state": "Queued", "created_at": time.time(), "plan": copy.deepcopy(plan), "build": copy.deepcopy(build), "players": players, "events": {e["id"]: {"status": "pending", "attempts": 0} for e in plan["events"]}, "model": plan["model"], "controller_version": CONTROLLER_VERSION, "world_namespace": run_id, "progress": {"completed_quests": [], "handler": None, "disagreement": [], "world_simulating": "unknown"}, "starting_milestones": [], "investigation_deadline": None, "goal_complete": False, "demo_complete": False, "validity": "unknown", "evidence_status": "collecting", "limitations": [], "interventions": [], "evidence": [], "launch_record": {"explicit": True, "timestamp": time.time(), "scenario_revision": revision, "idempotency_key": idempotency_key}}
            run["approved_plan"] = copy.deepcopy(plan)
            if agent_profile:
                run.update(agent_id=agent_profile["id"], agent_profile=copy.deepcopy(agent_profile))
                if len(plan["players"]) == 1:
                    run["subgoal_plan"] = new_plan(plan["players"][0]["goal"], plan["players"][0]["id"], run_id)
            run.update(api="codex", billing="subscription")
            run["source_run_id"] = scenario.get("source_run_id")
            run["rerun_kind"] = scenario.get("rerun_kind")
            run["project_id"] = run["build"].get("project_id")
            with self.store.transaction() as db:
                # Accepted/queued work owns the fixed global slot before any worker
                # allocation. SQLite serializes even independently constructed callers.
                if self.draining or self.stopping:
                    raise ValueError("The controller is draining; new instances are blocked.")
                self.require_instance_slot()
                db.execute("INSERT INTO leases VALUES (?,?,?)", ("instance", "global", run_id))
                self.save_run(run)
                self.store.command("launch", idempotency_key, {"run_id": run_id})
            self.store.event(run_id, "launch_approved", {"scenario_id": scenario_id, "revision": revision, "plan": plan, "identities": {k: v["identity"] for k, v in players.items()}})
            generate_report(self.store, run_id)
            return run

    async def admit(self, run):
        async with self.run_lock(run["id"]), self.lock:
            if self.draining or self.stopping:
                return
            # Re-read after locking: Stop may have ended a previously queued snapshot.
            run = self.store.require("runs", run["id"])
            if run["state"] != "Queued":
                return
            await self._admit(run)

    async def _admit(self, run):
        # Legacy cap values never authorize a second execution; multiple legacy
        # queued/active owners require explicit human recovery before admission.
        if self.import_interrupted() or any(owner["run_id"] != run["id"] for owner in self.occupied_instances()):
            return
        resources = [("instance", "global")]
        resources += [("worker", p["worker_id"]) for p in run["plan"]["players"]]
        resources += [("account", p["account_id"]) for p in run["plan"]["players"] if p["account_id"]]
        # Session endpoint belongs to the host network namespace and never collides.
        host = next(p for p in run["plan"]["players"] if p["role"] == "host")
        resources.append(("endpoint", f"{host['worker_id']}:{run['plan']['transport']['port']}"))
        if not self.store.reserve(run["id"], resources):
            return
        run.update(state="Starting", started_at=time.time(), last_progress_at=time.time(), coordinator_heartbeat=time.time())
        self.save_run(run)
        self.store.event(run["id"], "capacity_reserved", {"resources": resources})
        try:
            await self.start_player(run, host)
        except Exception as exc:
            await self.finish(run["id"], "infrastructure_failure", terminate=True, limitation=f"Host startup failed: {type(exc).__name__}: {exc}")

    async def start_player(self, run, player):
        state = run["players"][player["id"]]
        transport = run["plan"]["transport"]
        binding = {"run_id": run["id"], "player_id": player["id"], "epoch": state["epoch"], "identity": state["identity"], "transport": transport["mode"], "app_id": transport["app_id"], "settings": {**run["plan"]["evidence_policy"], "play_mode": run["plan"].get("play_mode", "structured"), "observation_max_age_seconds": run["plan"]["stall_policy"]["observation_max_age_seconds"]}}
        # Treat ambiguous bind execution as owned until teardown is reconciled.
        state["bound"] = True
        self.save_run(run)
        await self.request(state["worker_id"], "POST", "/bind", binding)
        setup = run["plan"]["world_setup"]
        if setup["mode"] != "fresh" and not state.get("restored"):
            fixture = self.store.require("fixtures", setup["fixture_id"])
            source = fixture["players"][setup["identity_mapping"][player["id"]]]
            if player["role"] == "host":
                source = next((p for p in fixture["players"].values() if p.get("role") == "host"), None)
                if not source:
                    raise ValueError("Saved-start package has no authoritative world archive")
            archive = Path(fixture["path"]) / source["archive"]
            if source.get("sha256"):
                from .repository import sha256_file
                if sha256_file(archive) != source["sha256"]:
                    raise ValueError("Saved-start archive changed after registration")
            from integration.fixtures import prepare_archive, _archive_files
            blob = archive.read_bytes()
            embedded = json.loads(_archive_files(blob)["ashad-fixture.json"])
            target_mapping = {p["id"]: run["players"][p["id"]]["identity"] for p in run["plan"]["players"]}
            prepared, prepared_manifest = prepare_archive(blob, embedded, target_mapping, setup["character_policy"], transport["mode"])
            await self.request(state["worker_id"], "POST", "/fixtures/import", {"epoch": state["epoch"], "manifest": prepared_manifest, "archive_base64": base64.b64encode(prepared).decode(), "identity_mapping": {}})
            self.store.event(run["id"], "fixture_staged", {"player_id": player["id"], "source_fixture": fixture["id"], "audit": prepared_manifest.get("setup_audit", [])})
            state["restored"] = True
            run["starting_milestones"] = fixture.get("starting_milestones", [])
        host = next(p for p in run["plan"]["players"] if p["role"] == "host")
        host_worker = self.store.require("workers", host["worker_id"])
        env = {"ASHAD_RUN_ID": run["id"], "ASHAD_PLAYER_ID": player["id"], "ASHAD_TRANSPORT": transport["mode"], "ASHAD_HOST": transport["host_address"] or host_worker.get("host_address", ""), "ASHAD_PORT": str(transport["port"]), "ASHAD_IDENTITY": state["identity"], "ASHAD_ROLE": player["role"]}
        if transport["app_id"]:
            env["ASHAD_STEAM_APP_ID"] = str(transport["app_id"])
        result = await self.request(state["worker_id"], "POST", "/launch", {"epoch": state["epoch"], "command": run["build"]["command"], "env": env})
        state.update(launched=True, phase="launched", launched_at=time.time(), activity="Starting through normal game menus", teardown_confirmed=False)
        self.save_run(run)
        self.store.event(run["id"], "player_launched", {"player_id": player["id"], "worker_id": state["worker_id"], "identity": state["identity"], "result": result})

    async def recover(self):
        for run in self.store.list("runs"):
            gaps = self.store.recover_ledger(run["id"])
            if gaps:
                run["limitations"].extend(gaps)
                run["evidence_status"] = "incomplete"
            if run["state"] in ACTIVE_STATES:
                if run.get("investigation_deadline") and run["investigation_deadline"] <= time.time():
                    await self.finish(run["id"], "investigation_expired", terminate=True)
                    continue
                if run["state"] == "Stopping":
                    await self.finish(run["id"], run.get("ending_reason", "infrastructure_failure"), terminate=True)
                    continue
                restart = self.store.event(run["id"], "coordinator_restarted", {"previous_state": run["state"]})
                if run.get("goal_recalculation", {}).get("status") == "planning":
                    run["goal_recalculation"].update(status="cancelled", finished_at=time.time())
                    run["pause"] = self.pause_details("coordinator_restarted", "Goal recalculation was interrupted. Existing goals are unchanged; retry Recalculate goals.", list(run["players"]))
                if run["state"] not in {"Paused", "Investigating"}:
                    run["resume_state"] = run["state"]
                    run["state"] = "Paused"
                    run["limitations"].append("Coordinator restarted; inputs held pending explicit Resume.")
                    run["pause"] = self.pause_details("coordinator_restarted", "The controller restarted. Bot inputs are held until you Resume.", list(run["players"]))
                    self.save_run(run)
                    record_task(self.store, run["id"], "restart", run["pause"]["reason"],
                        key=restart["id"], category="coordinator_restarted", event_id=restart["id"])
                for ps in run["players"].values():
                    ps["generation"] += 1
                    if ps["bound"]:
                        try:
                            await self.request(ps["worker_id"], "POST", "/pause", {"epoch": ps["epoch"]})
                        except Exception:
                            run["limitations"].append(f"Worker {ps['worker_id']} could not acknowledge recovery pause.")
                if run.get("human_control"):
                    try:
                        await self.close_human_control(run)
                        run["pause"] = self.pause_details("coordinator_restarted", "The controller restarted and closed human desktop access. Bots remain paused.", list(run["players"]))
                    except ValueError as exc:
                        run["limitations"].append(str(exc))
            self.save_run(run)
            generate_report(self.store, run["id"])
        publish_pending(self.store)

    async def loop(self):
        await self.recover()
        next_task_retry = time.monotonic() + 30
        while not self.stopping:
            try:
                if time.monotonic() >= next_task_retry:
                    publish_pending(self.store)
                    next_task_retry = time.monotonic() + 30
                for run in reversed(self.store.list("runs")):
                    if run["state"] == "Queued":
                        await self.admit(run)
                active = [r for r in self.store.list("runs") if r["state"] in ACTIVE_STATES]
                for run in active:
                    if not self.run_lock(run["id"]).locked() and (run["id"] not in self.ticks or self.ticks[run["id"]].done()):
                        self.ticks[run["id"]] = asyncio.create_task(self.tick_runner(run["id"]))
                self.store.put("settings", {"timestamp": time.time()}, "coordinator_heartbeat")
            except Exception as exc:
                # The separate watchdog owns expiry even when this loop cannot persist.
                print(f"ASHAD coordinator error: {type(exc).__name__}", flush=True)
            await asyncio.sleep(1)

    async def tick(self, run):
        now = time.time()
        if run.get("investigation_deadline") and now >= run["investigation_deadline"]:
            await self.finish(run["id"], "investigation_expired", terminate=True)
            return
        if await self.enforce_limits(run):
            return
        run["coordinator_heartbeat"] = now
        self.save_run(run)
        for player in run["plan"]["players"]:
            ps = run["players"][player["id"]]
            if not ps["bound"] or not ps["launched"]:
                continue
            monitoring_step = "sending worker heartbeat"
            try:
                await self.request(ps["worker_id"], "POST", "/heartbeat", {"epoch": ps["epoch"], "deadline": run.get("investigation_deadline")})
                monitoring_step = "reading worker status"
                status = await self.request(ps["worker_id"], "GET", "/status")
                ps["worker_status"] = status
                if run.get("human_control", {}).get("player_id") == player["id"] and status.get("human_control") is False:
                    run.pop("human_control", None)
                    run["pause"] = self.pause_details("manual_pause", "Remote desktop access closed. Bots remain paused; take over again or Resume when ready.", list(run["players"]))
                    ps["agent_status"] = {**(ps.get("agent_status") or {}), "phase": "paused", "detail": run["pause"]["reason"], "updated_at": now}
                    self.store.event(run["id"], "human_control_lost", {"player_id": player["id"], "reason": "Worker confirmed desktop access is closed"})
                ps["feed_captured_at"] = status.get("capture", {}).get("last_at")
                measured = status.get("build", {})
                for actual_key, expected_key in (("executable_sha256", "engine_sha256"), ("artifact_sha256", "artifact_sha256")):
                    if run["build"].get(expected_key) and measured.get(actual_key) != run["build"][expected_key]:
                        raise ValueError(f"Actual worker build {actual_key} differs from pinned artifact")
                if status.get("infrastructure_error"):
                    raise ValueError(f"Worker failure: {status['infrastructure_error']}")
                if not status.get("game_running", True) and now - ps["launched_at"] > 5:
                    raise ValueError("Game process exited; logs and capture required before classification")
                monitoring_step = "reading game telemetry"
                telemetry = await self.request(ps["worker_id"], "GET", "/telemetry")
                if "telemetry" in telemetry:
                    telemetry = telemetry["telemetry"] or {}
                previous = ps.get("telemetry") or {}
                if telemetry and telemetry.get("sequence") != previous.get("sequence"):
                    ps["telemetry_received_at"] = now
                    ps["telemetry"] = telemetry
                    if any(previous.get(k) != telemetry.get(k) for k in ("completed_quests", "handler", "votes", "connected", "world_synced", "resources", "world_id")):
                        self.store.event(run["id"], "monitor_observation", {"player_id": player["id"], "source": "read_only_diagnostics", "telemetry": telemetry})
                if telemetry:
                    self.check_identity(run, player, ps, telemetry)
                    ready = telemetry.get("connected") is True and telemetry.get("world_synced") is True and telemetry.get("world_loaded") is True
                    ps["phase"] = "ready" if ready else "synchronized" if telemetry.get("world_synced") else "connected" if telemetry.get("connected") else "authenticated" if telemetry.get("authenticated") else "launched"
                    if ready and not ps.get("ready_at"):
                        ps["ready_at"] = now
                        self.store.event(run["id"], "player_ready", {"player_id": player["id"], "telemetry_sequence": telemetry.get("sequence")})
                    if telemetry.get("sync_timed_out") and not ready:
                        raise ValueError("World synchronization timed out; readiness not established")
                if now - ps["launched_at"] > 180 and ps["phase"] != "ready" and run["state"] == "Starting":
                    raise ValueError("Initial player readiness deadline expired")
            except Exception as exc:
                self.save_run(run)
                await self.investigate(run["id"], "infrastructure_failure", f"{player['name']} while {monitoring_step}: {type(exc).__name__}: {exc}", players=[player["id"]])
                return
        host = next(p for p in run["plan"]["players"] if p["role"] == "host")
        host_state = run["players"][host["id"]]
        if host_state["phase"] == "ready" and not run.get("world_ready_at"):
            run["world_ready_at"] = now
            run["starting_milestones"] = list(host_state.get("telemetry", {}).get("completed_quests", []))
            world = host_state.get("telemetry", {}).get("world_id")
            run["world_namespace"] = f"{run['id']}:{world or 'unknown'}"
            self.store.event(run["id"], "world_ready", {"namespace": run["world_namespace"], "starting_milestones": run["starting_milestones"]})
        if run["state"] == "Starting" and run.get("world_ready_at"):
            for p in run["plan"]["players"]:
                if p["initial"] and not run["players"][p["id"]]["launched"]:
                    await self.start_player(run, p)
            if all(run["players"][p["id"]]["phase"] == "ready" for p in run["plan"]["players"] if p["initial"] and p["required"]):
                run.update(state="Running", validity="valid")
                self.store.event(run["id"], "session_running")
        self.save_run(run)
        if run["state"] in {"Starting", "Running", "Paused", "Investigating"}:
            await self.monitor(run, allow_decisions=run["state"] in {"Starting", "Running"})
            run = self.store.require("runs", run["id"])
            if run["state"] == "Running":
                await self.schedule(run)
            if run["state"] in {"Starting", "Running"}:
                for player in run["plan"]["players"]:
                    ps = run["players"][player["id"]]
                    key = (run["id"], player["id"])
                    if ps["launched"] and (key not in self.tasks or self.tasks[key].done()):
                        self.tasks[key] = asyncio.create_task(self.player_turn(run["id"], player["id"]))

    def check_identity(self, run, player, state, telemetry):
        expected = run["plan"]["transport"]
        if telemetry.get("transport") not in {None, "none", expected["mode"]}:
            raise ValueError("Actual transport differs from approved transport; offline fallback cannot pass")
        if telemetry.get("connected") and telemetry.get("transport") != expected["mode"]:
            raise ValueError("Connected client is not using the approved multiplayer transport")
        if expected["mode"] == "steam":
            if not telemetry.get("app_subscribed"):
                raise ValueError("Steam does not report licensed access to the selected game App ID")
            if str(telemetry.get("app_id")) != str(expected["app_id"]):
                raise ValueError("Actual Steam App ID differs from selected game")
            if str(telemetry.get("steam_id")) != str(state["identity"]) or not telemetry.get("authenticated"):
                account = self.store.get("accounts", player["account_id"])
                if account:
                    account["readiness"] = "human_sign_in_required"
                    self.store.put("accounts", account)
                raise ValueError("Steam requires human sign-in or has the wrong account")
        if player["role"] == "host" and telemetry.get("world_loaded") and telemetry.get("is_authority") is not True:
            raise ValueError("The planned host does not own world authority")
        if telemetry.get("world_loaded"):
            from integration.fixtures import character_key
            if telemetry.get("identity_key") and str(telemetry["identity_key"]) != character_key(state["identity"], expected["mode"]):
                raise ValueError("Actual game character identity differs from the assigned persistent identity")
            host = next(p for p in run["plan"]["players"] if p["role"] == "host")
            host_world = (run["players"][host["id"]].get("telemetry") or {}).get("world_id")
            if host_world and telemetry.get("world_id") != host_world:
                raise ValueError("Client joined a different world from the authoritative host")
            if player["role"] == "host" and run["plan"]["world_setup"]["mode"] != "fresh":
                fixture = self.store.require("fixtures", run["plan"]["world_setup"]["fixture_id"])
                if telemetry.get("world_id") != fixture["world_id"]:
                    raise ValueError("Restored host world differs from the approved saved-start package")

    async def enforce_limits(self, run):
        limits = run["plan"]["limits"]
        if limits["duration_seconds"] is not None and run.get("started_at") and time.time() - run["started_at"] >= limits["duration_seconds"]:
            await self.finish(run["id"], "limit_reached", limitation="Configured run duration reached.")
            return True
        usages = [u for u in self.store.list("usage") if u.get("run_id") == run["id"] and u.get("billing") != "subscription"]
        if limits["spend_usd"] is not None and usages:
            if any(u.get("estimated_cost_usd") is None for u in usages):
                await self.finish(run["id"], "limit_reached", limitation="Spend ceiling cannot be safely enforced because a call has unknown cost.")
                return True
            if sum(u["estimated_cost_usd"] for u in usages) >= limits["spend_usd"]:
                await self.finish(run["id"], "limit_reached", limitation="Configured estimated API spend ceiling reached.")
                return True
        size = sum(p.stat().st_size for p in self.store.run_dir(run["id"]).rglob("*") if p.is_file())
        size += sum((ps.get("worker_status", {}).get("resources", {}).get("evidence_bytes") or 0) for ps in run["players"].values())
        reserve = self.store.get("settings", "main", {}).get("storage_reserve_bytes", 1024**3)
        if size >= limits["storage_bytes"] or shutil.disk_usage(self.store.root).free < reserve:
            await self.finish(run["id"], "infrastructure_failure", terminate=True, limitation="Evidence storage budget or minimum disk headroom reached.")
            return True
        return False

    async def monitor(self, run, allow_decisions=True):
        now = time.time()
        host = next(p for p in run["plan"]["players"] if p["role"] == "host")
        h = run["players"][host["id"]]
        telemetry = h.get("telemetry") or {}
        progress = run["progress"]
        completed = telemetry.get("completed_quests", [])
        if completed != progress.get("completed_quests", []):
            run["last_progress_at"] = now
            self.store.event(run["id"], "milestone", {"completed_quests": completed, "source": "host_quest_authority"})
        progress.update(completed_quests=completed, handler=telemetry.get("handler"), votes=telemetry.get("votes"), world_simulating=telemetry.get("world_simulating", "unknown"), last_meaningful_progress=run.get("last_progress_at"), prerequisites=telemetry.get("active_quests", []))
        policy = run["plan"]["stall_policy"]
        for p in run["plan"]["players"]:
            ps = run["players"][p["id"]]
            if not ps["launched"]:
                continue
            t = ps.get("telemetry") or {}
            sim_tick = t.get("simulation_tick")
            if sim_tick is not None and sim_tick != ps.get("last_simulation_tick"):
                ps["last_simulation_tick"] = sim_tick
                ps["simulation_advanced_at"] = now
            if allow_decisions and t.get("world_simulating") is True and ps.get("simulation_advanced_at") and now - ps["simulation_advanced_at"] > policy["simulation_timeout_seconds"]:
                self.save_run(run)
                await self.investigate(run["id"], "suspected_game_freeze", "Simulation ticks stopped advancing while independent capture and telemetry remain available.", [p["id"]])
                return
            if allow_decisions and ps.get("telemetry_received_at") and now - ps["telemetry_received_at"] > policy["simulation_timeout_seconds"]:
                self.save_run(run)
                await self.investigate(run["id"], "suspected_game_freeze", "Read-only game heartbeat stopped while worker monitoring remained reachable; game freeze is suspected, not confirmed.", [p["id"]])
                return
            if allow_decisions and ps["invalid_actions"] >= policy["invalid_actions_threshold"]:
                self.save_run(run)
                await self.investigate(run["id"], "controller_failure", "Repeated rejected or unsuccessful input delivery invalidates measurement.", [p["id"]])
                return
        ready = [ps for ps in run["players"].values() if ps["phase"] == "ready"]
        host_world = telemetry.get("world_id")
        wrong_world = [ps["id"] for ps in ready if host_world and (ps.get("telemetry") or {}).get("world_id") != host_world]
        if wrong_world and allow_decisions:
            run["validity"] = "invalid"
            self.save_run(run)
            await self.investigate(run["id"], "infrastructure_failure", "Ready players belong to different world identities.", wrong_world)
            return
        disagreement = [ps["id"] for ps in ready if set((ps.get("telemetry") or {}).get("completed_quests", [])) != set(completed)]
        progress["disagreement"] = disagreement
        if disagreement:
            run.setdefault("divergence_since", now)
            if allow_decisions and now - run["divergence_since"] > policy["divergence_seconds"]:
                self.save_run(run)
                await self.investigate(run["id"], "client_disagreement", "Client quest state differs from authoritative host beyond the propagation window.", disagreement)
                return
        else:
            run.pop("divergence_since", None)
        criterion = run["plan"]["success_criteria"]
        def satisfied(t):
            if criterion["predicate"] == "quest_completed":
                return criterion["target"] in t.get("completed_quests", [])
            if criterion["predicate"] == "biome_unlocked":
                return criterion["target"] in t.get("unlocked_biomes", [])
            if criterion["predicate"] == "milestone":
                return criterion["target"] in t.get("completed_quests", [])
            if criterion["predicate"] == "item_quantity":
                inventories = t.get("diagnostics", {}).get("inventories", [])
                host_peer = str(telemetry.get("peer_id", 1))
                own = [inventory for inventory in inventories if inventory.get("path") in {f"/root/Game/Players/Player{host_peer}/Inventory", f"/root/Game/Players/Player{host_peer}/Hotbar"}]
                value = sum(item.get("item_count", 0) for inventory in own for item in inventory.get("items", []) if item.get("item_id") == criterion["target"] and isinstance(item.get("item_count"), (int, float)))
                return bool(own) and value >= criterion["quantity"]
            return False
        terminal = TERMINAL_QUEST in completed
        relevant = [ps for ps in ready if ps["launched"]]
        corroborated = bool(relevant) and all(satisfied(ps.get("telemetry") or {}) and ps.get("last_observation") and ps["last_observation"].get("timestamp", 0) >= run.get("completion_seen_at", 0) for ps in relevant)
        side_outcomes = {}
        for target in set(criterion.get("side_goals", [])) | {p["goal_id"] for p in run["plan"]["players"] if p["goal_id"] and p["goal_id"] != criterion["target"]}:
            done = target in completed or target in telemetry.get("unlocked_biomes", [])
            side_outcomes[target] = "achieved" if done else "unmet" if telemetry else "unknown"
        progress["side_goals"] = side_outcomes
        sides_done = all(side_outcomes.get(t) == "achieved" for t in criterion.get("side_goals", []))
        required_ready = (all(run["players"][p["id"]]["phase"] == "ready" for p in run["plan"]["players"] if p["required"]) if criterion["require_all"] else True) and sides_done
        if allow_decisions and satisfied(telemetry) and corroborated and required_ready:
            run.setdefault("completion_seen_at", now)
            if now - run["completion_seen_at"] >= criterion["corroboration_seconds"]:
                run["goal_complete"] = True
                run["demo_complete"] = terminal
                run["completion_origin"] = "already_completed_at_start" if criterion["target"] in run["starting_milestones"] else "achieved_during_run"
                self.save_run(run)
                await self.finish(run["id"], "completed")
                return
        elif not satisfied(telemetry) or not required_ready:
            run.pop("completion_seen_at", None)
        # A long stage alone never fires a stall: evidence must indicate a valid
        # unmet action with prerequisites satisfied, outside declared waits.
        diagnosis = telemetry.get("diagnostics", {})
        if allow_decisions and isinstance(diagnosis, dict) and diagnosis.get("blocking_action_failed") is True and diagnosis.get("prerequisites_satisfied") is True and not diagnosis.get("expected_wait") and now - run.get("last_progress_at", now) > policy["no_progress_seconds"]:
            self.save_run(run)
            await self.investigate(run["id"], "progression_blocker", "Valid action failed despite observed prerequisites; no expected production or vote wait accounts for the stall.")
            return
        self.save_run(run)

    async def schedule(self, run):
        now = time.time()
        for event in run["plan"]["events"]:
            state = run["events"][event["id"]]
            if state["status"] in {"complete", "skipped", "failed"}:
                continue
            if state["status"] == "executing":
                ps = run["players"][event["actor"]]
                done = not ps["launched"] if event["action"] == "disconnect" else ps["phase"] == "ready"
                if done:
                    state.update(status="complete", completed_at=now)
                    self.store.event(run["id"], "schedule_completed", {"event_id": event["id"], "actual_time": now})
                elif now - state["started_at"] >= event["timeout_seconds"]:
                    if state["attempts"] <= event["retries"]:
                        state.update(status="pending", retry_at=now + 2)
                    else:
                        state.update(status="failed", completed_at=now)
                        if event["failure_invalidates"]:
                            run["validity"] = "invalid"
                        self.store.event(run["id"], "schedule_failed", {"event": event, "reason": "Actual readiness timeout"})
                        self.save_run(run)
                        await self.investigate(run["id"], "infrastructure_failure", f"Scheduled event {event['id']} failed readiness.", [event["actor"]])
                        return
                continue
            if state.get("retry_at", 0) > now or state.get("rescheduled_at", 0) > now:
                continue
            if not all(run["events"][dep]["status"] == "complete" for dep in event["dependencies"]):
                continue
            basis = run.get("world_ready_at") if event["time_basis"] == "world_ready" else run.get("started_at")
            due = event_due(run, event, state, now)
            if not due:
                continue
            if state.get("held_during_pause") and event["overdue_policy"] == "skip":
                state.update(status="skipped", completed_at=now)
                self.store.event(run["id"], "schedule_skipped", {"event_id": event["id"], "reason": "Declared overdue policy"})
                continue
            player = next(p for p in run["plan"]["players"] if p["id"] == event["actor"])
            ps = run["players"][player["id"]]
            state.update(status="executing", started_at=now, attempts=state["attempts"]+1)
            self.store.event(run["id"], "schedule_started", {"event": event, "requested_time": (basis + float(event["value"])) if event["trigger"] == "elapsed" and basis else None, "actual_time": now})
            if event["action"] == "disconnect":
                if player["role"] == "host":
                    self.save_run(run)
                    await self.finish(run["id"], "host_lost", limitation="Scheduled host disconnect ends the world; host migration is not assumed.")
                    return
                await self.request(ps["worker_id"], "POST", "/disconnect", {"epoch": ps["epoch"], "terminate": False})
                ps.update(launched=False, phase="disconnected", generation=ps["generation"] + 1)
            elif not ps["launched"]:
                await self.start_player(run, player)
            self.save_run(run)
            # Stagger overdue joins on resume; explicitly concurrent fresh events
            # still execute together in this tick when not held during a pause.
            if state.get("held_during_pause"):
                break
        self.save_run(run)

    async def player_turn(self, run_id, player_id):
        try:
            return await self.structured.turn(run_id, player_id)
        except asyncio.CancelledError:
            self.store.event(run_id, "agent_task_cancelled", {"player_id": player_id, "reason": "Run control or controller shutdown cancelled the agent task."})
            raise
        except Exception as exc:
            reason = f"Agent task stopped unexpectedly: {type(exc).__name__}: {exc}"
            self.store.event(run_id, "agent_task_failed", {"player_id": player_id, "error": reason})
            if self.store.require("runs", run_id)["state"] in {"Starting", "Running"}:
                self.structured.status(run_id, player_id, "error", reason, last_error=reason, error_at=time.time())
                await self.investigate(run_id, "controller_failure", reason, [player_id])

    async def flag(self, run_id, actual, expected="Unknown; requires review", players=None, category="manual_flag", confidence="unknown", expected_source=None):
        run = self.store.require("runs", run_id)
        players = players or list(run["players"])
        signature = hashlib.sha256(json.dumps([category, run["plan"]["build_id"], sorted(players), run["progress"].get("completed_quests"), expected_source], sort_keys=True).encode()).hexdigest()
        # Deduplicate only when source/category/stage/actors match, not by wording.
        existing = next((i for i in self.store.list("incidents") if i["run_id"] == run_id and i.get("signature") == signature and expected_source and i["status"] == "suspected"), None)
        incident = existing or {"id": uid("incident_"), "run_id": run_id, "session_id": run_id, "category": category, "status": "suspected", "classification": "unresolved", "confidence": confidence, "severity": "review_required", "expected": expected, "expected_source": expected_source, "actual": actual, "players": players, "stage": run["progress"].get("completed_quests", []), "signature": signature, "first_occurrence": time.time(), "occurrences": [], "evidence_ids": [], "review_history": [], "reproduction": {"scenario_id": run["scenario_id"], "revision": run["scenario_revision"], "build": run["build"], "transport": run["plan"]["transport"]}}
        event = self.store.event(run_id, "incident_flag", {"incident_id": incident["id"], "actual": actual, "players": players, "category": category})
        incident["occurrences"].append({"timestamp": time.time(), "event_id": event["id"], "actual": actual})
        incident["evidence_ids"].append(event["id"])
        incident["last_occurrence"] = time.time()
        self.store.put("incidents", incident)
        pinned_evidence, pin_failures = [], []
        for player_id in players:
            ps = run["players"].get(player_id)
            if ps and ps["bound"] and run["state"] != "Ended":
                try:
                    pinned = await self.request(ps["worker_id"], "POST", "/pin", {"epoch": ps["epoch"], "incident_id": incident["id"], "before_seconds": run["plan"]["evidence_policy"]["pre_seconds"], "after_seconds": run["plan"]["evidence_policy"]["post_seconds"]})
                    pinned_evidence.append({"incident_id": incident["id"], "player_id": player_id, "worker_id": ps["worker_id"], "pin": pinned})
                except Exception as exc:
                    pin_failures.append(f"Incident {incident['id']} {player_id}: pin unavailable ({type(exc).__name__}).")
        # Pinning is asynchronous while gameplay/controls may continue. Commit only this operation's fields.
        run = self.store.require("runs", run_id)
        run["evidence"].extend(pinned_evidence)
        run["limitations"].extend(pin_failures)
        if pin_failures:
            run["evidence_status"] = "incomplete"
        self.save_run(run)
        generate_report(self.store, run_id)
        return incident

    async def investigate(self, run_id, category, reason, players=None):
        await self.cancel_tick(run_id)
        run = self.store.require("runs", run_id)
        if run["state"] in {"Ended", "Stopping", "Queued"}:
            return run
        if run["state"] == "Investigating":
            # A new failure can occur while inputs are already held (e.g. a crash).
            key = json.dumps([run.get("investigation_started_at", run.get("investigation_deadline")), category, reason, sorted(players or run["players"])])
            record_task(self.store, run_id, "investigation", reason, key=key, category=category)
            publish_pending(self.store)
            return run  # A repeated alert must never restart the 30-minute clock.
        run["resume_state"] = "Running" if run.get("world_ready_at") else "Starting"
        run.update(state="Investigating", investigation_deadline=time.time()+1800, investigation_started_at=time.time())
        run["pause"] = self.pause_details(category, reason, players or list(run["players"]), run["investigation_started_at"])
        if category in {"infrastructure_failure", "controller_failure", "capture_or_controller_failure"}:
            run["validity"] = "invalid"
        self.save_run(run)  # Persist deadline before any remote IO.
        event = self.store.event(run_id, "investigation_started", {"category": category, "reason": reason, "players": run["pause"]["players"], "deadline": run["investigation_deadline"], "world_may_continue": True})
        key = json.dumps([run["investigation_started_at"], category, reason, sorted(players or run["players"])])
        record_task(self.store, run_id, "investigation", reason, key=key, category=category, event_id=event["id"])
        await self.hold_inputs(run)
        if run.get("human_control"):
            try:
                await self.close_human_control(run)
            except ValueError as exc:
                run["limitations"].append(str(exc))
                self.save_run(run)
        publish_pending(self.store)
        await self.flag(run_id, reason, players=players, category=category)
        return self.store.require("runs", run_id)

    async def hold_inputs(self, run):
        run.setdefault("inputs_held_since", time.time())
        for state in run["events"].values():
            if state["status"] == "pending":
                state["held_during_pause"] = True
        for ps in run["players"].values():
            ps["generation"] += 1
            ps["agent_status"] = {**(ps.get("agent_status") or {}), "phase": "paused", "detail": (run.get("pause") or {}).get("reason", "Bot inputs held by run control."), "started_at": time.time(), "updated_at": time.time(), "wait_until": None}
        self.save_run(run)
        await self.cancel_players(run["id"])
        async def hold(ps):
            if ps["bound"]:
                try:
                    result = await self.request(ps["worker_id"], "POST", "/pause", {"epoch": ps["epoch"]})
                    await self.request(ps["worker_id"], "POST", "/heartbeat", {"epoch": ps["epoch"], "deadline": run.get("investigation_deadline")})
                    return {"worker_id": ps["worker_id"], "ack": result}
                except Exception as exc:
                    return {"worker_id": ps["worker_id"], "ack": "unknown", "error": type(exc).__name__}
        results = await asyncio.gather(*(hold(ps) for ps in run["players"].values()))
        self.store.event(run["id"], "inputs_released", {"workers": [r for r in results if r], "world_may_continue": True})
        return results

    async def close_human_control(self, run):
        control = run.get("human_control")
        if not control:
            return
        control["status"] = "unknown"
        self.save_run(run)
        ps = run["players"][control["player_id"]]
        try:
            result = await self.request(ps["worker_id"], "POST", "/desktop/finish", {"epoch": ps["epoch"]})
        except Exception as exc:
            raise ValueError(f"Could not close desktop access; bots remain paused. {exc}") from exc
        if result.get("human_control") is not False:
            raise ValueError("Desktop access could not be confirmed closed; bots remain paused.")
        run.pop("human_control", None)
        self.save_run(run)
        self.store.event(run["id"], "human_control_finished", {"player_id": ps["id"]})

    def external_control(self, run_id):
        return any(rid == run_id and owner != "agent" for (rid, _), owner in self.structured.control_owners.items())

    async def recalculate_goals(self, run_id, command_id):
        """Pause, rebuild from fresh player state, and require an explicit subsequent Resume."""
        if not isinstance(command_id, str) or not 1 <= len(command_id) <= 100:
            raise ValueError("Goal recalculation requires an idempotency ID of 1–100 characters")
        scope, key, started = "recalculate:" + run_id, None, False
        task = asyncio.current_task()
        try:
            async with self.run_lock(run_id):
                await self.cancel_tick(run_id)
                run = self.store.require("runs", run_id)
                if self.store.command(scope, command_id):
                    return run
                if (run["state"] not in {"Starting", "Running", "Paused", "Investigating"}
                        or not run.get("subgoal_plan") or len(run["players"]) != 1):
                    raise ValueError("Recalculate goals requires a live single-player instance with a goal plan")
                player_id = run["subgoal_plan"]["player_id"]
                ps = run["players"].get(player_id)
                if not ps or not ps["bound"] or not ps["launched"]:
                    raise ValueError("Wait for the player to launch before recalculating goals")
                if run.get("human_control") or self.external_control(run_id):
                    raise ValueError("Hand back human or external control before recalculating goals")
                if run.get("goal_recalculation", {}).get("status") == "planning":
                    raise ValueError("Goal recalculation is already in progress; wait or choose Pause to cancel")
                if run["state"] in {"Starting", "Running"}:
                    run["resume_state"] = run["state"]
                run.update(state="Paused", investigation_deadline=None,
                           pause=self.pause_details("goal_recalculation", "Recalculating goals from fresh game state. Inputs remain held.", [player_id]),
                           goal_recalculation={"command_id": command_id, "status": "planning", "started_at": time.time()})
                self.save_run(run)
                started = True
                self.store.event(run_id, "goal_recalculation_started", {"player_id": player_id, "command_id": command_id})
                results = await self.hold_inputs(run)
                if any(not result or not isinstance(result["ack"], dict) or result["ack"].get("lifecycle") != "paused" for result in results):
                    raise ValueError("Worker did not confirm paused input; inspect its connection before retrying")
                key, generation = (run_id, player_id), run["players"][player_id]["generation"]
                self.tasks[key] = task  # Existing Stop/Pause/shutdown cancellation also cancels manual inference.

            def current():
                live = self.store.require("runs", run_id)
                return (live["state"] == "Paused" and live["players"][player_id]["generation"] == generation
                        and live.get("goal_recalculation", {}).get("command_id") == command_id
                        and live["goal_recalculation"]["status"] == "planning"
                        and not live.get("human_control") and not self.external_control(run_id))

            # Do not hold the lifecycle lock during model I/O: paused games still need monitor heartbeats.
            pending = self.structured.pending.get(key) or (run["players"][player_id].get("pending_action") or {}).get("action_id")
            if pending:
                result = await self.structured.call_game(run_id, player_id, "get_action_status", {"action_id": pending})
                if not current():
                    raise asyncio.CancelledError()
                self.store.event(run_id, "structured_action_outcome", {"player_id": player_id, "result": result})
                if result.get("status") not in {"completed", "failed", "rejected", "expired", "cancelled"}:
                    raise ValueError("The previous action outcome is unresolved; inspect it before recalculating goals")
                self.structured.clear_pending(run_id, player_id, generation)
            observation = await self.structured.observe(run_id, player_id)
            if not current():
                raise asyncio.CancelledError()
            live = self.store.require("runs", run_id)
            previous = copy.deepcopy(live["subgoal_plan"])
            plan = new_plan(previous["primary_goal"], player_id, run_id)
            plan["revision"] = previous["revision"] + 1
            subgoals.reconcile(plan, observation, generation)
            player = next(p for p in live["plan"]["players"] if p["id"] == player_id)
            context = self.structured.decision_context({**live, "subgoal_plan": plan}, player, observation)
            context["replanning_reason"] = "The operator requested a new plan from current state. Old setup goals may be obsolete; keep the primary objective unchanged."
            self.store.event(run_id, "subgoal_planning_context", {"player_id": player_id, "revision": plan["revision"], "context": context})
            proposal = await self.ai.plan_subgoals(context, run_id)
            if not current():
                raise asyncio.CancelledError()
            fresh = await self.structured.observe(run_id, player_id)
            async with self.run_lock(run_id):
                await self.cancel_tick(run_id)
                if not current():
                    raise asyncio.CancelledError()
                live = self.store.require("runs", run_id)
                if (live["subgoal_plan"]["revision"] != previous["revision"]
                        or subgoals.planning_scene(fresh) != subgoals.planning_scene(observation)):
                    raise ValueError("Game binding, visible controls, or goal plan changed during recalculation; retry from fresh state")
                revised = subgoals.apply_decision(plan, proposal, context)
                verified = subgoals.reconcile(revised, fresh, generation)
                live["subgoal_plan"] = revised
                live["goal_recalculation"].update(status="completed", finished_at=time.time())
                live["pause"] = self.pause_details("goal_recalculation", "Goals recalculated. Review the current goal, then Resume when ready.", [player_id])
                if revised["status"] == "blocked":
                    live["pause"]["reason"] = "Goals recalculated but still need review: " + revised["reason"]
                # Retain the old plan before replacement. Metadata and retry acknowledgement commit together.
                archive = self.store.event(run_id, "subgoal_plan_recalculation_prepared", {"player_id": player_id, "command_id": command_id,
                    "previous_plan": previous, "retired_goal_ids": [g["id"] for g in previous["goals"] if g["id"] != previous["root_id"] and g["status"] != "completed"],
                    "plan": revised, "observation": fresh, "verified_goal_ids": verified})
                live["goal_recalculation"].update(archive_event_id=archive["id"], revision=revised["revision"])
                live["players"][player_id]["agent_status"].update(detail=live["pause"]["reason"], updated_at=time.time())
                with self.store.transaction():
                    self.save_run(live)
                    self.store.command(scope, command_id, {"acknowledged": True, "revision": revised["revision"]})
                self.structured.waits.pop(key, None)
                self.structured.last_decisions.pop(key, None)
                return live
        except Exception as exc:
            if started:
                async with self.run_lock(run_id):
                    await self.cancel_tick(run_id)
                    live = self.store.require("runs", run_id)
                    if (live.get("goal_recalculation", {}).get("command_id") == command_id
                            and live["goal_recalculation"]["status"] == "planning"):
                        live["goal_recalculation"].update(status="failed", error=str(exc), finished_at=time.time())
                        live["pause"] = self.pause_details("goal_recalculation", "Goal recalculation failed; inputs remain held. " + str(exc), list(live["players"]))
                        self.save_run(live)
                        self.store.event(run_id, "goal_recalculation_failed", {"command_id": command_id, "reason": str(exc)})
            raise
        finally:
            # No await here: cancellation may interrupt even the error handler's lock acquisition.
            # Lifecycle callers already hold that lock while awaiting this task's termination.
            try:
                if started:
                    live = self.store.require("runs", run_id)
                    operation = live.get("goal_recalculation", {})
                    if operation.get("command_id") == command_id and operation.get("status") == "planning":
                        operation.update(status="cancelled", finished_at=time.time())
                        self.save_run(live)
                        self.store.event(run_id, "goal_recalculation_cancelled", {"command_id": command_id})
            finally:
                if key and self.tasks.get(key) is task:
                    self.tasks.pop(key)

    async def control(self, run_id, command, reason="", command_id=None, **options):
        if not command_id:
            raise ValueError("Lifecycle command requires an idempotency ID")
        if not isinstance(reason, str):
            raise ValueError("Recovery instructions must be text")
        reason = reason.strip()
        if command == "takeover_finish" and type(options.get("resume", True)) is not bool:
            raise ValueError("Resume must be true or false")
        resuming = command == "resume" or (command == "takeover_finish" and options.get("resume", True))
        async with self.run_lock(run_id), (self.lock if resuming else nullcontext()):
            await self.cancel_tick(run_id)
            cached = self.store.command(run_id, command_id)
            if cached:
                return self.store.require("runs", run_id)
            run = self.store.require("runs", run_id)
            if run["state"] == "Ended":
                raise ValueError("Run has ended; create a rerun")
            # Rejected commands are not interventions. Resolve their targets and
            # validate options before recording intent or sending worker input.
            if command not in {"stop", "terminate", "pause", "resume", "extend", "manual_event", "goal", "takeover", "takeover_finish"}:
                raise ValueError("Unknown lifecycle command")
            recalculating = run.get("goal_recalculation", {}).get("status") == "planning"
            if recalculating and command not in {"stop", "terminate", "pause"}:
                raise ValueError("Wait for goal recalculation or cancel it before changing run control")
            if command == "takeover_finish" and not run.get("human_control") and run["state"] in {"Starting", "Running"}:
                return run
            if command == "takeover_finish" and run["state"] not in {"Paused", "Investigating"}:
                raise ValueError("Run is not paused or investigating")
            if resuming and run["state"] not in {"Paused", "Investigating"}:
                raise ValueError("Run is not paused or investigating")
            if command == "takeover":
                ps = run["players"].get(options.get("player_id"))
                if run["state"] not in {"Starting", "Running", "Paused", "Investigating"} or not ps or not ps["bound"] or not ps["launched"]:
                    raise ValueError("Take over requires a launched player in a live run")
                if run.get("human_control", {}).get("player_id", ps["id"]) != ps["id"]:
                    raise ValueError("Hand back the current desktop before taking over another player")
                if run.get("human_control"):
                    status = await self.request(ps["worker_id"], "GET", "/status")
                    if status.get("human_control") is True and status.get("lifecycle") == "paused":
                        run["human_control"]["status"] = "active"
                        self.save_run(run)
                        self.store.command(run_id, command_id, {"acknowledged": True})
                        return run
            if resuming:
                if self.draining or self.stopping:
                    raise ValueError("The controller is draining; bot resume is blocked.")
                self.require_instance_slot(run_id)
                status = await self.ai.validate(run.get("model") or run["plan"].get("model", MODEL))
                if status.get("status") != "ready":
                    raise ValueError(status.get("message") or "Connect your ChatGPT subscription before resuming.")
                if not self.store.reserve(run_id, [("instance", "global")]):
                    raise InstanceConflict([owner for owner in self.occupied_instances() if owner["run_id"] != run_id])
            if command == "extend":
                if run["state"] != "Investigating":
                    raise ValueError("No active investigation to extend")
                seconds = options.get("seconds", 1800)
                if not isinstance(seconds, (int, float)) or not 0 < seconds <= 86400:
                    raise ValueError("Extension must be 1–86400 seconds")
            if command == "manual_event":
                event_id = options.get("event_id")
                if not any(e["id"] == event_id and e["trigger"] == "manual" for e in run["plan"]["events"]):
                    raise ValueError("Unknown manual event")
            if command == "goal":
                player = next((p for p in run["plan"]["players"] if p["id"] == options.get("player_id")), None)
                if not player or not options.get("goal"):
                    raise ValueError("Goal intervention requires player and goal")
            intervention = {"command": command, "reason": reason, "timestamp": time.time(), "command_id": command_id, **options}
            run["interventions"].append(intervention)
            self.save_run(run)
            self.store.event(run_id, "intervention", intervention)
            if command in {"stop", "terminate"}:
                await self.finish(run_id, "user_stopped", terminate=command == "terminate")
            elif command == "takeover":
                if run["state"] in {"Starting", "Running"}:
                    run["resume_state"] = run["state"]
                run.update(state="Paused", investigation_deadline=None)
                run["human_control"] = run.get("human_control") or {"player_id": ps["id"], "worker_id": ps["worker_id"],
                    "started_at": time.time(), "desktop_url": options.get("desktop_url", "")}
                run["human_control"]["status"] = "opening"
                run["pause"] = self.pause_details("human_control", f"You are taking control of {ps['id']}. All bots are paused.", list(run["players"]))
                self.save_run(run)  # Keep ownership even if the remote start acknowledgement is lost.
                event = self.store.event(run_id, "human_control_requested", run["human_control"])
                record_task(self.store, run_id, "pause", run["pause"]["reason"], key=event["id"], category="manual_pause", event_id=event["id"])
                try:
                    results = await self.hold_inputs(run)
                    for player_state, result in zip(run["players"].values(), results):
                        if result and (not isinstance(result.get("ack"), dict) or
                                (player_state["launched"] and result["ack"].get("lifecycle") != "paused")):
                            raise ValueError("Not every worker confirmed its inputs are paused")
                    result = await self.request(ps["worker_id"], "POST", "/desktop/start", {"epoch": ps["epoch"]})
                    if result.get("human_control") is not True:
                        raise ValueError("Worker did not confirm human desktop control")
                    run["human_control"]["status"] = "active"
                    ps["agent_status"].update(phase="human_control", detail="You control this desktop. Hand back to bots when ready.")
                    self.save_run(run)
                    self.store.event(run_id, "human_control_started", run["human_control"])
                except Exception as exc:
                    try:
                        await self.close_human_control(run)
                    except Exception:
                        pass  # Retain ownership so a retry can close an ambiguously opened viewer.
                    run["pause"] = self.pause_details("infrastructure_failure", f"Take over failed; bots remain paused. {exc}", list(run["players"]))
                    self.save_run(run)
                    self.store.event(run_id, "human_control_failed", {"reason": run["pause"]["reason"]})
                    raise ValueError(run["pause"]["reason"]) from exc
                finally:
                    publish_pending(self.store)
            elif command == "takeover_finish" and not resuming:
                await self.close_human_control(run)
                run["pause"] = self.pause_details("manual_pause", "Human desktop access closed. Bots remain paused until you Resume.", list(run["players"]))
                self.save_run(run)
            elif command == "pause":
                if recalculating:
                    await self.cancel_players(run_id)
                    run = self.store.require("runs", run_id)
                    run["pause"] = self.pause_details("goal_recalculation", "Recalculation cancelled. Existing goals are unchanged; retry or Resume.", list(run["players"]))
                    self.save_run(run)
                if run["state"] in {"Starting", "Running"}:
                    run["resume_state"] = run["state"]
                    run["state"] = "Paused"
                    run["pause"] = self.pause_details("manual_pause", reason or "You paused the bots.", list(run["players"]))
                    self.save_run(run)
                    event = self.store.event(run_id, "run_paused", run["pause"])
                    record_task(self.store, run_id, "pause", run["pause"]["reason"],
                        key=event["id"], category="manual_pause", event_id=event["id"])
                    await self.hold_inputs(run)
                    publish_pending(self.store)
            elif resuming:
                await self.close_human_control(run)
                for ps in run["players"].values():
                    ps["generation"] += 1
                    ps["last_observation"] = None
                    ps["structured_context"] = None
                self.save_run(run)
                await self.cancel_players(run_id)
                try:
                    for ps in run["players"].values():
                        if ps["bound"] and ps["launched"]:
                            result = await self.request(ps["worker_id"], "POST", "/resume", {"epoch": ps["epoch"]})
                            if result.get("game_running") is False or (result.get("lifecycle") is not None and result["lifecycle"] != "running"):
                                raise ValueError(f"{ps['id']}: game did not resume; terminate and rerun if its process has exited")
                            await self.request(ps["worker_id"], "POST", "/heartbeat", {"epoch": ps["epoch"], "deadline": None})
                except Exception as exc:
                    message = f"Resume failed; bots remain paused. {type(exc).__name__}: {exc}"
                    run["pause"] = self.pause_details("infrastructure_failure", message, list(run["players"]))
                    self.save_run(run)
                    await self.hold_inputs(run)
                    event = self.store.event(run_id, "resume_failed", {"reason": message})
                    record_task(self.store, run_id, "resume_failed", message,
                        key=f"resume:{command_id}", category="infrastructure_failure", event_id=event["id"])
                    publish_pending(self.store)
                    raise ValueError(message) from exc
                run["investigation_deadline"] = None
                run["pause"] = None
                run["recovery_instruction"] = reason
                for ps in run["players"].values():
                    ps["invalid_actions"] = 0
                    ps["simulation_advanced_at"] = time.time()
                    ps["telemetry_received_at"] = time.time()
                    ps["agent_status"] = {**(ps.get("agent_status") or {}), "phase": "ready", "detail": "Resumed; acquiring fresh game state.", "started_at": time.time(), "updated_at": time.time(), "action_id": None, "wait_until": None}
                held_seconds = max(0, time.time() - run.pop("inputs_held_since", time.time()))
                for event in run["plan"]["events"]:
                    state = run["events"][event["id"]]
                    if state["status"] == "executing":
                        state["started_at"] += held_seconds
                    overdue = event_due(run, event, state, time.time())
                    state["held_during_pause"] = state["status"] == "pending" and bool(overdue)
                    if state.get("held_during_pause") and event["overdue_policy"] == "reschedule":
                        state["rescheduled_at"] = time.time() + max(1, float(event["value"]) if event["trigger"] == "elapsed" else 1)
                run["state"] = run.get("resume_state", "Running")
                self.save_run(run)
            elif command == "extend":
                run["investigation_deadline"] = max(time.time(), run["investigation_deadline"]) + seconds
                self.save_run(run)
                await self.hold_inputs(run)
            elif command == "manual_event":
                run["events"][event_id]["manual_triggered"] = True
                self.save_run(run)
            elif command == "goal":
                player["goal"] = options["goal"]
                if run.get("subgoal_plan", {}).get("player_id") == player["id"]:
                    self.store.event(run_id, "subgoal_plan_reset", {"previous_plan": run["subgoal_plan"], "reason": "Primary objective changed"})
                    run["subgoal_plan"] = new_plan(options["goal"], player["id"], run_id)
                    run["plan"]["primary_goal"] = options["goal"]
                run["players"][player["id"]]["generation"] += 1
                self.save_run(run)
                self.store.event(run_id, "goal_changed", {"player_id": player["id"], "goal": player["goal"]})
            self.store.command(run_id, command_id, {"acknowledged": True})
            generate_report(self.store, run_id)
            return self.store.require("runs", run_id)

    async def finish(self, run_id, reason, terminate=False, limitation=None):
        await self.cancel_tick(run_id)
        run = self.store.require("runs", run_id)
        if run["state"] == "Ended":
            return run
        run.update(state="Stopping", ending_reason=reason)
        if limitation:
            run["limitations"].append(limitation)
        if reason in {"infrastructure_failure", "controller_failure", "host_lost"}:
            run["validity"] = "invalid"
        for ps in run["players"].values():
            ps["generation"] += 1
        self.save_run(run)
        await self.cancel_players(run_id)
        run = self.store.require("runs", run_id)  # Retain cancellation outcomes written by the stopped tasks.
        event = self.store.event(run_id, "stopping", {"reason": reason, "terminate": terminate, "save": "not_promised" if terminate else "bounded_graceful_attempt"})
        record_task(self.store, run_id, "stop", limitation or reason, key="stop", category=reason, event_id=event["id"])
        async def stop(ps):
            if not ps["bound"]:
                return True, {"status": "never_launched"}
            try:
                result = await asyncio.wait_for(self.request(ps["worker_id"], "POST", "/stop", {"epoch": ps["epoch"], "terminate": terminate}), 8 if terminate else 20)
                confirmed = result.get("teardown_confirmed", result.get("stopped", False)) is True
                return confirmed, result
            except Exception as exc:
                try:
                    result = await asyncio.wait_for(self.request(ps["worker_id"], "POST", "/stop", {"epoch": ps["epoch"], "terminate": True}), 5)
                    return result.get("teardown_confirmed", result.get("stopped", False)) is True, result
                except Exception:
                    return False, {"status": "unknown", "error": type(exc).__name__}
        results = await asyncio.gather(*(stop(ps) for ps in run["players"].values()))
        for ps, (confirmed, result) in zip(run["players"].values(), results):
            ps.update(teardown_confirmed=confirmed, phase="ended" if confirmed else "teardown_unknown", launched=False if confirmed else ps["launched"])
            ps["agent_status"] = {**(ps.get("agent_status") or {}), "phase": "ended", "detail": f"Run ended: {reason}." if confirmed else "Teardown unconfirmed; use Reconcile teardown to retry.", "started_at": time.time(), "updated_at": time.time(), "wait_until": None}
            self.store.event(run_id, "teardown_result", {"player_id": ps["id"], "worker_id": ps["worker_id"], "confirmed": confirmed, "result": result})
        if all(x[0] for x in results):
            self.store.release(run_id)
        human = run.get("human_control")
        if human and run["players"][human["player_id"]]["teardown_confirmed"]:
            run.pop("human_control", None)
        if not all(x[0] for x in results):
            run["limitations"].append("Teardown was not confirmed; worker/account leases remain reserved pending reconciliation.")
            run["evidence_status"] = "incomplete"
        run.update(state="Ended", ended_at=time.time(), investigation_deadline=None)
        if run["evidence_status"] == "collecting":
            run["evidence_status"] = "complete" if not run["limitations"] else "partial"
        self.save_run(run)
        event = self.store.event(run_id, "run_ended", {"reason": reason, "validity": run["validity"], "goal_complete": run["goal_complete"], "demo_complete": run["demo_complete"], "evidence_status": run["evidence_status"]})
        if not all(x[0] for x in results):
            record_task(self.store, run_id, "teardown_failed", "Game teardown was not confirmed; worker/account leases remain reserved.",
                key="teardown_failed", category="infrastructure_failure", event_id=event["id"])
        publish_pending(self.store)
        generate_report(self.store, run_id)
        await self.collect_evidence(run_id)
        return run

    async def collect_evidence(self, run_id):
        """Copy retained evidence after processes stop; never delay enforced termination."""
        run = self.store.require("runs", run_id)
        root = self.store.run_dir(run_id)
        files = run.get("retained_files", [])
        existing = {f["name"] for f in files}
        for player_id, ps in run["players"].items():
            if not ps["bound"]:
                continue
            client = self.client(ps["worker_id"])
            if not hasattr(client, "evidence_index"):
                continue
            try:
                listing = await client.evidence_index(run_id)
                for item in listing.get("files", []):
                    relative = Path(item["path"])
                    if relative.is_absolute() or ".." in relative.parts or relative.parts[0] in {"rolling", "frames"}:
                        continue
                    name = f"workers/{player_id}/{relative.as_posix()}"
                    destination = (root / name).resolve()
                    if not destination.is_relative_to(root):
                        continue
                    if item.get("bytes", 0) > run["plan"]["limits"]["storage_bytes"]:
                        raise ValueError("Evidence file exceeds run storage budget")
                    blob = await client.evidence(item["path"], run_id)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(blob)
                    if name not in existing:
                        files.append({"name": name, "player_id": player_id, "kind": "image" if relative.suffix in {".jpg", ".png"} else "video" if relative.suffix in {".mp4", ".mkv"} else "log", "incident_id": relative.parts[1] if relative.parts[0] == "pinned" and len(relative.parts) > 2 else None, "bytes": len(blob)})
                        existing.add(name)
            except Exception as exc:
                note = f"Retained evidence transfer for {player_id} incomplete ({type(exc).__name__}); original worker evidence remains local."
                if note not in run["limitations"]:
                    run["limitations"].append(note)
                run["evidence_status"] = "incomplete"
        run["retained_files"] = files
        self.save_run(run)
        generate_report(self.store, run_id)
        return files

