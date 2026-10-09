"""Shared structured-play handlers for Decisions, dashboard inspection and MCP."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time

from .ai import ProviderError
from .repository import Repository
from .store import uid
from . import subgoals


def meaningful_signature(observation):
    """Ignore observation clocks and cosmetic-only revisions, preserve actionable facts."""
    return hashlib.sha256(json.dumps({k: observation.get(k) for k in
        ("generation", "world_id", "ready", "state", "entities", "active_action")},
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def prompt_observation(observation, max_chars=28000):
    result = copy.deepcopy(observation)
    entities = result.pop("entities", [])
    result.pop("known_locations", None)
    result["entities"] = []
    # Interactive targets before decorative labels. Preserve full state in evidence.
    for entity in sorted(entities, key=lambda e: not bool(e.get("actions"))):
        if len(json.dumps(result)) + len(json.dumps(entity)) > max_chars:
            result["complete"] = False
            result["context_truncated"] = True
            break
        result["entities"].append(entity)
    return result


def wait_signature(observation):
    state = observation.get("state", {})
    player = state.get("player", {})
    # Timers, animation, camera motion and moving bystanders must not cause inference.
    facts = {"generation": observation.get("generation"), "ready": observation.get("ready"),
             "inventory": player.get("inventory"), "hotbar": player.get("hotbar"),
             "objectives": state.get("objectives"), "completed": state.get("completed_quests"),
             "tiles": state.get("tiles"),
             "targets": [{"id": e.get("id"), "actions": e.get("actions"),
                          "crafting": e.get("state", {}).get("is_crafting")}
                         for e in observation.get("entities", []) if e.get("kind") != "ground_item"]}
    return hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()


class StructuredPlayers:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.store = coordinator.store
        self.waits = {}
        self.last_decisions = {}
        self.pending = {}
        self.generations = {}
        self.control_owners = {}

    def binding(self, run_id, player_id):
        run = self.store.require("runs", run_id)
        if run["plan"].get("play_mode", "structured") != "structured":
            raise ValueError("Run does not enable structured play")
        ps = run["players"][player_id]
        if not ps["bound"] or not ps["launched"]:
            raise ValueError("Player game is not launched")
        return run, ps

    def status(self, run_id, player_id, phase, detail, generation=None, **fields):
        run = self.store.require("runs", run_id)
        ps = run["players"][player_id]
        if generation is not None and (ps["generation"] != generation or run["state"] not in {"Starting", "Running"}):
            return
        previous = ps.get("agent_status") or {}
        ps["agent_status"] = {**previous, "phase": phase, "detail": detail, "updated_at": time.time(),
                              "action_id": None, "wait_until": None, **fields}
        if phase != previous.get("phase"):
            ps["agent_status"]["started_at"] = time.time()
        ps["activity"] = detail
        self.store.put("runs", run)

    def clear_pending(self, run_id, player_id, generation):
        self.pending.pop((run_id, player_id), None)
        run = self.store.require("runs", run_id)
        if run["players"][player_id]["generation"] == generation:
            run["players"][player_id]["pending_action"] = None
            self.store.put("runs", run)

    async def call_game(self, run_id, player_id, operation, arguments=None):
        run, ps = self.binding(run_id, player_id)
        return await self.coordinator.request(ps["worker_id"], "POST", "/game", {
            "run_id": run_id, "player_id": player_id, "epoch": ps["epoch"],
            "operation": operation, "arguments": arguments or {}})

    async def observe(self, run_id, player_id):
        result = await self.call_game(run_id, player_id, "observe")
        if result.get("schema_version") != 1 or result.get("run_id") != run_id or result.get("player_id") != player_id:
            raise ValueError("Structured game observation identity/schema mismatch")
        if not result.get("observation_id"):
            raise ValueError("Game observation is missing its observation ID")
        if subgoals.stale(result.get("timestamp")):
            raise ValueError("Structured game context is stale")
        # Game and controller clocks may disagree; planning freshness uses the controller's receipt time.
        result["received_at"] = time.time()
        run, ps = self.binding(run_id, player_id)
        ps["structured_context"] = result
        ps["last_observation"] = {"observation_id": result.get("observation_id"), "timestamp": result.get("timestamp"), "source": "structured_game"}
        signature = wait_signature(result)
        if signature != ps.get("observation_signature"):
            ps["last_state_change_at"] = time.time()
            ps["observation_signature"] = signature
        self.store.put("runs", run)
        return result

    def guidance(self, run, player, observation):
        active = observation.get("state", {}).get("objectives", [])
        result = {"level": run["plan"].get("guidance", "guided"), "goal": player["goal"], "active": active,
                  "completion": "Work toward the primary objective. The operator ends this instance; do not assume every objective requires full demo completion." if run["plan"]["success_criteria"]["predicate"] == "manual" else "The approved completion predicate must be verified by the controller."}
        if result["level"] == "guided":
            result["next_steps"] = (["Use the visible menu controls to create/select the world as host, or join as configured.",
                                     "Complete character creation and visible dialogue prompts first."] if not observation.get("ready") else
                ["Read the active objective and visible dialogue. Gather or produce its requested items using ordinary controls.",
                 "Select tools with slot_1 through slot_9; aim use_item at an observed world position.",
                 "Move in local waypoints, then interact when the target is highlighted. Use visible slots/buttons for transfers and crafting.",
                 "Inspect the next observation after input. Wait for production/growth or votes instead of repeating failed actions."])
        if player.get("context_policy") != "instructions":
            catalog = Repository(run["build"]).catalog(run["build"])
            ids = {q["id"] for q in active}
            result["definitions"] = [g for g in catalog.get("goals", []) if g.get("id") in ids]
        return result

    async def dispatch(self, run_id, player_id, name, args, observation, generation, owner="agent"):
        run, ps = self.binding(run_id, player_id)
        key = (run_id, player_id)
        if name in {"perform_action", "cancel_action", "wait_for_change"}:
            if run["state"] not in {"Starting", "Running"} or ps["generation"] != generation:
                return {"status": "cancelled", "reason": "Inputs held or generation changed"}
            if self.control_owners.get(key, "agent") != owner:
                return {"status": "rejected", "reason": "Player has a different control owner"}
        player = next(p for p in run["plan"]["players"] if p["id"] == player_id)
        if name == "observe":
            return await self.observe(run_id, player_id)
        if name == "explain_objective":
            return self.guidance(run, player, observation)
        if name == "list_entities":
            query = str(args.get("query", "")).casefold()
            entities = [e for e in observation.get("entities", []) if query in (e.get("label", "") + " " + e.get("kind", "")).casefold()]
            offset = max(0, int(args.get("offset", 0)))
            limit = max(1, min(int(args.get("limit", 40)), 80))
            return {"observation_id": observation["observation_id"], "entities": entities[offset:offset+limit], "total": len(entities), "next_offset": offset+limit if offset+limit < len(entities) else None}
        if name in {"inspect_entity", "list_actions", "get_action_status", "cancel_action", "read_changes"}:
            return await self.call_game(run_id, player_id, name, args)
        if name == "wait_for_change":
            self.waits[key] = (time.time() + max(1, min(float(args["seconds"]), 60)), wait_signature(observation), generation)
            ps["activity"] = str(args.get("reason", "Waiting for game state"))[:1000]
            if owner == "agent" and run.get("subgoal_plan", {}).get("player_id") == player_id:
                subgoals.record_action(run["subgoal_plan"])  # Repeated waits without progress must trigger review.
            self.store.put("runs", run)
            self.status(run_id, player_id, "waiting", ps["activity"], generation, wait_until=self.waits[key][0])
            return {"status": "waiting", "until": self.waits[key][0]}
        if name != "perform_action":
            return {"error": "Unsupported structured tool"}
        if key in self.pending:
            previous = await self.call_game(run_id, player_id, "get_action_status", {"action_id": self.pending[key]})
            if previous.get("status", "unknown") in {"unknown", "running", "accepted"}:
                return {"status": "rejected", "reason": "Reconcile pending action first", "previous": previous}
            self.pending.pop(key, None)
            run, ps = self.binding(run_id, player_id)
            if run["state"] not in {"Starting", "Running"} or ps["generation"] != generation or self.control_owners.get(key, "agent") != owner:
                return {"status": "cancelled", "reason": "Run control changed while reconciling the last action"}
        action_id = uid("game_")
        payload = {"action_id": action_id, "observation_id": observation["observation_id"],
                   "expires_at": time.time() + 30, "verb": args["verb"],
                   "target_id": args.get("target_id", ""), "target_version": args.get("target_version", -1),
                   "parameters": args.get("parameters", {})}
        event = self.store.event(run_id, "structured_action_intent", {"player_id": player_id, "action_id": action_id,
             "source_observation": observation["observation_id"], "reason": args.get("reason"), "input": payload})
        self.pending[key] = action_id
        ps["pending_action"] = {"action_id": action_id, "expires_at": payload["expires_at"]}
        if owner == "agent" and run.get("subgoal_plan", {}).get("player_id") == player_id:
            subgoals.record_action(run["subgoal_plan"])
        self.store.put("runs", run)
        self.status(run_id, player_id, "acting", str(args.get("reason") or f"Performing {args['verb']}"), generation, action_id=action_id)
        self.last_decisions.pop(key, None)
        try:
            result = await self.call_game(run_id, player_id, "perform_action", payload)
        except Exception as exc:
            # A worker HTTP 4xx is a definitive pre-execution rejection; only transport/5xx outcomes are unknown.
            rejected = 400 <= (getattr(exc, "status_code", None) or 0) < 500 and getattr(exc, "ambiguous", True) is False
            result = {"status": "rejected" if rejected else "unknown", "action_id": action_id,
                      "reason": str(exc) if rejected else "Acknowledgement lost; reconcile status",
                      "error": f"{type(exc).__name__}: {exc}", "error_code": getattr(exc, "code", None)}
        self.store.event(run_id, "action_result", {"player_id": player_id, "action_id": action_id,
            "intent_event_id": event["id"], "result": result, "mode": "structured", "retries": 0})
        if result.get("status", "unknown") not in {"running", "accepted", "unknown"}:
            self.clear_pending(run_id, player_id, generation)
        if result.get("status") in {"rejected", "failed", "expired", "cancelled"}:
            latest = self.store.require("runs", run_id)
            if latest["players"][player_id]["generation"] == generation:
                latest["players"][player_id]["invalid_actions"] = latest["players"][player_id].get("invalid_actions", 0) + 1
                self.store.put("runs", latest)
            self.request_replan(run_id, player_id, generation, "The last action failed; reassess prerequisites and approach.")
            self.status(run_id, player_id, "action_failed", str(result.get("reason") or result.get("status")), generation, action_id=action_id)
        return result

    def decision_context(self, run, player, observation):
        ps = run["players"][player["id"]]
        recent = self.store.events(run["id"], after=max(0, self.store.count_events(run["id"])-120), limit=120)
        own = [e for e in recent if e["payload"].get("player_id") == player["id"] and e["kind"] in
               {"decision_selected", "structured_action_intent", "action_result", "structured_action_outcome", "goal_changed"}][-20:]
        team = [e for e in recent if e["kind"] == "team_message"][-12:] if player.get("cooperation") == "team" else []
        context = {"play_mode": run["plan"]["play_mode"], "player": player, "goal": player["goal"],
            "game_controls": run.get("agent_profile", {}).get("instructions", ""), "agent_profile_id": run.get("agent_id"),
            "observation": prompt_observation(observation), "guidance": self.guidance(run, player, observation),
            "memory": ps.get("agent_memory", ""), "recent_own_actions": own, "team_messages": team,
            "setup": run["plan"]["world_setup"], "transport": run["plan"]["transport"],
            "world_namespace": run.get("world_namespace"), "seed": run["plan"].get("seed"), "recovery_instruction": run.get("recovery_instruction", "")}
        plan = run.get("subgoal_plan")
        if plan and plan["player_id"] == player["id"]:
            context.update(subgoal_plan=subgoals.prompt_plan(plan), current_goal=copy.deepcopy(subgoals.active_goal(plan)),
                           goal_path=[{"id": g["id"], "objective": g["objective"], "completion": g["completion"]} for g in subgoals.goal_path(plan)])
        return context

    async def advance_plan(self, run_id, player_id, observation, generation, current):
        """Verify on observations; infer only on expansion, changed knowledge, or failed progress."""
        run, _ = self.binding(run_id, player_id)
        plan = run.get("subgoal_plan")
        if not plan or plan["player_id"] != player_id:
            return True  # Historical multi-player/scenario runs retain their existing contract.
        previous_active = plan.get("active_goal_id")
        completed = subgoals.reconcile(plan, observation, generation)
        needs_plan = subgoals.needs_planning(plan)
        self.store.put("runs", run)
        if completed:
            self.store.event(run_id, "subgoals_verified", {"player_id": player_id, "goal_ids": completed,
                             "evidence": subgoals.observation_ref(observation), "full_observation": observation})
        if completed or previous_active != plan.get("active_goal_id"):
            self.waits.pop((run_id, player_id), None)
            self.last_decisions.pop((run_id, player_id), None)
        if plan["status"] == "blocked":
            self.status(run_id, player_id, "planner_blocked", plan["reason"], generation)
            await self.coordinator.investigate(run_id, "planner_blocked", plan["reason"], [player_id])
            return False
        if not needs_plan:
            return True
        player = next(p for p in run["plan"]["players"] if p["id"] == player_id)
        context = self.decision_context(run, player, observation)
        revision = plan["revision"]
        self.status(run_id, player_id, "planning", "Finding the next achievable prerequisite for: " + context["current_goal"]["objective"], generation)
        for attempt in range(2):
            self.store.event(run_id, "subgoal_planning_context", {"player_id": player_id, "revision": revision, "context": context})
            proposal = await self.coordinator.ai.plan_subgoals(context, run_id)
            if not current():
                self.store.event(run_id, "subgoal_planning_cancelled", {"player_id": player_id, "reason": "Run control changed during planning"})
                return False
            fresh = await self.observe(run_id, player_id)
            if not current():
                return False
            live, _ = self.binding(run_id, player_id)
            latest_plan = live.get("subgoal_plan")
            if (not latest_plan or latest_plan["revision"] != revision or
                    subgoals.planning_scene(fresh) != subgoals.planning_scene(observation)):
                self.store.event(run_id, "subgoal_planning_cancelled", {"player_id": player_id, "reason": "Plan, game binding, or visible controls changed during planning"})
                return False
            try:
                revised = subgoals.apply_decision(latest_plan, proposal, context)
                verified = subgoals.reconcile(revised, fresh, generation)
                break
            except ValueError as exc:
                self.store.event(run_id, "subgoal_plan_rejected", {"player_id": player_id, "reason": str(exc), "attempt": attempt + 1})
                if attempt:
                    raise ProviderError("invalid_plan", f"Planner returned an unsupported or ungrounded goal: {exc}") from exc
                # One corrective retry: show the planner its rejected answer and the validator's reason.
                context = {**context, "previous_attempt_rejected": {"reason": str(exc), "rejected_answer": proposal}}
        live["subgoal_plan"] = revised
        self.store.put("runs", live)
        self.store.event(run_id, "subgoal_plan_updated", {"player_id": player_id, "plan": revised})
        if verified:
            self.store.event(run_id, "subgoals_verified", {"player_id": player_id, "goal_ids": verified,
                             "evidence": subgoals.observation_ref(fresh), "full_observation": fresh})
        self.waits.pop((run_id, player_id), None)
        self.last_decisions.pop((run_id, player_id), None)
        self.status(run_id, player_id, "ready", "Plan updated; checking fresh state before the next action.", generation)
        return False  # Never act on the pre-planning snapshot after a potentially slow model call.

    def request_replan(self, run_id, player_id, generation, reason):
        run = self.store.require("runs", run_id)
        plan = run.get("subgoal_plan")
        if not plan or plan["player_id"] != player_id or run["players"][player_id]["generation"] != generation:
            return False
        goal = subgoals.active_goal(plan)
        if not goal:
            return False
        goal["actionable"] = False
        plan["reason"] = reason
        self.store.put("runs", run)
        self.waits.pop((run_id, player_id), None)
        self.last_decisions.pop((run_id, player_id), None)
        return True

    async def turn(self, run_id, player_id):
        key = (run_id, player_id)
        run, ps = self.binding(run_id, player_id)
        generation = ps["generation"]
        if self.generations.get(key) != generation:
            if ps.get("pending_action"):
                self.clear_pending(run_id, player_id, generation)  # Held/rebound input cannot still be pending.
            self.pending.pop(key, None)
            self.waits.pop(key, None)
            self.last_decisions.pop(key, None)
            self.generations[key] = generation
        def current():
            live = self.store.require("runs", run_id)
            return live["state"] in {"Starting", "Running"} and live["players"][player_id]["generation"] == generation and self.control_owners.get(key, "agent") == "agent"
        if not current():
            if self.control_owners.get(key, "agent") != "agent":
                self.status(run_id, player_id, "external_control", "An external MCP session controls this player; autonomous decisions are suspended.", generation)
            return
        try:
            previous_status = ps.get("agent_status") or {}
            # Polling a known wait must not replace its useful status with a flash of "observing".
            if key not in self.pending and key not in self.waits and key not in self.last_decisions:
                self.status(run_id, player_id, "observing", "Reading fresh game state.", generation)
            observation = await self.observe(run_id, player_id)
            if key in self.pending:
                result = await self.call_game(run_id, player_id, "get_action_status", {"action_id": self.pending[key]})
                if result.get("status") in {"running", "accepted"}:
                    deadline = result.get("expires_at") or (ps.get("pending_action") or {}).get("expires_at")
                    if deadline and time.time() > deadline + 5:
                        reason = "The game action is still running after its execution deadline. Inspect the action before retrying."
                        self.status(run_id, player_id, "error", reason, generation, last_error=reason, error_at=time.time(), action_id=self.pending[key])
                        await self.coordinator.investigate(run_id, "unknown_execution", reason, [player_id])
                        return
                    self.status(run_id, player_id, "waiting_action", "Waiting for the game to acknowledge the action outcome.", generation, action_id=self.pending[key], wait_until=deadline)
                    return
                self.store.event(run_id, "structured_action_outcome", {"player_id": player_id, "result": result})
                if result.get("status", "unknown") == "unknown":
                    self.status(run_id, player_id, "error", "The last action outcome is unknown; input is held for review.", generation, action_id=self.pending[key])
                    await self.coordinator.investigate(run_id, "unknown_execution", "Structured action acknowledgement could not be reconciled; fresh review required", [player_id])
                    return
                self.clear_pending(run_id, player_id, generation)
                self.last_decisions.pop(key, None)
                if result.get("status") in {"failed", "rejected", "expired", "cancelled"}:
                    self.request_replan(run_id, player_id, generation, "The last action failed; reassess prerequisites and approach.")
            if not current() or not await self.advance_plan(run_id, player_id, observation, generation, current):
                return
            signature = meaningful_signature(observation)
            wait = self.waits.get(key)
            if wait and wait[2] == generation and time.time() < wait[0] and wait_signature(observation) == wait[1]:
                self.status(run_id, player_id, "waiting", previous_status.get("detail") or ps.get("activity") or "Waiting for game state to change.", generation, wait_until=wait[0])
                return
            self.waits.pop(key, None)
            last = self.last_decisions.get(key)
            if not wait and last and last[2] == generation and last[0] == signature and time.time() - last[1] < 30:
                self.status(run_id, player_id, "waiting", "Game state is unchanged; the next decision will retry within 30 seconds.", generation, wait_until=last[1] + 30)
                return
            run, ps = self.binding(run_id, player_id)
            player = next(p for p in run["plan"]["players"] if p["id"] == player_id)
            context = self.decision_context(run, player, observation)
            self.store.event(run_id, "structured_decision_context", {"player_id": player_id, "context": context,
                "full_observation": observation, "source_revision": run["build"].get("source_revision")})
            self.last_decisions[key] = (signature, time.time(), generation)
            self.status(run_id, player_id, "thinking", "Waiting for Codex to choose the next action.", generation)
            decision_started_at = time.time()
            async def dispatch(name, args):
                nonlocal observation
                if not current():
                    return {"status": "cancelled"}
                self.status(run_id, player_id, "tool", f"Using {name}.", generation)
                result = await self.dispatch(run_id, player_id, name, args, observation, generation)
                if name == "observe" and result.get("observation_id"):
                    observation = result
                    result = prompt_observation(result)
                self.store.event(run_id, "structured_tool_result", {"player_id": player_id, "tool": name, "result": result})
                return result
            result = await self.coordinator.ai.player_turn(context, Repository(run["build"]), dispatch, current, run_id)
            self.store.event(run_id, "agent_turn_completed", {"player_id": player_id, "duration_seconds": round(time.time() - decision_started_at, 3), "result": result})
            if current():
                latest = self.store.require("runs", run_id)
                status = latest["players"][player_id].get("agent_status") or {}
                status["completed_at"] = time.time()
                latest["players"][player_id]["agent_status"] = status
                self.store.put("runs", latest)
                phase = status.get("phase")
                if phase in {"thinking", "tool"}:
                    self.status(run_id, player_id, "ready", "Decision finished; checking the next observation.", generation, completed_at=time.time())
        except asyncio.CancelledError:
            raise
        except ProviderError as exc:
            if current():
                if exc.status == "decision_needs_instructions" and self.request_replan(run_id, player_id, generation,
                        "No supported direct action fits. Decompose missing prerequisites or explain the blocker."):
                    self.status(run_id, player_id, "planning", "The current goal needs a smaller prerequisite or another approach.", generation)
                    return
                self.status(run_id, player_id, "error", exc.message, generation, last_error=exc.message, error_at=time.time())
                category = "decision_needs_instructions" if exc.status == "decision_needs_instructions" else "provider_failure"
                self.store.event(run_id, "agent_turn_failed", {"player_id": player_id, "category": category, "provider_status": exc.status, "error": exc.message})
                if exc.status == "spend_limit":
                    await self.coordinator.finish(run_id, "limit_reached", limitation=exc.message)
                else:
                    await self.coordinator.investigate(run_id, category, exc.message, [player_id])
        except Exception as exc:
            if current():
                reason = f"{type(exc).__name__}: {exc}"
                # Only a worker-confirmed unopened bridge gets the bounded startup retry.
                if getattr(exc, "code", None) == "game_bridge_starting" and time.time() - ps.get("launched_at", 0) < 15:
                    self.status(run_id, player_id, "startup_retry", f"Waiting for game interface startup: {reason}", generation, last_error=reason, error_at=time.time(), wait_until=ps["launched_at"] + 15)
                    return
                self.status(run_id, player_id, "error", reason, generation, last_error=reason, error_at=time.time())
                self.store.event(run_id, "agent_turn_failed", {"player_id": player_id, "category": "structured_interface_failure", "error": reason})
                await self.coordinator.investigate(run_id, "structured_interface_failure", reason, [player_id])
