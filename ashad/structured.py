"""Shared structured-play handlers for Decisions, dashboard inspection and MCP."""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import time
import traceback
from collections import Counter
from datetime import datetime, timezone

from .ai import PROMPT_OBSERVATION_CHARS, ProviderError, jev_triage, planning_evidence, static_prefix
from .game_controls import run_configuration
from .menu import GAME_VERBS, capability_summary, option_key, prompt_observation, resolve
from .models import JEV_MODEL, MODEL, PROVIDERS, provider_of
from .repository import Repository
from .store import uid
from . import facts, repair_tasks, repeat, skills, subgoals

LOG = logging.getLogger(__name__)
BATCH_LIMIT = 5  # Actions per turn from one batch decide.
REPEAT_LIMIT = 30  # Actions per turn from one replay.
REPLAY_MISSES = ("failed", "no_effect", "fact_moved_away", "not_offered")  # Replay stops that mark the source missed on the leaf.
FAILED_KEPT = 8  # Failed actions a leaf keeps out of the decider menus.
# ponytail: a fixed cap so a label that ticks on its own (a countdown) cannot reset a leaf's attempts forever; a flow with
# more distinct screens than this spends attempts past it, so raise it if a real menu or cutscene flow ever needs more.
MAX_SCREENS = 32  # New screens per leaf that count as progress while the player cannot act.
TIMER_FACTS = ("crops_harvestable", "crop_growth_stage_min")  # Growth facts the code keeps waiting on after a wait.
# v6 wheat: 4-6 s per growth stage (wheat_seed.tres initial_growth_timer), 3 stages to max_growth_state 4,
# and the timer runs only on a Watered tile (tile.gd:106). Watered wheat is ripe in 12-18 s; unwatered never.
CODE_WAIT_STEP = 6  # One stage at most; a stage change ends the wait sooner (wait_signature).
REUSE_OBSERVATION_SECONDS = 1.0  # A post-action observation this young is the turn's observation; no second observe.
ISSUE_PREFIXES = {"unsupported_action": "uae_", "crash": "crash_", "game_bug": "bug_", "false_completion": "done_", "capability_gap": "gap_", "skill_regression": "reg_"}


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


def selected_slot(observation):
    """The hotbar index the game shows as selected; the hotbar entries do not say, only the HotbarUI entities do."""
    return next((e["state"].get("inventory_index") for e in observation.get("entities", [])
                 if e.get("parent") == "HotbarUI" and e.get("state", {}).get("selected")), None)


def wait_signature(observation):
    state = observation.get("state", {})
    player = state.get("player", {})
    # Timers, animation, camera motion and moving bystanders must not cause inference.
    facts = {"generation": observation.get("generation"), "ready": observation.get("ready"),
             "inventory": player.get("inventory"), "hotbar": player.get("hotbar"), "selected_slot": selected_slot(observation),
             "objectives": state.get("objectives"), "completed": state.get("completed_quests"),
             "tiles": state.get("tiles"), "facts": state.get("facts"),
             "targets": [{"id": e.get("id"), "actions": e.get("actions"),
                          "crafting": e.get("state", {}).get("is_crafting")}
                         for e in observation.get("entities", []) if e.get("kind") != "ground_item"]}
    return hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()


def is_select_input(action):
    """A hotbar select input: pressing an already-selected slot is harmless, so it is never banned from the menus."""
    args = action.get("args", {})
    name = str(args.get("parameters", {}).get("name", ""))
    return args.get("verb") == "input" and (name.startswith("slot_") or name in ("hotbar_prev", "hotbar_next"))


def shown_text(observation):
    """The text every UI element shows, e.g. a cutscene's typewriter line, which has no actions."""
    return sorted(str(e.get("label", "")) for e in observation.get("entities", []) if e.get("kind") == "ui")


def effect_basis(observation):
    basis = {"facts": wait_signature(observation), "position": observation.get("state", {}).get("player", {}).get("position")}
    if not subgoals.playable(observation):
        # Off a playable world a press may only change text: the first next_text in a cutscene finishes its typewriter line.
        # In a playable world HUD labels such as a clock change on their own, so text is left out there.
        basis["text"] = hashlib.sha256(json.dumps(shown_text(observation)).encode()).hexdigest()[:12]
    return basis


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


def screen_id(observation):
    """A short id of the screen: its actionable UI controls by node, as skills.signature names them, and the text it shows,
    so each cutscene slide is its own screen."""
    screen = skills.signature({"fact": "", "args": [], "op": ""}, observation, "")["screen"]
    return hashlib.sha256(json.dumps([screen, shown_text(observation)]).encode()).hexdigest()[:12]


def is_leaf(plan, goal):
    """A non-root goal with no children: the goal the state machine counts attempts on."""
    return goal["id"] != plan["root_id"] and not any(g.get("parent_id") == goal["id"] for g in plan["goals"])


