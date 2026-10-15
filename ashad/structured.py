"""Shared structured-play handlers for Decisions, dashboard inspection and MCP."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import time
import traceback
from datetime import datetime, timezone

from .ai import (GAME_VERBS, SNAPSHOT_CHARS, ProviderError, batch_options, capability_summary, observation_delta, option_key, picked_options,
                 prompt_observation, resolve, static_prefix)
from .game_controls import run_configuration
from .models import MODEL, PROVIDERS, provider_of
from .repository import Repository
from .store import uid
from . import facts, macros, recipes, repair_tasks, subgoals

LOG = logging.getLogger(__name__)
LOOP_BACKSTOP_DECISIONS = 6
MOVEMENT_STALL_LIMIT = 2  # Failed moves from one unchanged position: input is not reaching the character.
BATCH_LIMIT = 5  # Actions per turn from one batch decide.
MACRO_LIMIT = 30  # Actions per turn from one code macro.
SNAPSHOT_RESET_CHARS = 12000  # A delta this large costs more than a fresh snapshot (one cache write).
TIMER_FACTS = ("crops_harvestable", "crop_growth_stage_min")  # Growth facts the code waits on after the model chose to wait.
CODE_WAIT_STEP = 10
CODE_WAIT_LIMIT = 120  # Seconds of code waits per goal before the model decides again.
ISSUE_PREFIXES = {"unsupported_action": "uae_", "crash": "crash_", "game_bug": "bug_", "false_completion": "done_", "capability_gap": "gap_"}
# Controller replan reasons, by prefix, as category labels; planner prose never reaches the timing row.
PLAN_TRIGGERS = (("The last action failed", "action_failed"),
                 ("Observed objectives changed", "knowledge_changed"), ("Control or game binding changed", "rebound"),
                 ("No supported direct action fits", "needs_instructions"), ("The game state did not change across", "loop_backstop"),
                 ("You said the primary goal is not done", "completion_rejected"),
                 ("Repeating the same action", "action_repeat"), ("Recipe replay stopped", "recipe_failed"))


def meaningful_signature(observation):
    """Ignore observation clocks and cosmetic-only revisions, preserve actionable facts."""
    return hashlib.sha256(json.dumps({k: observation.get(k) for k in
        ("generation", "world_id", "ready", "state", "entities", "active_action")},
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@contextlib.contextmanager
def timed(timing, step):
    """Add the block's wall time and one call to a turn-timing step."""
    started = time.perf_counter()
    try:
        yield
    finally:
        timing["seconds"][step] = timing["seconds"].get(step, 0.0) + time.perf_counter() - started
        timing["calls"][step] = timing["calls"].get(step, 0) + 1


def json_bytes(value):
    return len(json.dumps(value, ensure_ascii=False, default=str).encode())


def wait_signature(observation):
    state = observation.get("state", {})
    player = state.get("player", {})
    # Timers, animation, camera motion and moving bystanders must not cause inference.
    facts = {"generation": observation.get("generation"), "ready": observation.get("ready"),
             "inventory": player.get("inventory"), "hotbar": player.get("hotbar"),
             "objectives": state.get("objectives"), "completed": state.get("completed_quests"),
             "tiles": state.get("tiles"), "facts": state.get("facts"),
             "targets": [{"id": e.get("id"), "actions": e.get("actions"),
                          "crafting": e.get("state", {}).get("is_crafting")}
                         for e in observation.get("entities", []) if e.get("kind") != "ground_item"]}
    return hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()


def effect_basis(observation):
    return {"facts": wait_signature(observation), "position": observation.get("state", {}).get("player", {}).get("position")}


def action_effect(before, observation):
    """Did the observed state change after a completed action? The game's own `completed` only means input was delivered."""
    if not before:
        return None
    return "no_effect" if before == effect_basis(observation) else "changed"


def settle_effect(result, before, observation):
    """Record the effect on a completed result; an action that changed nothing is reported as no_effect, not completed."""
    result["effect"] = action_effect(before, observation)
    if result["effect"] == "no_effect":
        result["status"] = "no_effect"


def queued_actions(context, keys):
    """A model batch's next_action: pop the next queued option key and re-find it on the fresh observation."""
    queue = list(keys)
    def next_action(fresh):
        if not queue:
            return None, "done"
        action = resolve({**context, "observation": fresh}, queue.pop(0))
        return (action, "queued") if action else (None, "not_offered")
    return next_action


def own_action_line(event):
    """One short line per own event for the decision context, keeping only keys with values."""
    payload = event["payload"]
    given, result = payload.get("input") or {}, payload.get("result") or {}
    line = {"kind": event["kind"],
            "verb": (payload.get("action") or {}).get("tool") if event["kind"] == "decision_selected" else given.get("verb"),
            "target_id": given.get("target_id") or None, "status": result.get("status"), "effect": result.get("effect"),
            "reason": str(payload.get("reason") or result.get("reason") or "")[:160]}
    return {k: v for k, v in line.items() if v}


OWN_ACTION_KINDS = frozenset({"decision_selected", "structured_action_intent", "action_result", "structured_action_outcome", "goal_changed"})


def observed_entity(observation, target_id):
    return next((e for e in observation.get("entities", []) if target_id and e.get("id") == target_id), {})


GOAL_CONTEXT_KEYS = ("id", "parent_id", "objective", "completion", "status", "actionable", "measured", "attempts", "steps")
FAILED_STATUSES = frozenset({"failed", "rejected", "expired", "cancelled"})
DELIVERED_STATUSES = frozenset({"completed", "no_effect"})


def repeated_failures(events):
    """Actions that failed more than once, so the planner sees them instead of re-planning them."""
    intents = {e["payload"].get("action_id"): e["payload"].get("input") or {}
               for e in events if e["kind"] == "structured_action_intent"}
    failures = {}
    for event in events:
        result = event["payload"].get("result") or {}
        action_id = event["payload"].get("action_id") or result.get("action_id")
        action = intents.get(action_id)
        if event["kind"] in {"action_result", "structured_action_outcome"} and action and result.get("status") in FAILED_STATUSES:
            failures[action_id] = (action, str(result.get("reason") or result["status"])[:200])
    grouped = {}
    for action, reason in failures.values():
        fingerprint = json.dumps([action.get("verb"), action.get("target_id", ""), action.get("parameters") or {}], sort_keys=True)
        entry = grouped.setdefault(fingerprint, {"verb": action.get("verb"), "target_id": action.get("target_id", ""),
                                                 "parameters": action.get("parameters") or {}, "reason": reason, "failures": 0})
        entry.update(reason=reason, failures=entry["failures"] + 1)
    return [entry for entry in grouped.values() if entry["failures"] >= 2]