class StructuredPlayers:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.store = coordinator.store
        self.waits = {}
        self.pending = {}
        self.generations = {}
        self.control_owners = {}
        self.bug_reports = {}
        self.turn_ended_at = {}
        self.goal_intents = {}  # (run_id, player_id) -> (goal_id, [repeat.step_for of each agent action]), saved as a skill when the goal completes
        self.held = {}  # (run_id, player_id) -> item_id of the hotbar slot the controller last pressed; the game does not report it
        self.crashes_recorded = set()

    def _forget_turn(self, key):
        self.waits.pop(key, None)
        # The intents belong to one goal; a replan of the same goal keeps them. The leaf's own state lives on its goal record.
        plan = self.store.require("runs", key[0]).get("subgoal_plan") or {}
        if key in self.goal_intents and self.goal_intents[key][0] != plan.get("active_goal_id"):
            del self.goal_intents[key]

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
        if owner == "agent" and args.get("target_id"):
            # Fact ticks (crop growth) bump target versions while a batch, repeat or skill step waits; send the current one.
            latest = self.store.get("observations", f"{run_id}:{player_id}") or observation
            player = next(p for p in run["plan"]["players"] if p["id"] == player_id)
            menu = {"observation": latest, "player": player, "transport": run["plan"]["transport"],
                    "world_namespace": run.get("world_namespace"), "seed": run["plan"].get("seed"), "bug_already_reported": True}
            offered = resolve(menu, option_key({"tool": "perform_action", "args": args}))
            entity = observed_entity(latest, args["target_id"])
            if offered is None and not entity:
                return {"status": "rejected", "reason": "Target changed: no longer offered on the latest observation"}
            # A drag onto an expanded destination is not in the menu; its source entity's version is the one to send.
            if offered:
                args = {**args, "target_version": offered["args"]["target_version"]}
            elif type(entity.get("version")) is int:
                args = {**args, "target_version": entity["version"]}
            observation = latest
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
            goal_id = run["subgoal_plan"].get("active_goal_id")
            if self.goal_intents.get(key, (None,))[0] != goal_id:
                self.goal_intents[key] = (goal_id, [])
            self.goal_intents[key][1].append(repeat.step_for(payload, observation))
            name = str(payload["parameters"].get("name", ""))
            if payload["verb"] == "input" and name.startswith("slot_"):
                # The press is assumed to land, because a failed slot press is rare and the next press corrects it.
                hotbar = observation.get("state", {}).get("player", {}).get("hotbar") or []
                self.held[key] = next((e.get("item_id") or "" for e in hotbar if isinstance(e, dict) and e.get("input") == name), "")
        elif owner != "agent":
            self.held.pop(key, None)  # A human or MCP session may have changed the hotbar, so the held item is unknown again.
        self.store.put("runs", run)
        self.status(run_id, player_id, "acting", str(args.get("reason") or f"Performing {args['verb']}"), generation, action_id=action_id)
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
            self._count_invalid(run_id, player_id, generation, result)
            if result.get("status") == "completed":
                settle_effect(result, ps["pending_action"]["before"], fresh)
            self.store.event(run_id, "structured_action_outcome", {"player_id": player_id, "result": result})
            self.clear_pending(run_id, player_id, generation)
            result["observation"] = fresh
            return result
        if fresh is not None:
            result["observation"] = fresh
        if result.get("status", "unknown") not in {"running", "accepted", "unknown"}:
            self.clear_pending(run_id, player_id, generation)
        self._count_invalid(run_id, player_id, generation, result)
        if result.get("status") in FAILED_STATUSES:
            self.status(run_id, player_id, "action_failed", str(result.get("reason") or result.get("status")), generation, action_id=action_id)
        return result

    def _count_invalid(self, run_id, player_id, generation, result):
        """The pause counts consecutive failures: a settled non-failed result resets it; in-progress results and stale-target rejections (no input delivered) are skipped."""
        failed = result.get("status") in FAILED_STATUSES
        if result.get("status", "unknown") in {"running", "accepted", "unknown"} or (failed and result.get("status") == "rejected" and "Target changed" in str(result.get("reason"))):
            return
        latest = self.store.require("runs", run_id)
        ps = latest["players"][player_id]
        if ps["generation"] == generation:
            ps["invalid_actions"] = ps.get("invalid_actions", 0) + 1 if failed else 0
            self.store.put("runs", latest)

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
                     else "game_bug" if effects and all(e == "no_effect" for e in effects) else None)
            source = "rule"
            if cause is None:
                cause = "planner_gap"  # The rule's default when it can't decide.
                gap = {"objective": focus["objective"], "completion": check, "measured": focus.get("measured"), "missing": missing[:500],
                       "tried": [str(t)[:200] for t in tried][:10], "effects": dict(Counter(effects))}
                label, confidence = await jev_triage(gap, run["plan"].get("jev_model") or JEV_MODEL, self.coordinator.credentials)
                self.store.event(run_id, "jev_check", {"player_id": player_id, "goal_id": focus["id"], "check": "triage",
                                                       "choice": label, "confidence": confidence, "applied": label is not None})
                if label:
                    cause, source = label, "jev"
            basis = {"kind": kind, "cause": cause, "focus_objective": repair_tasks.target_label(focus["objective"]),
                     "missing": re.sub(r"(?<=\.)\d+(?=[.)])", "#", missing)[:200]}
            bounded = []
            for item in tried:
                text = json.dumps(item, ensure_ascii=False)
                bounded.append(item if len(text) <= 2000 else text[:2000])
            shown = missing
            if known:
                shown += f" (measured {focus.get('measured')}, target {check['op']} {check['value']})"
            sections = {"gap": {"kind": kind, "cause": cause, "cause_source": source, "missing": shown, "tried": bounded,
                "subgoal": {"id": focus["id"], "objective": focus["objective"], "completion": focus["completion"],
                            "goal_path": [g["objective"] for g in subgoals.goal_path(plan, focus["id"])]}},
                "capabilities": capability_summary(observation)}
            await self._record(run, ps, player_id, "capability_gap", basis, "agent", observation, sections, skip_pin=kind != "broken",
                               event_fields={"goal_id": focus["id"]})
        except Exception:
            LOG.exception("Could not record capability gap for %s/%s", run_id, player_id)

    async def report_skill_regression(self, run_id, player_id, record, kind, reason):
        """Record a skill that worked on an earlier build failing on this one; never raises or blocks the turn."""
        try:
            run, ps = self.binding(run_id, player_id)
            build_id = run["build"].get("id")
            basis = {"skill": record["name"][:160], "kind": kind, "build_id": build_id, "good_build": record["won_builds"][0]}  # First win: stable, so one issue per (skill, failed build).
            sections = {"regression": {"skill": record["name"], "kind": kind, "good_build": basis["good_build"], "build_id": build_id,
                                       "won_builds": record["won_builds"], "reason": reason, "wins": record["wins"], "fails": record["fails"]}}
            await self._record(run, ps, player_id, "skill_regression", basis, "agent",
                               self.store.get("observations", f"{run_id}:{player_id}") or {}, sections)
        except Exception:
            LOG.exception("Could not record skill regression for %s/%s", run_id, player_id)

    async def report_false_completion(self, run_id, player_id, plan, note):
        """Record a completion claim the operator rejected and count a failure for the plan patterns behind it: the one it
        was saved as and the cached one it started from; never raises."""
        try:
            run, ps = self.binding(run_id, player_id)
            claim = subgoals.completion_claim(plan)
            root = next(g for g in plan["goals"] if g["id"] == plan["root_id"])
            for key in {claim.pop("plan_saved", None), root.pop("plan_pattern", None)} - {None}:
                skills.save_plan(self.store, run.get("project_id"), root, [], False, build_id=run["build"].get("id"), run_id=run_id, key=key)
            goals = {g["id"]: g for g in plan["goals"]}
            basis = {"primary_goal": repair_tasks.target_label(plan["primary_goal"])}
            sections = {"claim": {"reason": claim["reason"], "operator_note": note,
                "verified_goals": [{k: goals[i][k] for k in ("id", "objective", "completion")} for i in claim["verified_goal_ids"]],
                "plan_revision": plan["revision"]}}
            await self._record(run, ps, player_id, "false_completion", basis, "operator", self.store.get("observations", f"{run_id}:{player_id}") or {}, sections, skip_pin=True)
        except Exception:
            LOG.exception("Could not record false completion for %s/%s", run_id, player_id)

    def forget_run(self, run_id):
        for counters in (self.bug_reports, self.turn_ended_at, self.goal_intents, self.held):
            for key in [k for k in counters if k[0] == run_id]:
                del counters[key]
        self.crashes_recorded -= {e for e in self.crashes_recorded if e[0] == run_id}

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
        """Verify on observations; infer only on expansion or a broken leaf.

        Returns None when the turn ends, else the observation to act on."""
        run, _ = self.binding(run_id, player_id)
        plan = run.get("subgoal_plan")
        if not plan or plan["player_id"] != player_id:
            return {"observation": observation}  # Historical multi-player/scenario runs retain their existing contract.
        previous_active = plan.get("active_goal_id")
        with timed(timing, "plan_reconcile"):
            completed = subgoals.reconcile(plan, observation, generation)
            needs_plan = subgoals.needs_planning(plan)
        self.store.put("runs", run)
        if completed:
            self.store.event(run_id, "subgoals_verified", {"player_id": player_id, "goal_ids": completed,
                             "evidence": subgoals.observation_ref(observation), "full_observation": observation})
            self._save_leaf_skills(run_id, player_id, run, plan, completed)
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
        # The goal is claimed only in code: when its own check passes (or, for a goal with no check, every stage is verified).
        reason = subgoals.reached(plan, observation)
        if reason:
            subgoals.claim(plan, observation, reason)
            claim = subgoals.completion_claim(plan)
            if claim["checked"]:
                # Only a plan that reached its goal's check is saved for reuse; the operator's "not done" counts against it.
                claim["plan_saved"] = self._save_plan(run, plan)
            self.store.put("runs", run)
            self.store.event(run_id, "subgoal_plan_updated", {"player_id": player_id, "plan": plan, "warnings": [], "source": "goal_reached"})
            self.store.event(run_id, "goal_reached", {"player_id": player_id, "revision": plan["revision"], "claim": claim})
            await self.coordinator.goal_reached(run_id, player_id, reason)
            return None
        if subgoals.broken(plan):
            await self._break(run_id, player_id, generation, observation,
                              f"Broken: {subgoals.BUDGET} turns on this leaf made no progress; plan another approach.")
            return None
        goal = subgoals.active_goal(plan)
        if (needs_plan and is_leaf(plan, goal) and not goal.get("replanned")
                and any(k not in (goal.get("missed") or []) for k, _, _ in self._saved_skills(run.get("project_id"), goal, observation))):
            # A saved skill for this leaf's shape replays in the turn (_replay) instead of a planner call; a re-planned leaf is planned.
            goal["actionable"] = True
            self.store.put("runs", run)
            needs_plan = False
        if not needs_plan:
            return {"observation": observation}
        reason = plan.get("reason") or ""
        timing["plan_trigger"] = "new_active_goal" if plan.get("active_goal_id") != previous_active else (
            re.match(r"\w*", reason).group().casefold() or "other")
        context = self._planning_context(run, player_id, observation)
        cached = await self._cached_plan(run_id, player_id, generation, run, plan, context, observation)
        if cached:
            return cached
        revision = plan["revision"]
        self.status(run_id, player_id, "planning", "Finding the next achievable prerequisite for: " + context["current_goal"]["objective"], generation)
        warnings = []
        # What plan_subgoals sends after its prefix (logged); plan_subgoals gets the full context to build that prefix.
        sent = planning_evidence(context)
        self.store.event(run_id, "subgoal_planning_context", {"player_id": player_id, "revision": revision, "context": sent,
                         "prefix_sha": hashlib.sha256(static_prefix(context).encode()).hexdigest()[:12]})
        for attempt in range(2):
            warnings = []
            self.store.event(run_id, "plan_routed", {"player_id": player_id, "route": "opus", "trigger": timing["plan_trigger"]})
            with timed(timing, "plan_model"):
                proposal = await self._plan_subgoals(run_id, player_id, context, observation)
            if not current():
                self.store.event(run_id, "subgoal_planning_cancelled", {"player_id": player_id, "reason": "Run control changed during planning"})
                return None
            with timed(timing, "plan_observe"):
                fresh = await self.observe(run_id, player_id)
            if not current():
                return None
            live, _ = self.binding(run_id, player_id)
            latest_plan = live.get("subgoal_plan")
            # A UI control appearing mid-call does not void a plan of whole stages; only a new revision or world does.
            if (not latest_plan or latest_plan["revision"] != revision
                    or fresh.get("world_id") not in ("", observation.get("world_id"))):
                self.store.event(run_id, "subgoal_planning_cancelled", {"player_id": player_id, "reason": "Plan or world changed during planning"})
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
                sent = {**sent, "previous_attempt_rejected": context["previous_attempt_rejected"]}
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
        self.status(run_id, player_id, "ready", "Plan updated; acting on fresh state.", generation)
        return {"observation": fresh}

    def _planning_context(self, run, player_id, observation):
        """The planner's decision context with the fact_catalog for this observation."""
        player = next(p for p in run["plan"]["players"] if p["id"] == player_id)
        context = self.decision_context(run, player, observation)
        context["fact_catalog"] = facts.catalog(observation, self.item_ids(run))
        return context

    async def _cached_plan(self, run_id, player_id, generation, run, plan, context, observation):
        """Jev hybrid: expand an unplanned root from a saved plan pattern Jev picks, with its args and the goal's count, instead of calling Opus.

        Returns the turn's observation when the pattern was applied, else None (the planner takes the goal)."""
        project_id = run.get("project_id")
        root = next(g for g in plan["goals"] if g["id"] == plan["root_id"])
        if (not project_id or plan["active_goal_id"] != root["id"]
                or subgoals.stages(plan) or plan.get("completion_rejected")):
            return None
        patterns = skills.plan_patterns(self.store, project_id)
        pick = await self.coordinator.ai.jev_plan_pick(root["objective"], patterns, context["fact_catalog"], context["item_ids"], run_id)
        live, _ = self.binding(run_id, player_id)
        if not pick["reason"] and (live.get("subgoal_plan") or {}).get("revision") != plan["revision"]:
            pick["reason"] = "stale"  # The plan moved during the request.
        self.store.event(run_id, "plan_pick", {"player_id": player_id, **pick})
        if pick["reason"]:
            return None  # The planner decides.
        key, args = pick["key"], pick["args"]
        record = dict(patterns)[key]
        count = skills.count_of(root["objective"])
        warnings = []
        try:
            if record["uses_count"] and count is None:
                raise ValueError("The goal names no single count")
            proposal = skills.instantiate(record, args, count, context["fact_catalog"]["values"])
            revised = subgoals.apply_decision(live["subgoal_plan"], proposal, context, warnings)
        except ValueError as exc:
            self.store.event(run_id, "plan_pattern_rejected", {"player_id": player_id, "key": key, "reason": subgoals.rejection_message(exc)})
            return None
        next(g for g in revised["goals"] if g["id"] == revised["root_id"])["plan_pattern"] = key
        verified = subgoals.reconcile(revised, observation, generation)
        live["subgoal_plan"] = revised
        self.store.put("runs", live)
        self.store.event(run_id, "subgoal_plan_updated", {"player_id": player_id, "plan": revised, "warnings": warnings, "source": "plan_pattern"})
        self.store.event(run_id, "plan_routed", {"player_id": player_id, "route": "plan_cache", "key": key, "name": record["name"]})
        if verified:
            self.store.event(run_id, "subgoals_verified", {"player_id": player_id, "goal_ids": verified,
                             "evidence": subgoals.observation_ref(observation), "full_observation": observation})
        self._forget_turn((run_id, player_id))
        return {"observation": observation}

    async def _plan_subgoals(self, run_id, player_id, context, observation):
        """plan_subgoals, retried once on context_limit with a trimmed context built from the raw observation.

        context keeps game_controls and item_ids: plan_subgoals builds the cached prefix from them and sends the rest."""
        try:
            return await self.coordinator.ai.plan_subgoals(context, run_id)
        except ProviderError as exc:
            if exc.status != "context_limit":
                raise
        trimmed = {k: v for k, v in context.items() if k != "known_locations"} | {
            "recent_own_actions": context["recent_own_actions"][-3:], "guidance": json.dumps(context["guidance"], ensure_ascii=False)[:1000],
            "observation": prompt_observation(observation, max_chars=PROMPT_OBSERVATION_CHARS // 2)}
        size = json_bytes(planning_evidence(trimmed))
        self.store.event(run_id, "planner_context_trimmed", {"player_id": player_id, "before_bytes": json_bytes(planning_evidence(context)), "after_bytes": size})
        try:
            return await self.coordinator.ai.plan_subgoals(trimmed, run_id)
        except ProviderError as exc:
            if exc.status != "context_limit":
                raise
            raise ProviderError("context_limit", f"Planner context still exceeds the budget after trimming ({size} bytes). "
                                "Report this; Resume will not help until the game state shrinks.") from exc

    def _save_leaf_skills(self, run_id, player_id, run, plan, completed):
        """Save each completed non-root leaf's steps as a win for its completion shape: the planner's or a replayed
        skill's steps unless their replay missed, else the steps its own actions took. Runs before the turn state of the
        old goal is forgotten."""
        project_id = run.get("project_id")
        if not project_id:
            return
        intents = self.goal_intents.get((run_id, player_id))
        for goal in plan["goals"]:
            if (goal["id"] not in completed or goal["id"] == plan["root_id"]
                    or any(g.get("parent_id") == goal["id"] for g in plan["goals"])):
                continue
            missed = (goal.get("skill_key") or "steps") in (goal.get("missed") or [])
            steps = (not missed and goal.get("steps")) or (skills.from_intents(intents[1]) if intents and intents[0] == goal["id"] else [])
            if steps:
                saved = skills.save(self.store, project_id, goal["completion"], steps, True, build_id=run["build"].get("id"), run_id=run_id)
                self.store.event(run_id, "skill_saved", {"player_id": player_id, "goal_id": goal["id"], "skill": saved["name"], "steps": saved["steps"]})

    def _save_plan(self, run, plan):
        """Save the plan whose goal check passed as the project's plan pattern for goals like this one: its stages are the
        tree's completed leaves in depth-first order (without the built-in enter stage), so a plan the planner re-planned or
        expanded on the way saves as the flat path that won. Returns the saved key, else None."""
        project_id = run.get("project_id")
        if not project_id:
            return None
        root = next(g for g in plan["goals"] if g["id"] == plan["root_id"])

        def won_leaves(parent_id):
            leaves = []
            for goal in plan["goals"]:
                if goal.get("parent_id") != parent_id or goal.get("retired") or goal["status"] != "completed" or goal["id"] == subgoals.ENTER or goal.get("gave_up"):
                    continue
                has_children = any(g.get("parent_id") == goal["id"] and not g.get("retired") for g in plan["goals"])
                leaves += won_leaves(goal["id"]) if has_children else [goal]
            return leaves

        stages = won_leaves(root["id"])
        if not stages:
            return None
        key = skills.plan_key(project_id, root, stages)
        saved = skills.save_plan(self.store, project_id, root, stages, True, build_id=run["build"].get("id"), run_id=run["id"], key=key)
        if not saved:
            return None
        self.store.event(run["id"], "plan_saved", {"player_id": plan["player_id"], "key": key, "name": saved["name"]})
        return key

    def _active(self, run_id, player_id):
        """The stored run and this player's active goal, or None when the run has no plan for this player."""
        run = self.store.require("runs", run_id)
        plan = run.get("subgoal_plan")
        return run, subgoals.active_goal(plan) if plan and plan["player_id"] == player_id else None

    async def _break(self, run_id, player_id, generation, observation, reason):
        """The only re-plan trigger: the active leaf is re-planned once, and a second break on it gives up with one gap.

        Returns False when there is no current leaf to break."""
        run, goal = self._active(run_id, player_id)
        if not goal or run["players"][player_id]["generation"] != generation:
            return False
        outcome = "gap" if goal.get("replanned") else "replan"
        self.store.event(run_id, "subgoal_broken", {"player_id": player_id, "goal_id": goal["id"], "reason": reason, "outcome": outcome})
        if outcome == "gap":
            await self._give_up(run_id, player_id, generation, observation, kind="broken", tried=goal.get("failed", []), missing=reason)
            return True
        goal.update(replanned=True, attempts=0)
        for spent in ("missed", "failed", "step", "skill", "skill_key"):
            goal.pop(spent, None)
        plan = run["subgoal_plan"]
        root = next(g for g in plan["goals"] if g["id"] == plan["root_id"])
        if root.get("plan_pattern"):
            # The first re-plan under a cached plan counts once against its pattern.
            skills.save_plan(self.store, run.get("project_id"), root, [], False, build_id=run["build"].get("id"), run_id=run_id,
                             key=root.pop("plan_pattern"))
        self.store.put("runs", run)
        self.request_replan(run_id, player_id, generation, reason)
        self.status(run_id, player_id, "planning", "Re-planning the current goal: " + reason, generation)
        return True

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
        self._count_invalid(run_id, player_id, generation, result)
        self.clear_pending(run_id, player_id, generation)
        return False  # A failure is one more attempt on the leaf, counted by the turn that dispatched it.

    async def _run_batch(self, run_id, player_id, generation, current, context, start, fresh, last, next_action, timing, limit=BATCH_LIMIT, tried=None):
        """Run actions from `next_action(fresh) -> (action | None, why)` back to back, each chosen on the observation the previous action left.

        `start` is the observation the batch was chosen on. `last` is the result of the batch action already run (its
        post-action observation is `fresh`), or None when the batch starts here on `fresh`. With a `tried` set (repeat steps),
        one no_effect that is not a move is tolerated: the used target and every offered target of the same kind join `tried`; a
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
                elif ran and str(ran[-1]["args"].get("parameters", {}).get("name", "")).startswith("slot_"):
                    pass  # Re-selecting the equipped slot is already satisfied; it is neither a stop nor a missed tile.
                elif tried is None or missed or not ran or ran[-1]["args"].get("verb") == "move_to":
                    return "no_effect", ran
                else:
                    target = repeat.action_key(ran[-1])
                    if target is not None:
                        tried.add(target)
                    tried.update(repeat.same_kind(ran[-1], {**context, "observation": fresh}))
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
            with timed(timing, "tool:act:perform_action"):
                last = await self.dispatch(run_id, player_id, "perform_action",
                                           {**action["args"], "reason": "Batch: " + action["description"]}, fresh, generation)
            fresh = last.pop("observation", None)
            ran.append(action)
            done += 1

    def _step_replayer(self, key, context, steps, completion, cursor):
        """A next_action over the steps from cursor["index"], advancing it as steps finish.

        An untargeted step finishes on one use (or its select, or its ordinary input). A targeted step repeats until no
        target is left or, with a count, until its verifying fact moved by count (count uses when it verifies nothing).
        cursor["tried"] holds the current step's no_effect targets; _run_batch adds to it."""
        state, uses, before = None, 0, None

        def verifying(step):
            # The verifying fact reads the goal's args that it also takes (e.g. plant_id); others read its default.
            names = facts.FACTS.get(step["verifies"], ((),))[0]
            return {"fact": step["verifies"], "args": [a for a in completion["args"] if a["name"] in names], "op": ">=", "value": 0}

        def finish():
            nonlocal state
            cursor["index"] += 1
            cursor["tried"].clear()
            state = None

        def next_action(fresh):
            nonlocal state, uses, before
            while cursor["index"] < len(steps):
                step = steps[cursor["index"]]
                if state is None:
                    state = {"select": "", "tried": cursor["tried"], "skipped": False, "held": self.held.get(key, "")}
                    uses, before = 0, subgoals.measure(verifying(step), fresh)[1]
                if step.get("targets") and step["count"]:
                    now = subgoals.measure(verifying(step), fresh)[1] if before is not None else None
                    if (step["verifies"] == "unknown" and uses >= step["count"]) or (now is not None and abs(now - before) >= step["count"]):
                        finish()
                        continue
                step_context = {**context, "observation": fresh}
                if step.get("targets") or step["control"] == "select":
                    action, why = repeat.next_action(step, step_context, state)
                else:
                    action = repeat.ordinary(step, step_context)
                    why = "use" if action else "not_offered"
                if action is None and ((state["select"] == "held" and not step.get("targets")) or (why == "done" and step.get("targets"))):
                    finish()  # A select of the item already held needs no press; a repeat with no target left is done.
                    continue
                if action is None:
                    return None, "not_offered"
                if step.get("targets"):
                    uses += why == "use"
                elif why == "use" or step["control"] == "select":
                    finish()
                return action, why
            return None, "done"
        return next_action

    def _saved_skills(self, project_id, goal, observation):
        """The project's usable saved skills for the goal's completion shape that can be measured here, as (key, name, bound steps)."""
        for skill_key, skill in skills.find(self.store, project_id, goal["completion"]) if project_id else []:
            # Steps rebuilt from dispatched actions verify nothing (unknown), so the goal's own fact must be readable instead.
            first = skill["steps"][0]["verifies"]
            check = goal["completion"] if first in (goal["completion"]["fact"], "unknown") else {"fact": first, "args": []}
            if subgoals.measure({**check, "op": ">=", "value": 0}, observation)[1] is not None:
                yield skill_key, skill["name"], skills.bind(skill["steps"], goal["completion"], replay=True, measured=goal.get("measured"))

    async def _replay(self, run_id, player_id, generation, current, context, observation, goal, timing):
        """Replay cached steps in code, with no model call: the leaf's steps from goal["step"] (a saved skill for its shape
        when the planner gave none), then the decision cache for this situation. A source in goal["missed"], or one that
        cannot start here or whose first action is not offered, is skipped. A source that misses is tallied failed and
        joins goal["missed"].

        Returns (stop, ran, source key), or (None, [], None) when nothing was replayed."""
        key = (run_id, player_id)
        run = self.store.require("runs", run_id)
        plan, project_id = run["subgoal_plan"], run.get("project_id")
        leaf = is_leaf(plan, goal)
        sig = skills.signature(goal["completion"], observation, self.held.get(key, ""))

        def sources():
            """(kind, key, name, steps, start) in order."""
            if leaf and goal.get("steps"):
                yield ("skill" if goal.get("skill_key") else "steps"), goal.get("skill_key") or "steps", goal.get("skill") or goal["objective"], goal["steps"], goal.get("step", 0)
            elif leaf:
                for skill_key, skill_name, steps in self._saved_skills(project_id, goal, observation):
                    yield "skill", skill_key, skill_name, steps, 0
            record = skills.find_decision(self.store, project_id, sig) if project_id else None
            if record:
                yield "decision", skills.decision_key(project_id, sig), record["name"], skills.bind(record["steps"], goal["completion"], replay=True), 0

        here = {**context, "observation": observation}
        for kind, source, name, steps, start in sources():
            if source in (goal.get("missed") or []):
                continue
            probe = self._step_replayer(key, context, steps, goal["completion"], {"index": start, "tried": set()})
            why = ("finished" if start >= len(steps) else repeat.gate(steps[start:], here)
                   or ("not_offered" if probe(observation)[0] is None else None))
            if not why:
                break
            self.store.event(run_id, "replay_skipped", {"player_id": player_id, "goal_id": goal["id"], "key": source, "why": why})
        else:
            return None, [], None
        cursor = {"index": start, "tried": set()}
        stop, ran = await self._run_batch(run_id, player_id, generation, current, context, observation, observation, None,
                                          self._step_replayer(key, context, steps, goal["completion"], cursor), timing, REPEAT_LIMIT, cursor["tried"])
        timing["counts"]["repeat_actions"] = timing["counts"].get("repeat_actions", 0) + len(ran)
        replayed = {"steps": "repeat_stopped", "skill": "skill_replayed", "decision": "decision_replayed"}[kind]
        self.store.event(run_id, replayed, {"player_id": player_id, "goal_id": goal["id"], "key": source, "name": name,
                                            "control": steps[min(start, len(steps) - 1)]["control"], "reason": stop, "actions": len(ran)})
        live, active = self._active(run_id, player_id)
        if active and active["id"] == goal["id"]:
            if kind != "decision":
                active.update(steps=steps, step=cursor["index"])
            if kind == "skill":
                active.update(skill=name, skill_key=source)
            if stop in REPLAY_MISSES:
                active["missed"] = (active.get("missed") or []) + [source]
            self.store.put("runs", live)
        if stop in REPLAY_MISSES and kind != "steps":
            self.store.event(run_id, f"{kind}_failed", {"player_id": player_id, "goal_id": goal["id"], "key": source, "reason": stop})
            build_id = run["build"].get("id")
            record = (skills.save_decision(self.store, project_id, sig, goal["completion"], None, False, build_id=build_id, run_id=run_id)
                      if kind == "decision" else
                      skills.save(self.store, project_id, goal["completion"], None, False, build_id=build_id, run_id=run_id, key=source))
            if record["last_result"]["regression"]:
                await self.report_skill_regression(run_id, player_id, record, "decision" if kind == "decision" else "subgoal", stop)
        return stop, ran, source

    def _account(self, run_id, player_id, goal_id, decided_on, route, stop, actions):
        """Count a turn that dispatched or waited: progress resets the leaf's attempts, else it is one more. True on progress.

        Progress is a goal-path fact moving toward its target or, while the player cannot act (menus, character creation,
        cutscenes, where no fact moves), reaching a screen this leaf has not seen, up to MAX_SCREENS; a seen screen is no
        progress, so going back and forth between screens still spends the budget. A failed or no_effect stop keeps the last action out of the
        leaf's decider menus. goal["waited"] tells the next turn that this one waited, so a growth leaf keeps waiting in code."""
        run, goal = self._active(run_id, player_id)
        if not goal or goal["id"] != goal_id or not (actions or stop == "wait"):
            return False
        goal["waited"] = stop == "wait"
        latest = self.store.get("observations", f"{run_id}:{player_id}") or decided_on
        progressed = any(subgoals.toward(check, subgoals.measure(check, decided_on)[1], subgoals.measure(check, latest)[1])
                         for check in (g["completion"] for g in subgoals.goal_path(run["subgoal_plan"])))
        if not progressed and not subgoals.playable(latest):
            seen = goal.setdefault("screens", [])
            before, after = screen_id(decided_on), screen_id(latest)
            if before not in seen:
                seen.append(before)
            progressed = after not in seen and len(seen) < MAX_SCREENS
            if progressed:
                seen.append(after)
        if progressed:
            subgoals.credit_progress(goal, time.time())
        elif stop in ("failed", "no_effect") and actions and not is_select_input(actions[-1]):
            goal["failed"] = ((goal.get("failed") or []) + [option_key(actions[-1])])[-FAILED_KEPT:]
        attempts = None if progressed else subgoals.record_attempt(run["subgoal_plan"])
        self.store.put("runs", run)
        if attempts:
            self.store.event(run_id, "subgoal_attempt", {"player_id": player_id, "goal_id": goal_id, "route": route, "stop": stop, "attempts": attempts})
        return progressed

    async def _jev_decide(self, run_id, player_id, generation, context, observation, goal, dispatch, current, routing, timing):
        """Jev decides the turn: its action is dispatched, or a fallback leaves the turn to Sonnet.

        Fills routing. Returns (result, stop): Jev's dispatched result with stop None, (None, "cancelled") when the turn was
        cancelled, or (None, None) for a Sonnet fallback."""
        with timed(timing, "decide"):
            res = await self.coordinator.ai.jev_turn(context, dispatch, current, run_id)
        if res is None:
            return None, "cancelled"
        routing.update(menu=res["menu"], confidence=res["confidence"], jev_ms=res["latency_ms"])
        if res["status"] == "fallback":
            routing["fallback"] = res["reason"]
            return None, None
        routing.update(route="jev", pick=res["pick"], model=res["model"])
        return res, None

    async def _decide(self, run_id, player_id, generation, current, run, context, observation, goal, routing, timing):
        """Route one turn: the cache while the leaf has no attempts, then Jev while attempts < BUDGET // 2, else Sonnet.
        A Jev fallback (low confidence, error, or no TypeSafe key) or an operator instruction sends the turn to Sonnet.

        Fills routing (route, model, latency_ms, Jev's fields and the batch to learn). Returns (result, first action, stop, actions run after it)."""
        key = (run_id, player_id)
        if goal and not goal.get("attempts"):
            with timed(timing, "act"):
                stop, ran, _ = await self._replay(run_id, player_id, generation, current, context, observation, goal, timing)
            if ran:
                routing["route"] = "cache"
                return None, {}, stop, ran
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
        # Keyed by the observation and held item the model decides on, before its first action changes them.
        sig = skills.signature(goal["completion"], observation, self.held.get(key, "")) if goal else None
        result, stop, ran = None, None, []
        if goal:
            if context.get("operator_instruction"):
                routing["fallback"] = "operator_instruction"
            elif goal.get("attempts", 0) >= subgoals.BUDGET // 2:
                routing["fallback"] = "attempts"
            else:
                self.status(run_id, player_id, "thinking", "Waiting for Jev to choose the next action.", generation)
                result, stop = await self._jev_decide(run_id, player_id, generation, context, observation, goal, dispatch, current, routing, timing)
        if routing["route"] == "jev":
            routing["latency_ms"] = routing["jev_ms"]
        elif stop is None:
            label = PROVIDERS[provider_of(run.get("model") or run["plan"].get("model", MODEL))]["label"]
            self.status(run_id, player_id, "thinking", f"Waiting for {label} to choose the next action.", generation)
            with timed(timing, "decide"):
                result = await self.coordinator.ai.batch_turn(context, dispatch, current, run_id)
            if result:
                routing.update(route=result["model"], model=result["model"], latency_ms=routing["jev_ms"] + result["latency_ms"])
        first = (result or {}).get("action") or {}
        if result is None:
            stop = stop or "cancelled"
        elif first.get("tool") == "wait_for_change":
            stop = "wait"
        elif first.get("tool") != "perform_action":
            stop = "done"  # A game bug report: nothing was dispatched.
        else:
            with timed(timing, "act"):
                stop, ran = await self._run_batch(run_id, player_id, generation, current, context, observation, fresh,
                                                  result["result"], queued_actions(context, result["queued"]), timing)
            if run.get("project_id") and goal and stop in ("done", "goal_met", "scene_changed", "limit"):
                # Jev's or the model's batch may become this situation's decision skill: non-move actions only, read against the
                # observation it was chosen on. turn() saves it once _account shows the turn made progress.
                steps = [s for s in (repeat.step_for(a["args"], observation) for a in [first] + ran) if s["control"] != "move_to"]
                if steps:
                    routing["learned"] = (sig, steps)
        return result, first, stop, ran

    def _turn_context(self, run, player_id, observation, goal, signature, timing):
        """The decision context for this turn, logged without the manual: the leaf's failed actions are kept out of the menus
        and the deciders see only its unfinished steps."""
        key = (run["id"], player_id)
        player = next(p for p in run["plan"]["players"] if p["id"] == player_id)
        with timed(timing, "decision_context"):
            context = self.decision_context(run, player, observation)
            context["bug_already_reported"] = self.bug_reports.get(key) == signature
            if goal:
                context["failed"] = goal.get("failed") or []
                if goal.get("step") and context.get("current_goal", {}).get("steps"):
                    context["current_goal"]["steps"] = goal["steps"][goal["step"]:]
            # The manual is the same every turn; log its size and hash, not its text.
            controls = context.get("game_controls") or ""
            self.store.event(run["id"], "structured_decision_context", {"player_id": player_id,
                "context": {k: v for k, v in context.items() if k != "game_controls"},
                "game_controls_bytes": len(controls.encode()), "game_controls_sha": hashlib.sha256(controls.encode()).hexdigest()[:12],
                "observation_ref": subgoals.observation_ref(observation), "source_revision": run["build"].get("source_revision")})
        timing["bytes"].update(prompt_observation=json_bytes(context["observation"]), decision_context=json_bytes(context))
        timing["context_truncated"] = bool(context["observation"].get("context_truncated"))
        return context

    async def turn(self, run_id, player_id, tick_seconds=None):
        """One tick of the subgoal state machine: observe, verify the plan, then wait in code, replay or decide, and count the attempt."""
        key = (run_id, player_id)
        run, ps = self.binding(run_id, player_id)
        generation = ps["generation"]
        if self.generations.get(key) != generation:
            if ps.get("pending_action"):
                self.clear_pending(run_id, player_id, generation)  # Held/rebound input cannot still be pending.
            self.pending.pop(key, None)
            self._forget_turn(key)
            self.turn_ended_at.pop(key, None)
            self.bug_reports.pop(key, None)
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
            if key not in self.pending and key not in self.waits:
                self.status(run_id, player_id, "observing", "Reading fresh game state.", generation)
            stored = None if key in self.pending else self.store.get("observations", f"{run_id}:{player_id}")
            if stored and time.time() - stored["received_at"] < REUSE_OBSERVATION_SECONDS:
                observation = stored
                timing["counts"]["observe_reused"] = 1
            else:
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
            # After a planner call this is the post-planning observation.
            observation = planned["observation"]
            signature = meaningful_signature(observation)
            wait = self.waits.get(key)
            if wait and wait[2] == generation and time.time() < wait[0] and wait_signature(observation) == wait[1]:
                self.status(run_id, player_id, "waiting", previous_status.get("detail") or ps.get("activity") or "Waiting for game state to change.", generation, wait_until=wait[0])
                timing["outcome"] = "waiting"
                return timing["outcome"]
            self.waits.pop(key, None)
            run, ps = self.binding(run_id, player_id)
            plan = run.get("subgoal_plan")
            goal = subgoals.active_goal(plan) if plan and plan["player_id"] == player_id else None
            decision_started_at = time.time()
            routing = {"route": None, "model": None, "latency_ms": 0, "pick": None, "confidence": None, "fallback": None, "jev_ms": 0, "menu": {},
                       "learned": None}
            if (goal and goal.get("waited") and goal["completion"].get("fact") in TIMER_FACTS
                    and not subgoals.measure(goal["completion"], observation)[0]):
                # After a wait on a growth fact, keep waiting in code instead of asking a decider again; it counts like any wait.
                await self.dispatch(run_id, player_id, "wait_for_change", {"seconds": CODE_WAIT_STEP, "reason": "Waiting for crops to grow."},
                                    observation, generation)
                result, first, stop, ran, attempt_route = None, {}, "wait", [], "wait"
            else:
                context = self._turn_context(run, player_id, observation, goal, signature, timing)
                result, first, stop, ran = await self._decide(run_id, player_id, generation, current, run, context, observation, goal, routing, timing)
                attempt_route = routing["route"]
            dispatched = ([first] if first.get("tool") == "perform_action" else []) + ran
            if goal and self._account(run_id, player_id, goal["id"], observation, attempt_route, stop, dispatched) and routing["learned"]:
                # Only a batch that made progress is saved as this situation's decision skill.
                sig, steps = routing["learned"]
                saved = skills.save_decision(self.store, run["project_id"], sig, goal["completion"], steps, True,
                                             build_id=run["build"].get("id"), run_id=run_id)
                self.store.event(run_id, "decision_saved", {"player_id": player_id, "goal_id": goal["id"],
                                                            "key": skills.decision_key(run["project_id"], sig), "name": saved["name"]})
            timing["outcome"] = "decided"
            timing["counts"]["batch_actions"] = len(dispatched)
            timing["batch_stop"] = stop
            if result:
                timing["bytes"].update(result["bytes"])
            if routing["route"]:
                # Every route carries part 1's keys; a Jev route adds pick, a Sonnet route after Jev adds fallback, and both add the menu Jev was asked.
                extra = {**{k: routing[k] for k in ("pick", "fallback") if routing[k]}, **routing["menu"]}
                self.store.event(run_id, "decision_routed", {"player_id": player_id, "route": routing["route"], "latency_ms": routing["latency_ms"],
                                                             "confidence": routing["confidence"], "model": routing["model"], "actions": len(dispatched), **extra})
            self.store.event(run_id, "batch_stopped", {"player_id": player_id, "reason": stop, "actions": len(dispatched)})
            self.store.event(run_id, "agent_turn_completed", {"player_id": player_id, "duration_seconds": round(time.time() - decision_started_at, 3), "result": result})
            if current():
                latest = self.store.require("runs", run_id)
                status = latest["players"][player_id].get("agent_status") or {}
                status["completed_at"] = time.time()
                # A completed turn means an earlier error (e.g. a startup-window bridge 503) is no longer current.
                status.pop("last_error", None)
                status.pop("error_at", None)
                latest["players"][player_id]["agent_status"] = status
                self.store.put("runs", latest)
                if status.get("phase") in {"thinking", "tool"}:
                    self.status(run_id, player_id, "ready", "Decision finished; checking the next observation.", generation, completed_at=time.time())
                if isinstance(result, dict) and result.get("status") == "bug_reported":
                    with timed(timing, "issue_reports"):
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
                    if await self._break(run_id, player_id, generation, observation,
                                         "No supported direct action fits. Decompose missing prerequisites or explain the blocker."):
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