class StructuredPlayers:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.store = coordinator.store
        self.waits = {}
        self.last_decisions = {}
        self.pending = {}
        self.generations = {}
        self.control_owners = {}
        self.loop_counts = {}
        self.movement_stalls = {}
        self.bug_reports = {}
        self.turn_ended_at = {}
        self.snapshots = {}  # (run_id, player_id) -> (goal_id, generation, prompt observation at subgoal start)
        self.code_waits = {}  # (run_id, player_id) -> (goal_id, seconds of code waits used)
        self.step_index = {}  # (run_id, player_id) -> (goal_id, index of the goal's next unfinished step, decider takes the next turn)
        self.goal_intents = {}  # (run_id, player_id) -> (goal_id, [macros.step_for of each agent action]), saved as a recipe when the goal completes
        # (run_id, player_id) -> (next leaf's goal_id, plan revision once the current leaf completes, planning_scene at start, planner task).
        # At most one per player, started while a code macro runs; advance_plan uses or cancels it.
        self.lookahead = {}
        self.crashes_recorded = set()

    def _forget_turn(self, key):
        self.waits.pop(key, None)
        self.last_decisions.pop(key, None)
        # The snapshot, code-wait budget, step index and intents belong to one goal; a replan of the same goal keeps them.
        plan = self.store.require("runs", key[0]).get("subgoal_plan") or {}
        for kept in (self.snapshots, self.code_waits, self.step_index, self.goal_intents):
            if key in kept and kept[key][0] != plan.get("active_goal_id"):
                del kept[key]

    def claim_control(self, key, owner):
        if key in self.control_owners:
            raise ValueError("Player already has an external control lease")
        self.control_owners[key] = owner

    def release_control(self, key, owner):
        if self.control_owners.get(key) == owner:
            del self.control_owners[key]

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

    async def call_game(self, run_id, player_id, operation, arguments=None, *, settle_seconds=0):
        run, ps = self.binding(run_id, player_id)
        return await self.coordinator.request(ps["worker_id"], "POST", "/game", {
            "run_id": run_id, "player_id": player_id, "epoch": ps["epoch"],
            "operation": operation, "arguments": arguments or {}, "settle_seconds": settle_seconds})

    async def observe(self, run_id, player_id):
        return self._accept_observation(run_id, player_id, await self.call_game(run_id, player_id, "observe"))

    def _accept_observation(self, run_id, player_id, result):
        """Check a worker observation's identity and freshness, then store it as the player's latest."""
        if result.get("schema_version") != 1 or result.get("run_id") != run_id or result.get("player_id") != player_id:
            raise ValueError("Structured game observation identity/schema mismatch")
        if not result.get("observation_id"):
            raise ValueError("Game observation is missing its observation ID")
        if subgoals.stale(result.get("timestamp")):
            raise ValueError("Structured game context is stale")
        # Game and controller clocks may disagree; planning freshness uses the controller's receipt time.
        result["received_at"] = time.time()
        run, ps = self.binding(run_id, player_id)
        self.store.put("observations", result, f"{run_id}:{player_id}")
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
                 "Select a hotbar item with the input named on its hotbar entry (slot_1 is hotbar slot 0); aim use_item at an observed world position.",
                 "Move in local waypoints, then interact when the target is highlighted. Use visible slots/buttons for transfers and crafting.",
                 "Inspect the next observation after input. Wait for production/growth or votes instead of repeating failed actions."])
        if player.get("context_policy") != "instructions":
            catalog = Repository(run["build"]).catalog(run["build"])
            ids = {q["id"] for q in active}
            result["definitions"] = [g for g in catalog.get("goals", []) if g.get("id") in ids]
        return result

    def item_ids(self, run):
        return [i["id"] for i in Repository(run["build"]).catalog(run["build"]).get("items", []) if isinstance(i, dict) and isinstance(i.get("id"), str)]

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
        return await self._perform_action(run_id, player_id, args, observation, generation, owner, settle=owner == "agent")

    async def _perform_action(self, run_id, player_id, args, observation, generation, owner, *, settle=False):
        run, ps = self.binding(run_id, player_id)
        key = (run_id, player_id)
        if key in self.pending:
            previous = await self.call_game(run_id, player_id, "get_action_status", {"action_id": self.pending[key]})
            if previous.get("status", "unknown") in {"unknown", "running", "accepted"}:
                return {"status": "rejected", "reason": "Reconcile pending action first", "previous": previous}
            if previous.get("status") == "completed":
                settle_effect(previous, (ps.get("pending_action") or {}).get("before"), observation)
            self.store.event(run_id, "structured_action_outcome", {"player_id": player_id, "result": previous})
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
             "source_observation": observation["observation_id"], "reason": args.get("reason"), "input": payload,
             "target_kind": observed_entity(observation, payload["target_id"]).get("kind")})
        self.pending[key] = action_id
        ps["pending_action"] = {"action_id": action_id, "expires_at": payload["expires_at"], "verb": payload["verb"],
                              "before": effect_basis(observation)}
        if owner == "agent" and run.get("subgoal_plan", {}).get("player_id") == player_id:
            subgoals.record_action(run["subgoal_plan"], payload)
            goal_id = run["subgoal_plan"].get("active_goal_id")
            if self.goal_intents.get(key, (None,))[0] != goal_id:
                self.goal_intents[key] = (goal_id, [])
            goal_fact = (subgoals.active_goal(run["subgoal_plan"]) or {}).get("completion", {}).get("fact", "")
            self.goal_intents[key][1].append(macros.step_for(payload, observation, goal_fact))
        self.store.put("runs", run)
        self.status(run_id, player_id, "acting", str(args.get("reason") or f"Performing {args['verb']}"), generation, action_id=action_id)
        self.last_decisions.pop(key, None)
        try:
            result = await self.call_game(run_id, player_id, "perform_action", payload, settle_seconds=1 if settle else 0)
        except Exception as exc:
            # A worker HTTP 4xx is a definitive pre-execution rejection; only transport/5xx outcomes are unknown.
            rejected = 400 <= (getattr(exc, "status_code", None) or 0) < 500 and getattr(exc, "ambiguous", True) is False
            result = {"status": "rejected" if rejected else "unknown", "action_id": action_id,
                      "reason": str(exc) if rejected else "Acknowledgement lost; reconcile status",
                      "error": f"{type(exc).__name__}: {exc}", "error_code": getattr(exc, "code", None),
                      "http_status": getattr(exc, "status_code", None)}
        # A settling worker returns the final status plus the post-action observation.
        fresh = result.pop("observation", None)
        if fresh is not None:
            fresh = self._accept_observation(run_id, player_id, fresh)
        settled = fresh is not None and result.get("status", "unknown") not in {"running", "accepted", "unknown"}
        # The ledger keeps the acknowledgement as action_result and the final status as structured_action_outcome.
        ack = {**result, "status": result["acknowledged_status"]} if settled else result
        outcome_event = self.store.event(run_id, "action_result", {"player_id": player_id, "action_id": action_id,
            "intent_event_id": event["id"], "result": ack, "mode": "structured", "retries": 0})
        if ack.get("status") == "rejected" and ack.get("http_status") not in {None, 409}:
            await self.report_unsupported(run_id, player_id, "worker_rejected", {"tool": "perform_action",
                "verb": payload["verb"], "target_id": payload["target_id"] or None, "parameters": payload["parameters"],
                "controller": "agent" if owner == "agent" else "mcp"}, observation,
                outcome=result, event_ids=(outcome_event["id"],))
        if settled:
            if result.get("status") == "completed":
                settle_effect(result, ps["pending_action"]["before"], fresh)
            self.store.event(run_id, "structured_action_outcome", {"player_id": player_id, "result": result})
            self.clear_pending(run_id, player_id, generation)
            await self._after_outcome(run_id, player_id, generation, payload["verb"], fresh, result)
            result["observation"] = fresh
            return result
        if fresh is not None:
            result["observation"] = fresh
        if result.get("status", "unknown") not in {"running", "accepted", "unknown"}:
            self.clear_pending(run_id, player_id, generation)
        if result.get("status") in FAILED_STATUSES:
            latest = self.store.require("runs", run_id)
            if latest["players"][player_id]["generation"] == generation:
                latest["players"][player_id]["invalid_actions"] = latest["players"][player_id].get("invalid_actions", 0) + 1
                self.store.put("runs", latest)
            if await self._pause_if_stalled(run_id, player_id, generation, payload["verb"], observation, result):
                return result
            self.request_replan(run_id, player_id, generation, "The last action failed; reassess prerequisites and approach.")
            self.status(run_id, player_id, "action_failed", str(result.get("reason") or result.get("status")), generation, action_id=action_id)
        return result

    async def report_unsupported(self, run_id, player_id, source, wanted, observation, *, outcome=None, event_ids=()):
        """Record one unsupported-action hit; never blocks or fails the turn."""
        try:
            run, ps = self.binding(run_id, player_id)
            target = observed_entity(observation, wanted.get("target_id"))
            wanted = {"tool": None, "verb": None, "target_id": None, "parameters": None, "blocker": None,
                      "controller": "agent", "follow_up_error": None, **wanted,
                      "target_kind": target.get("kind"), "target_label": target.get("label")}
            basis = {"tool": wanted["tool"], "verb": wanted["verb"], "blocker": wanted["blocker"],
                     "target_kind": target.get("kind") or "none",
                     "target_label": repair_tasks.target_label(target.get("label")),
                     "error_code": (outcome or {}).get("error_code") or (outcome or {}).get("http_status")}
            capabilities = {**capability_summary(observation), "target_advertised_actions": list(target.get("actions", []))}
            outcome_fields = None if outcome is None else {
                k: (str(outcome.get(k))[:2000] if k in {"reason", "error"} and outcome.get(k) is not None else outcome.get(k))
                for k in ("status", "reason", "error", "error_code", "http_status")}
            await self._record(run, ps, player_id, "unsupported_action", basis, source, observation,
                {"wanted": wanted, "outcome": outcome_fields, "capabilities": capabilities}, event_ids=event_ids)
        except Exception:
            LOG.exception("Could not record unsupported action for %s/%s", run_id, player_id)

    def _recent_events(self, run_id):
        """The last 120 ledger events: the window every issue example reads its context from."""
        return self.store.events(run_id, after=max(0, self.store.count_events(run_id) - 120), limit=120)

    def _recent_decisions(self, run_id, player_id):
        recent = self._recent_events(run_id)
        player_events = [e for e in recent if e["payload"].get("player_id") == player_id
                         and e["kind"] in {"decision_selected", "action_result", "structured_tool_result"}]
        decisions = []
        for event in player_events[-5:]:
            text = json.dumps(event["payload"], ensure_ascii=False)
            decisions.append({"event_id": event["id"], "kind": event["kind"], "at": event["timestamp"],
                              "payload": event["payload"] if len(text) <= 2000 else text[:2000]})
        return decisions

    def _intent(self, run, player_id):
        player = next(p for p in run["plan"]["players"] if p["id"] == player_id)
        plan = run.get("subgoal_plan") if (run.get("subgoal_plan") or {}).get("player_id") == player_id else None
        goal = subgoals.active_goal(plan) if plan else None
        return {"primary_goal": plan["primary_goal"] if plan else player["goal"],
                "current_goal": {k: goal[k] for k in ("id", "objective", "completion")} if goal else None,
                "goal_path": [g["objective"] for g in subgoals.goal_path(plan)] if plan else [],
                "plan_reason": plan.get("reason") if plan else None}

    async def _pin(self, ps, run_id, player_id, example_id, observation, *, skip):
        screenshot = {"observation_id": observation.get("observation_id"), "pin_id": example_id,
                      "status": "unavailable", "frames": [],
                      "retained_path_prefix": f"runs/{run_id}/workers/{player_id}/pinned/{example_id}/"}
        if not skip:
            try:
                pinned = await self.coordinator.request(ps["worker_id"], "POST", "/pin", {
                    "epoch": ps["epoch"], "incident_id": example_id, "before_seconds": 10, "after_seconds": 0})
                screenshot.update(status="pinned", frames=list((pinned or {}).get("frames", [])))
            except Exception:
                pass  # Status stays "unavailable"; a missing pin never blocks the record.
        return screenshot

    def _example(self, run, ps, player_id, example_id, source, observation, screenshot, event_ids):
        root = self.store.run_dir(run["id"])
        telemetry = ps.get("telemetry") or {}
        return {"example_id": example_id, "at": datetime.now(timezone.utc).isoformat(), "run_id": run["id"],
            "project_id": run.get("project_id"), "player_id": player_id, "scenario_id": run.get("scenario_id"),
            "scenario_revision": run.get("scenario_revision"), "model": run.get("model"),
            "controller_version": run.get("controller_version"),
            "build": {"id": run["build"].get("id"), "source_revision": run["build"].get("source_revision")},
            "source": source, "intent": self._intent(run, player_id),
            "recent_decisions": self._recent_decisions(run["id"], player_id),
            "game_state": {"observation_id": observation.get("observation_id"),
                "observation": prompt_observation(observation, max_chars=20000),
                "telemetry": {"completed_quests": telemetry.get("completed_quests"), "diagnostics": telemetry.get("diagnostics")},
                "progress": run.get("progress")},
            "screenshot": screenshot,
            "evidence": {"run_dir": str(root), "ledger": str(root / "ledger.jsonl"), "report": str(root / "report.md"),
                         "event_ids": list(event_ids)},
            "truncated": []}

    async def _record(self, run, ps, player_id, issue_type, basis, source, observation, sections, *, event_ids=(), skip_pin=False, event_fields=None):
        """Write one issue example of any type; returns the outcome, or None when recording failed."""
        try:
            run_id = run["id"]
            project_id = run.get("project_id")
            key = repair_tasks.issue_key(self.store, project_id, issue_type, basis)
            previous = self.store.get("issues", key)
            room = repair_tasks.issue_room(self.store, project_id, issue_type, basis)
            if room == "full":
                self.store.event(run_id, "issue_cap_reached", {"player_id": player_id, "issue_type": issue_type,
                    "signature": repair_tasks.issue_signature(issue_type, basis)})
                return "full"
            example_id = uid(ISSUE_PREFIXES[issue_type])
            screenshot = await self._pin(ps, run_id, player_id, example_id, observation, skip=skip_pin or room != "keep")
            example = {**self._example(run, ps, player_id, example_id, source, observation, screenshot, event_ids), **sections}
            outcome = repair_tasks.record_issue(self.store, issue_type, basis, example)
            if outcome in {"kept", "counted"}:
                self.store.event(run_id, f"{issue_type}_recorded", {"player_id": player_id,
                    "issue_id": key, "example_id": example_id,
                    "source": source, "kept": outcome == "kept", "outcome": outcome, **(event_fields or {})})
                if previous and previous["status"] == "closed":
                    self.store.event(run_id, "issue_reopened", {"player_id": player_id, "issue_id": key, "example_id": example_id})
            elif outcome == "full":
                self.store.event(run_id, "issue_cap_reached", {"player_id": player_id, "issue_type": issue_type,
                    "signature": repair_tasks.issue_signature(issue_type, basis)})
            repair_tasks.publish_issues(self.store)
            return outcome
        except Exception:
            LOG.exception("Could not record %s issue for %s/%s", issue_type, run["id"], player_id)
            return None

    async def report_crash(self, run_id, player_id, kind, detector, reason):
        """Record a game or environment death as a crash issue; never raises."""
        try:
            run = self.store.require("runs", run_id)
            ps = run["players"][player_id]
            dedupe = (run_id, player_id, ps["generation"], kind)
            if dedupe in self.crashes_recorded:
                return
            self.crashes_recorded.add(dedupe)
            status = ps.get("worker_status") or {}
            game_exit = status.get("game_exit") or {}
            exit_code, signal, log_tail = (game_exit.get(k) for k in ("exit_code", "signal", "log_tail"))
            exit = f"signal:{signal}" if signal else f"code:{exit_code}" if exit_code is not None else None
            basis = {"kind": kind, "detector": detector, "exit": exit}
            recent = self._recent_events(run_id)
            intent = next((e for e in reversed(recent) if e["kind"] == "structured_action_intent"
                           and e["payload"].get("player_id") == player_id), None)
            last_action = None
            if intent:
                payload = intent["payload"]
                result = next((e["payload"].get("result") or {} for e in reversed(recent) if e["kind"] == "action_result"
                               and e["payload"].get("action_id") == payload.get("action_id")), {})
                last_action = {"event_id": intent["id"], "at": intent["timestamp"], "verb": payload["input"]["verb"],
                    "target_id": payload["input"]["target_id"], "status": result.get("status")}
            sections = {
                "crash": {"kind": kind, "detector": detector, "reason": str(reason)[:2000], "exit_code": exit_code,
                    "signal": signal, "log_tail": log_tail,
                    "worker_status": {k: status.get(k) for k in ("lifecycle", "game_running", "infrastructure_error",
                        "capture", "controller_age_seconds", "resources")}},
                "last_action": last_action,
                "timing": {"detected_at": time.time(), **{k: ps.get(k) for k in ("launched_at", "telemetry_received_at",
                    "feed_captured_at", "simulation_advanced_at")}}}
            await self._record(run, ps, player_id, "crash", basis, "monitor", self.store.get("observations", f"{run_id}:{player_id}") or {},
                sections, skip_pin=kind == "worker_lost")
        except Exception:
            LOG.exception("Could not report %s crash for %s/%s", kind, run_id, player_id)

    async def report_bug(self, run_id, player_id, report, observation):
        """Record a suspected game bug the agent reported; never raises."""
        try:
            run, ps = self.binding(run_id, player_id)

            def reject(reason):
                self.store.event(run_id, "game_bug_report_rejected", {"player_id": player_id, "reason": reason})

            if report.get("follow_up_error"):
                reject("follow_up_failed")
                return
            if report.get("symptom") == "not_a_bug":
                reject("not_a_bug")
                return
            action_id = report.get("related_action_id")
            recent = self._recent_events(run_id)
            intent = next((e for e in recent if e["kind"] == "structured_action_intent" and e["payload"].get("action_id") == action_id), None)
            result_event = next((e for e in reversed(recent) if e["kind"] == "action_result" and e["payload"].get("action_id") == action_id), None)
            completed = (result_event and (result_event["payload"].get("result") or {}).get("status") in DELIVERED_STATUSES) or any(
                e["kind"] == "structured_action_outcome" and (e["payload"].get("result") or {}).get("action_id") == action_id
                and e["payload"]["result"].get("status") in DELIVERED_STATUSES for e in recent)
            related = None
            if action_id and intent:
                given = intent["payload"]["input"]
                related = {"action_id": action_id, "verb": given.get("verb"), "target_id": given.get("target_id"),
                    "parameters": given.get("parameters"),
                    "status": "completed" if completed else result_event and (result_event["payload"].get("result") or {}).get("status"),
                    "intent_event_id": intent["id"], "result_event_id": result_event["id"] if result_event else None}
            if report["symptom"] == "wrong_result" and not (related and related["status"] == "completed"):
                reject("no_completed_action")
                return
            target = observed_entity(observation, report.get("target_id"))
            basis = {"symptom": report["symptom"], "expected": report["expected"], "observed": report["observed"],
                "action_verb": related["verb"] if related else None, "target_kind": target.get("kind") or "none",
                "target_label": repair_tasks.target_label(target.get("label"))}
            sections = {"report": {"symptom": report["symptom"], "expected": report["expected"], "observed": report["observed"],
                "target_id": report.get("target_id"), "target_kind": target.get("kind"), "target_label": target.get("label"),
                "target_state": target.get("state"), "related_action": related}}
            await self._record(run, ps, player_id, "game_bug", basis, "agent_signal", observation, sections)
        except Exception:
            LOG.exception("Could not report game bug for %s/%s", run_id, player_id)

    async def report_capability_gap(self, run_id, player_id, plan, focus, *, kind, tried, missing, observation):
        """Record one given-up goal; never raises or blocks the turn."""
        try:
            run, ps = self.binding(run_id, player_id)
            effects = []
            for event in self._recent_events(run_id):
                if (event["kind"] not in {"action_result", "structured_action_outcome"} or event["payload"].get("player_id") != player_id
                        or event["timestamp"] < focus.get("activated_at", 0)):
                    continue
                effect = (event["payload"].get("result") or {}).get("effect")
                if effect:
                    effects.append(effect)
            check = focus["completion"]
            known = check.get("fact", "unknown") != "unknown"
            value = facts.read(facts.values(observation), check["fact"], facts.args_of(check)) if known else None
            cause = ("planner_gap" if not known else "mcp_gap" if value is None
                     else "game_bug" if effects and all(e == "no_effect" for e in effects) else "planner_gap")
            basis = {"kind": kind, "cause": cause, "focus_objective": repair_tasks.target_label(focus["objective"]),
                     "missing": re.sub(r"(?<=\.)\d+(?=[.)])", "#", missing)[:200]}
            bounded = []
            for item in tried:
                text = json.dumps(item, ensure_ascii=False)
                bounded.append(item if len(text) <= 2000 else text[:2000])
            shown = missing
            if known:
                shown += f" (measured {focus.get('measured')}, target {check['op']} {check['value']})"
            sections = {"gap": {"kind": kind, "cause": cause, "missing": shown, "tried": bounded,
                "subgoal": {"id": focus["id"], "objective": focus["objective"], "completion": focus["completion"],
                            "goal_path": [g["objective"] for g in subgoals.goal_path(plan, focus["id"])]}},
                "capabilities": capability_summary(observation)}
            await self._record(run, ps, player_id, "capability_gap", basis, "agent", observation, sections, skip_pin=kind != "stuck",
                               event_fields={"goal_id": focus["id"]})
        except Exception:
            LOG.exception("Could not record capability gap for %s/%s", run_id, player_id)

    async def report_false_completion(self, run_id, player_id, plan, note):
        """Record a completion claim the operator rejected; never raises."""
        try:
            run, ps = self.binding(run_id, player_id)
            claim = subgoals.completion_claim(plan)
            goals = {g["id"]: g for g in plan["goals"]}
            basis = {"primary_goal": repair_tasks.target_label(plan["primary_goal"])}
            sections = {"claim": {"reason": claim["reason"], "operator_note": note,
                "verified_goals": [{k: goals[i][k] for k in ("id", "objective", "completion")} for i in claim["verified_goal_ids"]],
                "plan_revision": plan["revision"]}}
            await self._record(run, ps, player_id, "false_completion", basis, "operator", self.store.get("observations", f"{run_id}:{player_id}") or {}, sections, skip_pin=True)
        except Exception:
            LOG.exception("Could not record false completion for %s/%s", run_id, player_id)

    def forget_run(self, run_id):
        for counters in (self.loop_counts, self.movement_stalls, self.bug_reports, self.turn_ended_at, self.snapshots, self.code_waits, self.step_index,
                         self.goal_intents):
            for key in [k for k in counters if k[0] == run_id]:
                del counters[key]
        for key in [k for k in self.lookahead if k[0] == run_id]:
            self.lookahead.pop(key)[3].cancel()
        self.crashes_recorded -= {e for e in self.crashes_recorded if e[0] == run_id}

    async def _loop_backstop(self, run_id, player_id, generation, signature, observation, result):
        """Unchanged state across several dispatched decisions means the agent is stuck without saying so."""
        if not isinstance(result, dict) or result.get("action", {}).get("tool") not in {"perform_action", "wait_for_change"}:
            return
        key = (run_id, player_id)
        previous = self.loop_counts.get(key)
        count = previous[2] + 1 if previous and previous[:2] == (generation, signature) else 1
        if count < LOOP_BACKSTOP_DECISIONS:
            self.loop_counts[key] = (generation, signature, count)
            return
        self.loop_counts.pop(key, None)
        action = result["action"]
        args = action.get("args", {})
        await self.report_unsupported(run_id, player_id, "loop_backstop", {"tool": action["tool"], "verb": args.get("verb"),
            "target_id": args.get("target_id"),
            "parameters": args.get("parameters") or {k: v for k, v in args.items() if k != "reason"}}, observation)
        reason = f"The game state did not change across {LOOP_BACKSTOP_DECISIONS} decisions; the agent may be repeating an unsupported action."
        if self.request_replan(run_id, player_id, generation, reason):
            self.status(run_id, player_id, "planning", "Repeated actions did not change the game; revising the approach.", generation)
            return
        self.status(run_id, player_id, "error", reason, generation, last_error=reason, error_at=time.time())
        await self.coordinator.investigate(run_id, "decision_needs_instructions", reason, [player_id])

    def _movement_stalled(self, key, generation, observation):
        """True once enough failed moves leave the player at one position; any other position restarts the count."""
        position = observation.get("state", {}).get("player", {}).get("position")
        if not isinstance(position, list):
            self.movement_stalls.pop(key, None)
            return False
        previous = self.movement_stalls.get(key)
        count = previous[2] + 1 if previous and previous[:2] == (generation, position) else 1
        if count < MOVEMENT_STALL_LIMIT:
            self.movement_stalls[key] = (generation, position, count)
            return False
        self.movement_stalls.pop(key, None)
        return True

    async def _pause_if_stalled(self, run_id, player_id, generation, verb, observation, result):
        """Pause for review when a failed move leaves the player stalled; True when paused."""
        if verb != "move_to" or not self._movement_stalled((run_id, player_id), generation, observation):
            return False
        position = observation["state"]["player"]["position"]
        reason = (f"The character has not moved from {position} after "
                  f"{MOVEMENT_STALL_LIMIT} failed moves; game input is not reaching it. "
                  f"Last result: {result.get('reason') or result.get('status')}")
        self.status(run_id, player_id, "error", reason, generation, last_error=reason, error_at=time.time())
        await self.coordinator.investigate(run_id, "movement_unavailable", reason, [player_id])
        return True

    def decision_context(self, run, player, observation):
        ps = run["players"][player["id"]]
        recent = self.store.events(run["id"], after=max(0, self.store.count_events(run["id"])-120), limit=120)
        own = [e for e in recent if e["payload"].get("player_id") == player["id"] and e["kind"] in
               OWN_ACTION_KINDS][-20:]
        team = [e for e in recent if e["kind"] == "team_message"][-12:] if player.get("cooperation") == "team" else []
        configuration_id, snapshot = run_configuration(run)
        failures = repeated_failures(own)
        controls = (self.store.get("run_game_controls", run["id"]) or run.get("game_controls") or {}).get("controls")
        context = {"play_mode": run["plan"]["play_mode"], "player": player, "goal": player["goal"],
            "game_controls": controls or (snapshot or {}).get("instructions", ""), "configuration_id": configuration_id,
            "observation": prompt_observation(observation, max_chars=48000), "guidance": self.guidance(run, player, observation),
            "memory": ps.get("agent_memory", ""), "recent_own_actions": [own_action_line(e) for e in own[-5:]], "team_messages": team,
            "setup": run["plan"]["world_setup"], "transport": run["plan"]["transport"],
            "world_namespace": run.get("world_namespace"), "seed": run["plan"].get("seed"),
            "item_ids": self.item_ids(run), "known_locations": observation.get("known_locations")}
        if failures:
            context["repeated_failures"] = failures
        if run.get("recovery_instruction"):
            context["operator_instruction"] = {"text": run["recovery_instruction"], "after": run.get("recovery_after") or "resume"}
        plan = run.get("subgoal_plan")
        if plan and plan["player_id"] == player["id"]:
            context.update(subgoal_plan=subgoals.prompt_plan(plan), current_goal={k: v for k, v in subgoals.active_goal(plan).items() if k in GOAL_CONTEXT_KEYS},
                           goal_path=[{"id": g["id"], "objective": g["objective"], "completion": g["completion"], "measured": g.get("measured")}
                                      for g in subgoals.goal_path(plan)])
        return context

    async def _give_up(self, run_id, player_id, generation, observation, *, kind, tried, missing):
        run, _ = self.binding(run_id, player_id)
        plan = run["subgoal_plan"]
        focus = subgoals.active_goal(plan)
        await self.report_capability_gap(run_id, player_id, plan, focus, kind=kind, tried=tried, missing=missing, observation=observation)
        run, _ = self.binding(run_id, player_id)
        plan = run["subgoal_plan"]
        subgoals.give_up(plan, focus["id"], missing)
        self.store.put("runs", run)
        self.store.event(run_id, "subgoal_plan_updated", {"player_id": player_id, "plan": plan, "warnings": [], "outcome": "gave_up"})
        self._forget_turn((run_id, player_id))
        await self._after_give_up(run_id, player_id, generation, plan, focus)

    async def _after_give_up(self, run_id, player_id, generation, plan, focus):
        if plan["status"] == "handed_back":
            self.status(run_id, player_id, "handed_back", plan["reason"], generation)
            await self.coordinator.investigate(run_id, "handed_back", plan["reason"], [player_id])
        else:
            self.status(run_id, player_id, "ready", f"Skipped “{focus['objective']}”: logged as MCP or game needs updating.", generation)

    async def advance_plan(self, run_id, player_id, observation, generation, current, timing):
        """Verify on observations; infer only on expansion, changed knowledge, or failed progress.

        Returns None when the turn ends, else the observation to act on and the planner's first batch as option keys."""
        run, _ = self.binding(run_id, player_id)
        plan = run.get("subgoal_plan")
        if not plan or plan["player_id"] != player_id:
            return {"observation": observation, "first_actions": []}  # Historical multi-player/scenario runs retain their existing contract.
        previous_active = plan.get("active_goal_id")
        with timed(timing, "plan_reconcile"):
            completed = subgoals.reconcile(plan, observation, generation)
            needs_plan = subgoals.needs_planning(plan)
        self.store.put("runs", run)
        if completed:
            self.store.event(run_id, "subgoals_verified", {"player_id": player_id, "goal_ids": completed,
                             "evidence": subgoals.observation_ref(observation), "full_observation": observation})
            self._save_recipes(run_id, player_id, run, plan, completed)
        if completed or previous_active != plan.get("active_goal_id"):
            self._forget_turn((run_id, player_id))
        if plan["status"] == "blocked":
            self.status(run_id, player_id, "planner_blocked", plan["reason"], generation)
            await self.coordinator.investigate(run_id, "planner_blocked", plan["reason"], [player_id])
            return None
        if plan["status"] == "handed_back":
            await self._after_give_up(run_id, player_id, generation, plan, subgoals.active_goal(plan))
            return None
        if plan["status"] == "goal_reached":
            await self.coordinator.goal_reached(run_id, player_id, subgoals.completion_claim(plan)["reason"])
            return None
        stuck = subgoals.stuck(plan, needs_plan)
        if stuck:
            tried = [label for _, label in subgoals.active_goal(plan).get("recent_actions") or []]
            return await self._give_up(run_id, player_id, generation, observation, kind="stuck", tried=tried, missing=stuck)
        if not needs_plan:
            return {"observation": observation, "first_actions": []}
        if self._replay_recipe(run_id, player_id, run, plan, observation):
            return {"observation": observation, "first_actions": []}
        # The replan reason is input too: a failed action or first repeat on unchanged state still replans once.
        planning_key = hashlib.sha256(json.dumps([plan["active_goal_id"], generation, wait_signature(observation), plan.get("reason")]).encode()).hexdigest()
        if planning_key == plan.get("last_planning_key"):
            return await self._give_up(run_id, player_id, generation, observation, kind="stuck", tried=[],
                                       missing="Planning was needed again on unchanged game state")
        player = next(p for p in run["plan"]["players"] if p["id"] == player_id)
        reason = plan.get("reason") or ""
        timing["plan_trigger"] = "new_active_goal" if plan.get("active_goal_id") != previous_active else next(
            (label for prefix, label in PLAN_TRIGGERS if reason.startswith(prefix)), "other")
        context = self.decision_context(run, player, observation)
        context["fact_catalog"] = facts.catalog(observation, self.item_ids(run))
        groups, context["action_options"] = batch_options(context)
        revision = plan["revision"]
        self.status(run_id, player_id, "planning", "Finding the next achievable prerequisite for: " + context["current_goal"]["objective"], generation)
        warnings = []
        self.store.event(run_id, "subgoal_planning_context", {"player_id": player_id, "revision": revision, "context": context})
        # A look-ahead planned while the last macro ran is this leaf's first attempt when nothing it saw has changed.
        proposal, ahead = None, self.lookahead.pop((run_id, player_id), None)
        if ahead:
            ahead_goal_id, ahead_revision, scene, task = ahead
            why = ("goal_changed" if ahead_goal_id != plan["active_goal_id"] else "plan_changed" if ahead_revision != revision
                   else "scene_changed" if scene != subgoals.planning_scene(observation) else None)
            if why:
                task.cancel()
            else:
                try:
                    with timed(timing, "plan_model"):
                        # Its first_actions name the macro-start observation's options, so the decider picks instead.
                        proposal = {**await task, "first_actions": []}
                except Exception:
                    why = "error"
            self.store.event(run_id, "lookahead_discarded" if why else "lookahead_used", {"player_id": player_id, "goal_id": ahead_goal_id, "why": why})
        for attempt in range(2):
            warnings = []
            if attempt or proposal is None:
                with timed(timing, "plan_model"):
                    proposal = await self.coordinator.ai.plan_subgoals(context, run_id, prefix=static_prefix(context, None))
            if not current():
                self.store.event(run_id, "subgoal_planning_cancelled", {"player_id": player_id, "reason": "Run control changed during planning"})
                return None
            with timed(timing, "plan_observe"):
                fresh = await self.observe(run_id, player_id)
            if not current():
                return None
            live, _ = self.binding(run_id, player_id)
            latest_plan = live.get("subgoal_plan")
            if (not latest_plan or latest_plan["revision"] != revision or
                    subgoals.planning_scene(fresh) != subgoals.planning_scene(observation)):
                self.store.event(run_id, "subgoal_planning_cancelled", {"player_id": player_id, "reason": "Plan, game binding, or visible controls changed during planning"})
                return None
            try:
                with timed(timing, "plan_validate"):
                    revised = subgoals.apply_decision(latest_plan, proposal, context, warnings)
                    verified = subgoals.reconcile(revised, fresh, generation)
                break
            except ValueError as exc:
                timing["counts"]["plan_rejected"] = timing["counts"].get("plan_rejected", 0) + 1
                reason = subgoals.rejection_message(exc)
                focus = subgoals.active_goal(latest_plan)
                self.store.event(run_id, "subgoal_plan_rejected", {"player_id": player_id, "reason": reason, "attempt": attempt + 1,
                                 "goal_id": focus["id"], "objective": focus["objective"], "revision": revision, "proposal": proposal})
                if attempt:
                    tried = [context["previous_attempt_rejected"]["rejected_answer"], proposal]
                    return await self._give_up(run_id, player_id, generation, fresh, kind="planner", tried=tried,
                                               missing=f"The planner's completion check did not match fact_catalog: {reason}")
                # One corrective retry: show the planner its rejected answer and the validator's reason.
                context = {**context, "previous_attempt_rejected": {"reason": reason, "rejected_answer": proposal}}
        revised["last_planning_key"] = planning_key  # Only an applied plan counts; cancelled or rejected planning may retry.
        live["subgoal_plan"] = revised
        self.store.put("runs", live)
        self.store.event(run_id, "subgoal_plan_updated", {"player_id": player_id, "plan": revised, "warnings": warnings})
        if verified:
            self.store.event(run_id, "subgoals_verified", {"player_id": player_id, "goal_ids": verified,
                             "evidence": subgoals.observation_ref(fresh), "full_observation": fresh})
        self._forget_turn((run_id, player_id))
        if proposal.get("mode") == "blocked":
            await self.report_capability_gap(run_id, player_id, latest_plan, subgoals.active_goal(latest_plan), kind="blocked",
                                             tried=[proposal], missing=proposal.get("reason") or "The planner reported the goal as blocked", observation=fresh)
            await self._after_give_up(run_id, player_id, generation, revised, subgoals.active_goal(latest_plan))
            return None
        if revised["status"] == "goal_reached":
            claim = subgoals.completion_claim(revised)
            self.store.event(run_id, "goal_reached", {"player_id": player_id, "revision": revised["revision"], "claim": claim})
            await self.coordinator.goal_reached(run_id, player_id, claim["reason"])
            return None
        # The scene check above makes the post-planning observation safe to act on; the planner's IDs name pre-planning options.
        picked = picked_options({"actions": proposal.get("first_actions")}, groups, context["action_options"])
        self.status(run_id, player_id, "ready", "Plan updated; acting on fresh state.", generation)
        return {"observation": fresh, "first_actions": [option_key(a) for _, _, a in picked if a and a["tool"] == "perform_action"]}

    def _save_recipes(self, run_id, player_id, run, plan, completed):
        """Save each completed non-root leaf's steps as a win for its completion shape: the planner's or a replayed
        recipe's steps, else the steps its own actions took. Runs before the turn state of the old goal is forgotten."""
        configuration_id = run_configuration(run)[0]
        if not configuration_id:
            return
        intents = self.goal_intents.get((run_id, player_id))
        for goal in plan["goals"]:
            if (goal["id"] not in completed or goal["id"] == plan["root_id"]
                    or any(g.get("parent_id") == goal["id"] for g in plan["goals"])):
                continue
            steps = goal.get("steps") or (recipes.from_intents(intents[1]) if intents and intents[0] == goal["id"] else [])
            if steps:
                saved = recipes.save(self.store, configuration_id, goal["completion"], steps, True)
                self.store.event(run_id, "recipe_saved", {"player_id": player_id, "goal_id": goal["id"], "shape": saved["shape"], "steps": saved["steps"]})

    def _replay_recipe(self, run_id, player_id, run, plan, observation):
        """Give a just-activated leaf a saved recipe's steps instead of calling the planner; True when replayed.

        A leaf reached by a controller replan (a PLAN_TRIGGERS reason, including a failed replay) is planned instead."""
        goal = subgoals.active_goal(plan)
        configuration_id = run_configuration(run)[0]
        if (not configuration_id or goal["id"] == plan["root_id"] or any(g.get("parent_id") == goal["id"] for g in plan["goals"])
                or any((plan.get("reason") or "").startswith(prefix) for prefix, _ in PLAN_TRIGGERS)):
            return False
        recipe = recipes.find(self.store, configuration_id, goal["completion"])
        if not recipe:
            return False
        first = recipe["steps"][0]["verifies"]
        check = goal["completion"] if goal["completion"]["fact"] == first else {"fact": first, "args": []}
        if subgoals.measure({**check, "op": ">=", "value": 0}, observation)[1] is None:
            return False
        goal.update(steps=recipes.bind(recipe["steps"], goal["completion"], replay=True, measured=goal.get("measured")),
                    actionable=True, recipe=recipe["shape"])
        self.store.put("runs", run)
        self.step_index.pop((run_id, player_id), None)  # Replay starts at the recipe's first step.
        ahead = self.lookahead.pop((run_id, player_id), None)
        if ahead:  # The replayed leaf needs no planner call, so its look-ahead is dropped.
            ahead[3].cancel()
            self.store.event(run_id, "lookahead_discarded", {"player_id": player_id, "goal_id": ahead[0], "why": "recipe"})
        self.store.event(run_id, "recipe_replayed", {"player_id": player_id, "shape": recipe["shape"], "goal_id": goal["id"]})
        return True

    def _recipe_failed(self, run_id, player_id, generation, goal_id, reason):
        """Count a failed replay against the recipe and hand the leaf to the planner; True when the replan was requested."""
        run = self.store.require("runs", run_id)
        goal = subgoals.active_goal(run.get("subgoal_plan") or {"goals": []})
        if not goal or goal["id"] != goal_id or not goal.get("recipe"):
            return False
        shape = goal.pop("recipe")
        goal.pop("steps", None)
        goal["actionable"] = False
        self.store.put("runs", run)
        self.step_index.pop((run_id, player_id), None)
        recipes.save(self.store, run_configuration(run)[0], goal["completion"], None, False)
        self.store.event(run_id, "recipe_failed", {"player_id": player_id, "goal_id": goal_id, "shape": shape, "reason": reason})
        return self.request_replan(run_id, player_id, generation, f"Recipe replay stopped ({reason}); plan this leaf.")

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
        self._forget_turn((run_id, player_id))
        return True

    def _failed_again(self, run_id, player_id):
        skip = {None, "running", "accepted", "unknown"}
        settled = []
        for event in self._recent_events(run_id):
            if event["kind"] not in {"action_result", "structured_action_outcome"}:
                continue
            if event["payload"].get("player_id") != player_id:
                continue
            status = (event["payload"].get("result") or {}).get("status")
            if status in skip:
                continue
            settled.append(status)
        return len(settled) >= 2 and settled[-2] in FAILED_STATUSES

    async def _reconcile_pending(self, run_id, player_id, ps, generation, observation):
        """Settle the last action's outcome; True when this turn must end."""
        key = (run_id, player_id)
        result = await self.call_game(run_id, player_id, "get_action_status", {"action_id": self.pending[key]})
        if result.get("status") in {"running", "accepted"}:
            deadline = result.get("expires_at") or (ps.get("pending_action") or {}).get("expires_at")
            if deadline and time.time() > deadline + 5:
                reason = "The game action is still running after its execution deadline. Inspect the action before retrying."
                self.status(run_id, player_id, "error", reason, generation, last_error=reason, error_at=time.time(), action_id=self.pending[key])
                await self.coordinator.investigate(run_id, "unknown_execution", reason, [player_id])
                return True
            self.status(run_id, player_id, "waiting_action", "Waiting for the game to acknowledge the action outcome.", generation, action_id=self.pending[key], wait_until=deadline)
            return True
        if result.get("status") == "completed":
            settle_effect(result, (ps.get("pending_action") or {}).get("before"), observation)
        self.store.event(run_id, "structured_action_outcome", {"player_id": player_id, "result": result})
        if result.get("status", "unknown") == "unknown":
            self.status(run_id, player_id, "error", "The last action outcome is unknown; input is held for review.", generation, action_id=self.pending[key])
            await self.coordinator.investigate(run_id, "unknown_execution", "Structured action acknowledgement could not be reconciled; fresh review required", [player_id])
            return True
        verb = (ps.get("pending_action") or {}).get("verb")
        self.clear_pending(run_id, player_id, generation)
        self.last_decisions.pop(key, None)
        return await self._after_outcome(run_id, player_id, generation, verb, observation, result)

    async def _after_outcome(self, run_id, player_id, generation, verb, observation, result):
        """Apply the settled-outcome rules: stalled-move pause, replan on the second failure; True when the turn must end."""
        if result.get("status") in FAILED_STATUSES:
            if await self._pause_if_stalled(run_id, player_id, generation, verb, observation, result):
                return True
            if self._failed_again(run_id, player_id):
                self.request_replan(run_id, player_id, generation, "The last action failed; reassess prerequisites and approach.")
        return False

    async def _run_batch(self, run_id, player_id, generation, current, context, start, fresh, last, next_action, timing, limit=BATCH_LIMIT, tried=None):
        """Run actions from `next_action(fresh) -> (action | None, why)` back to back, each chosen on the observation the previous action left.

        `start` is the observation the batch was chosen on. `last` is the result of the batch action already run (its
        post-action observation is `fresh`), or None when the batch starts here on `fresh`. With a `tried` set (macros),
        one no_effect that is not a move is tolerated: a use_item's tile joins `tried` and the next pick skips it; a
        second no_effect in a row stops. Returns the stop reason and the actions dispatched here."""
        key = (run_id, player_id)
        run = self.store.require("runs", run_id)
        plan = run.get("subgoal_plan")
        mine = plan and plan["player_id"] == player_id
        checks = [g["completion"] for g in subgoals.goal_path(plan)] if mine else []
        goal = subgoals.active_goal(plan) if mine else None
        facts_before = [subgoals.measure(check, start)[1] for check in checks]
        scene = subgoals.planning_scene(start)
        done, ran, missed = int(last is not None), [], False
        while True:
            if not current():
                return "cancelled", ran
            if last is not None:
                status = last.get("status")
                if status in FAILED_STATUSES:
                    return ("cancelled" if status == "cancelled" else "failed"), ran
                if fresh is None or key in self.pending:
                    return "pending", ran  # Still running after the settle wait; the next turn reconciles it.
                if last.get("effect") == "changed":
                    missed = False
                elif str((last.get("parameters") or {}).get("name", "")).startswith("slot_"):
                    pass  # Re-selecting the equipped slot is already satisfied; it is neither a stop nor a missed tile.
                elif tried is None or missed or not ran or ran[-1]["args"].get("verb") == "move_to":
                    return "no_effect", ran
                else:
                    position = ran[-1]["args"].get("parameters", {}).get("world_position")
                    if position:
                        tried.add(tuple(position))
                    missed = True
            if goal and subgoals.measure(goal["completion"], fresh)[0]:
                return "goal_met", ran
            # Progress toward a goal-path target continues the batch; any other move of those facts is a surprise.
            facts_now = [subgoals.measure(check, fresh)[1] for check in checks]
            if any(old != new and not subgoals.toward(check, old, new) for check, old, new in zip(checks, facts_before, facts_now)):
                return "fact_moved_away", ran
            facts_before = facts_now
            if subgoals.planning_scene(fresh) != scene:
                return "scene_changed", ran
            action, why = next_action(fresh)
            if action is None:
                return why, ran  # "done" for an empty queue, else "not_offered".
            if done >= limit:
                return "limit", ran
            with timed(timing, "tool:perform_action"):
                last = await self.dispatch(run_id, player_id, "perform_action",
                                           {**action["args"], "reason": "Batch: " + action["description"]}, fresh, generation)
            fresh = last.pop("observation", None)
            ran.append(action)
            done += 1

    def _start_lookahead(self, run_id, player_id, run, player, plan, observation):
        """Start planning the leaf after this one in parallel with a code macro, unless a saved recipe will replay it.

        Builds the planning context exactly as advance_plan does, from a copy of the plan with the current leaf completed."""
        key = (run_id, player_id)
        ahead_plan = subgoals.next_leaf(plan)
        if not ahead_plan:
            return
        leaf = subgoals.active_goal(ahead_plan)
        configuration_id = run_configuration(run)[0]
        if (configuration_id and not any(g.get("parent_id") == leaf["id"] for g in ahead_plan["goals"])
                and recipes.find(self.store, configuration_id, leaf["completion"])):
            return
        kept = self.lookahead.get(key)
        if kept and kept[:2] == (leaf["id"], ahead_plan["revision"]):
            return
        if kept:
            kept[3].cancel()
            self.store.event(run_id, "lookahead_discarded", {"player_id": player_id, "goal_id": kept[0], "why": "replaced"})
        context = self.decision_context({**run, "subgoal_plan": ahead_plan}, player, observation)
        context["fact_catalog"] = facts.catalog(observation, self.item_ids(run))
        _, context["action_options"] = batch_options(context)
        task = asyncio.create_task(self.coordinator.ai.plan_subgoals(context, run_id, prefix=static_prefix(context, None)))
        # Retrieve a dropped task's error so asyncio never logs "Task exception was never retrieved".
        task.add_done_callback(lambda done: done.cancelled() or done.exception())
        self.lookahead[key] = (leaf["id"], ahead_plan["revision"], subgoals.planning_scene(observation), task)
        self.store.event(run_id, "lookahead_started", {"player_id": player_id, "goal_id": leaf["id"]})

    async def _run_macro(self, run_id, player_id, generation, current, context, observation, step, timing):
        """Run one macro step (`step["control"]` names a MACROS entry, `step["item"]` the hotbar item) in code, with no model call.

        Selects the item's hotbar slot first, then moves to and uses each matching tile until a stop. Returns the stop
        reason and the actions dispatched."""
        macro, selected, tried = step["control"], False, set()
        def next_action(fresh):
            nonlocal selected
            action, why = macros.next_action(macro, step, {**context, "observation": fresh}, selected, tried)
            selected = selected or why == "select"
            return action, why
        stop, ran = await self._run_batch(run_id, player_id, generation, current, context, observation, observation, None,
                                          next_action, timing, MACRO_LIMIT, tried)
        timing["counts"]["macro_actions"] = timing["counts"].get("macro_actions", 0) + len(ran)
        self.store.event(run_id, "macro_stopped", {"player_id": player_id, "macro": macro, "item": step.get("item", ""), "reason": stop, "actions": len(ran)})
        return stop, ran

    async def turn(self, run_id, player_id, tick_seconds=None):
        key = (run_id, player_id)
        run, ps = self.binding(run_id, player_id)
        generation = ps["generation"]
        if self.generations.get(key) != generation:
            if ps.get("pending_action"):
                self.clear_pending(run_id, player_id, generation)  # Held/rebound input cannot still be pending.
            self.pending.pop(key, None)
            self._forget_turn(key)
            self.loop_counts.pop(key, None)
            self.movement_stalls.pop(key, None)
            self.turn_ended_at.pop(key, None)
            self.bug_reports.pop(key, None)
            self.snapshots.pop(key, None)
            self.code_waits.pop(key, None)
            self.step_index.pop(key, None)
            ahead = self.lookahead.pop(key, None)
            if ahead:
                ahead[3].cancel()
            self.generations[key] = generation
        def current():
            live = self.store.require("runs", run_id)
            return live["state"] in {"Starting", "Running"} and live["players"][player_id]["generation"] == generation and self.control_owners.get(key, "agent") == "agent"
        if not current():
            if self.control_owners.get(key, "agent") != "agent":
                self.status(run_id, player_id, "external_control", "An external MCP session controls this player; autonomous decisions are suspended.", generation)
            return
        started = time.perf_counter()
        store_before = (self.store.write_seconds, self.store.write_bytes, self.store.writes)
        ended = self.turn_ended_at.get(key)
        timing = {"outcome": "interrupted", "started_at": time.time(), "gap_seconds": None if ended is None else started - ended,
                  "tick_seconds": tick_seconds, "capture_seconds": ((ps.get("worker_status") or {}).get("capture") or {}).get("last_seconds"),
                  "context_truncated": None, "plan_trigger": None, "seconds": {}, "calls": {}, "counts": {}, "bytes": {}}
        try:
            previous_status = ps.get("agent_status") or {}
            # Polling a known wait must not replace its useful status with a flash of "observing".
            if key not in self.pending and key not in self.waits and key not in self.last_decisions:
                self.status(run_id, player_id, "observing", "Reading fresh game state.", generation)
            with timed(timing, "observe"):
                observation = await self.observe(run_id, player_id)
            timing["bytes"]["observation"] = json_bytes(observation)
            if key in self.pending:
                with timed(timing, "reconcile_pending"):
                    turn_ends = await self._reconcile_pending(run_id, player_id, ps, generation, observation)
                if turn_ends:
                    timing["outcome"] = "pending_action"
                    return timing["outcome"]
            if not current():
                timing["outcome"] = "cancelled"
                return timing["outcome"]
            planned = await self.advance_plan(run_id, player_id, observation, generation, current, timing)
            if planned is None:
                timing["outcome"] = "planning"
                return timing["outcome"]
            # After a planner call this is the post-planning observation, and the planner may have named the first batch.
            observation, planned_keys = planned["observation"], planned["first_actions"]
            signature = meaningful_signature(observation)
            wait = self.waits.get(key)
            if wait and wait[2] == generation and time.time() < wait[0] and wait_signature(observation) == wait[1]:
                self.status(run_id, player_id, "waiting", previous_status.get("detail") or ps.get("activity") or "Waiting for game state to change.", generation, wait_until=wait[0])
                timing["outcome"] = "waiting"
                return timing["outcome"]
            self.waits.pop(key, None)
            last = self.last_decisions.get(key)
            if not wait and last and last[2] == generation and last[0] == signature and time.time() - last[1] < 30:
                self.status(run_id, player_id, "waiting", "Game state is unchanged; the next decision will retry within 30 seconds.", generation, wait_until=last[1] + 30)
                timing["outcome"] = "unchanged"
                return timing["outcome"]
            run, ps = self.binding(run_id, player_id)
            # After the model chose to wait on a growth fact, keep waiting in code instead of asking it again.
            plan = run.get("subgoal_plan")
            goal = subgoals.active_goal(plan) if plan and plan["player_id"] == player_id else None
            code_wait = self.code_waits.get(key)
            if (not planned_keys and goal and code_wait and code_wait[0] == goal["id"] and code_wait[1] < CODE_WAIT_LIMIT
                    and goal["completion"].get("fact") in TIMER_FACTS and not subgoals.measure(goal["completion"], observation)[0]):
                until = time.time() + CODE_WAIT_STEP
                self.waits[key] = (until, wait_signature(observation), generation)
                self.code_waits[key] = (goal["id"], code_wait[1] + CODE_WAIT_STEP)
                self.status(run_id, player_id, "waiting", "Waiting for crops to grow.", generation, wait_until=until)
                self.store.event(run_id, "code_wait", {"player_id": player_id, "goal_id": goal["id"], "seconds_used": code_wait[1] + CODE_WAIT_STEP})
                timing["outcome"] = "code_wait"
                return timing["outcome"]
            player = next(p for p in run["plan"]["players"] if p["id"] == player_id)
            with timed(timing, "decision_context"):
                context = self.decision_context(run, player, observation)
                context["bug_already_reported"] = self.bug_reports.get(key) == signature
                self.store.event(run_id, "structured_decision_context", {"player_id": player_id, "context": context,
                    "observation_ref": subgoals.observation_ref(observation), "source_revision": run["build"].get("source_revision")})
            timing["bytes"].update(prompt_observation=json_bytes(context["observation"]), decision_context=json_bytes(context))
            timing["context_truncated"] = bool(context["observation"].get("context_truncated"))
            # One snapshot per subgoal sits in the cached prefix; each call sends only the delta against it.
            goal_id = (context.get("current_goal") or {}).get("id")
            view = prompt_observation(context["observation"], max_chars=SNAPSHOT_CHARS)
            kept = self.snapshots.get(key)
            if (not kept or kept[:2] != (goal_id, generation)
                    or len(json.dumps(observation_delta(kept[2], view))) > SNAPSHOT_RESET_CHARS):
                kept = self.snapshots[key] = (goal_id, generation, view)
            self.last_decisions[key] = (signature, time.time(), generation)
            decision_started_at = time.time()
            result, first, stop, ran, finished = None, {}, None, [], False
            # The leaf's next unfinished step runs in code when it names a macro. A macro stop that leaves the step
            # unfinished after dispatching actions hands the next turn to the decider; then the macro is tried again.
            steps = (goal or {}).get("steps") or []
            goal_id, index, decider_turn = self.step_index.get(key) or (None, 0, False)
            if not goal or goal_id != goal["id"]:
                index, decider_turn = 0, False
            # A select right before a macro on the same item is redundant: the macro selects its own item first.
            if (index + 1 < len(steps) and (steps[index]["control"] == "select" or steps[index]["control"].startswith("slot_"))
                    and steps[index + 1]["control"] in macros.MACROS and steps[index + 1]["item"] == steps[index]["item"]):
                index += 1
            step = steps[index] if index < len(steps) else None
            if steps and context.get("current_goal"):
                # The decider and relevant_groups' tile filter see only the unfinished steps, so a finished macro's tiles are not the ones kept.
                context["current_goal"]["steps"] = steps[index:]
            if planned_keys:
                # The planner named the first batch: run it on the post-planning observation, with no decide call.
                stop, ran = await self._run_batch(run_id, player_id, generation, current, context, observation, observation, None, queued_actions(context, planned_keys), timing)
            elif step and step["control"] in macros.MACROS and not decider_turn:
                # The verifying fact reads the goal's plant_id when the goal checks another fact (e.g. item_count), else it would read the zero default.
                check = {"fact": step["verifies"], "args": [a for a in goal["completion"]["args"] if goal["completion"]["fact"] == step["verifies"] or a["name"] == "plant_id"], "op": ">=", "value": 0}
                before = subgoals.measure(check, observation)[1]
                self._start_lookahead(run_id, player_id, run, player, plan, observation)
                stop, ran = await self._run_macro(run_id, player_id, generation, current, context, observation, step, timing)
                after = subgoals.measure(check, self.store.get("observations", f"{run_id}:{player_id}") or {})[1]
                finished = stop in ("goal_met", "done") or bool(step["count"] and before is not None and after is not None and abs(after - before) >= step["count"])
                self.step_index[key] = (goal["id"], index + finished, not finished and bool(ran))
            elif decider_turn:
                self.step_index[key] = (goal["id"], index, False)
            # A replayed recipe whose step stops unfinished hands the leaf to the planner on the next turn, with no decide call now.
            replanned = bool(goal and goal.get("recipe") and not finished and stop in ("failed", "no_effect", "not_offered")
                             and self._recipe_failed(run_id, player_id, generation, goal["id"], stop))
            if not replanned and (stop is None or (stop == "not_offered" and not ran)):
                # No planner batch, or its first option is gone: the model decides in this same turn.
                label = PROVIDERS[provider_of(run.get("model") or run["plan"].get("model", MODEL))]["label"]
                self.status(run_id, player_id, "thinking", f"Waiting for {label} to choose the next action.", generation)
                fresh = None  # The post-action observation of the batch's first action.
                async def dispatch(name, args):
                    nonlocal observation, fresh
                    if not current():
                        return {"status": "cancelled"}
                    self.status(run_id, player_id, "tool", f"Using {name}.", generation)
                    with timed(timing, "tool:" + name):
                        result = await self.dispatch(run_id, player_id, name, args, observation, generation)
                    if name == "perform_action":
                        fresh = result.pop("observation", None)  # Stored already as the latest observation; keep it out of the ledger.
                    if name == "observe" and result.get("observation_id"):
                        observation = result
                        result = prompt_observation(result)
                    self.store.event(run_id, "structured_tool_result", {"player_id": player_id, "tool": name, "result": result})
                    return result
                with timed(timing, "decide"):
                    result = await self.coordinator.ai.batch_turn(context, kept[2], dispatch, current, run_id)
                first = (result or {}).get("action") or {}
                if result is None:
                    stop = "cancelled"
                elif first.get("tool") == "wait_for_change":
                    stop = "wait"
                elif first.get("tool") != "perform_action":
                    stop = "done"  # A game bug report: nothing was dispatched.
                else:
                    stop, ran = await self._run_batch(run_id, player_id, generation, current, context, observation, fresh,
                                                      result["result"], queued_actions(context, result["queued"]), timing)
            timing["outcome"] = "decided"
            timing["counts"]["batch_actions"] = len(ran) + (first.get("tool") == "perform_action")
            timing["batch_stop"] = stop
            if result:
                timing["bytes"].update(result["bytes"])
            self.store.event(run_id, "batch_stopped", {"player_id": player_id, "reason": stop, "actions": timing["counts"]["batch_actions"]})
            # Code waits start only after the model itself chose to wait on a growth goal; any other batch ends them.
            current_goal = context.get("current_goal") or {}
            if stop == "wait" and current_goal.get("completion", {}).get("fact") in TIMER_FACTS:
                if self.code_waits.get(key, (None,))[0] != current_goal["id"]:
                    self.code_waits[key] = (current_goal["id"], 0)
            else:
                self.code_waits.pop(key, None)
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
                with timed(timing, "issue_reports"):
                    # Once per turn, on the batch's last action.
                    backstop = {**(result or {}), "action": ran[-1]} if ran else result
                    await self._loop_backstop(run_id, player_id, generation, signature, observation, backstop)
                    if isinstance(result, dict) and result.get("status") == "bug_reported":
                        self.bug_reports[key] = signature
                        await self.report_bug(run_id, player_id, result.get("report") or {}, observation)
        except ProviderError as exc:
            timing["outcome"] = "provider_error"
            if current():
                if exc.status == "decision_needs_instructions":
                    report = exc.report or {}
                    await self.report_unsupported(run_id, player_id, report.get("source", "agent_signal"), {
                        "tool": "perform_action" if report.get("wanted_verb") in GAME_VERBS else None,
                        "verb": report.get("wanted_verb"), "target_id": report.get("wanted_target_id"),
                        "blocker": report.get("blocker"), "follow_up_error": report.get("follow_up_error")}, observation)
                if exc.status == "decision_needs_instructions" and self.request_replan(run_id, player_id, generation,
                        "No supported direct action fits. Decompose missing prerequisites or explain the blocker."):
                    self.status(run_id, player_id, "planning", "The current goal needs a smaller prerequisite or another approach.", generation)
                    return timing["outcome"]
                self.status(run_id, player_id, "error", exc.message, generation, last_error=exc.message, error_at=time.time())
                category = "decision_needs_instructions" if exc.status == "decision_needs_instructions" else "provider_failure"
                self.store.event(run_id, "agent_turn_failed", {"player_id": player_id, "category": category, "provider_status": exc.status, "error": exc.message})
                if exc.status == "spend_limit":
                    await self.coordinator.finish(run_id, "limit_reached", limitation=exc.message)
                else:
                    await self.coordinator.investigate(run_id, category, exc.message, [player_id])
        except Exception as exc:
            timing["outcome"] = "error"
            if current():
                reason = f"{type(exc).__name__}: {exc}"
                # Only a worker-confirmed unopened bridge gets the bounded startup retry.
                if getattr(exc, "code", None) == "game_bridge_starting" and time.time() - ps.get("launched_at", 0) < 15:
                    self.status(run_id, player_id, "startup_retry", f"Waiting for game interface startup: {reason}", generation, last_error=reason, error_at=time.time(), wait_until=ps["launched_at"] + 15)
                    timing["outcome"] = "startup_retry"
                    return timing["outcome"]
                self.status(run_id, player_id, "error", reason, generation, last_error=reason, error_at=time.time())
                trace = "".join(traceback.format_exception(exc)[-4:])[-1500:]
                self.store.event(run_id, "agent_turn_failed", {"player_id": player_id, "category": "structured_interface_failure", "error": reason, "traceback": trace})
                await self.coordinator.investigate(run_id, "structured_interface_failure", reason, [player_id])
                # Report after investigate, as the coordinator does: the failure hold is not delayed by the screenshot pin.
                await self.report_crash(run_id, player_id, "agent_turn_failure", "structured_interface_failure", f"{reason}\n{trace}")
        finally:
            self._record_turn_timing(run_id, player_id, timing, started, store_before)
        return timing["outcome"]

    def _record_turn_timing(self, run_id, player_id, timing, started, store_before):
        """One ledger row per turn; numbers only, never observation content. Never raises."""
        try:
            store_after = (self.store.write_seconds, self.store.write_bytes, self.store.writes)
            seconds, written, writes = (after - before for before, after in zip(store_before, store_after))
            payload = {**timing, "player_id": player_id, "total_seconds": time.perf_counter() - started,
                       "store": {"seconds": round(seconds, 6), "bytes": written, "writes": writes}}
            for field in ("total_seconds", "gap_seconds", "tick_seconds"):
                if payload[field] is not None:
                    payload[field] = round(payload[field], 6)
            payload["seconds"] = {step: round(value, 6) for step, value in timing["seconds"].items()}
            self.store.event(run_id, "agent_turn_timing", payload)
        except Exception:
            LOG.exception("Could not record turn timing for %s/%s", run_id, player_id)
        self.turn_ended_at[(run_id, player_id)] = time.perf_counter()
