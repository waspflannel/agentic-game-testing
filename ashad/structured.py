"""Shared structured-play handlers for Decisions, dashboard inspection and MCP."""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import time
from datetime import datetime, timezone

from .ai import GAME_VERBS, ProviderError, capability_summary
from .game_controls import run_configuration
from .models import MODEL, PROVIDERS, provider_of
from .repository import Repository
from .store import uid
from . import repair_tasks, subgoals

LOG = logging.getLogger(__name__)
LOOP_BACKSTOP_DECISIONS = 6
ISSUE_PREFIXES = {"unsupported_action": "uae_", "crash": "crash_", "game_bug": "bug_"}


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


def observed_entity(observation, target_id):
    return next((e for e in observation.get("entities", []) if target_id and e.get("id") == target_id), {})


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
        self.bug_reports = {}
        self.crashes_recorded = set()

    def _forget_turn(self, key):
        self.waits.pop(key, None)
        self.last_decisions.pop(key, None)

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
        return await self._perform_action(run_id, player_id, args, observation, generation, owner)

    async def _perform_action(self, run_id, player_id, args, observation, generation, owner):
        run, ps = self.binding(run_id, player_id)
        key = (run_id, player_id)
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
                      "error": f"{type(exc).__name__}: {exc}", "error_code": getattr(exc, "code", None),
                      "http_status": getattr(exc, "status_code", None)}
        outcome_event = self.store.event(run_id, "action_result", {"player_id": player_id, "action_id": action_id,
            "intent_event_id": event["id"], "result": result, "mode": "structured", "retries": 0})
        if result.get("status") == "rejected" and result.get("http_status") not in {None, 409}:
            await self.report_unsupported(run_id, player_id, "worker_rejected", {"tool": "perform_action",
                "verb": payload["verb"], "target_id": payload["target_id"] or None, "parameters": payload["parameters"],
                "controller": "agent" if owner == "agent" else "mcp"}, observation,
                outcome=result, event_ids=(outcome_event["id"],))
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
            "player_id": player_id, "scenario_id": run.get("scenario_id"),
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

    async def _record(self, run, ps, player_id, issue_type, basis, source, observation, sections, *, event_ids=(), skip_pin=False):
        """Write one issue example of any type; returns the outcome, or None when recording failed."""
        try:
            run_id = run["id"]
            room = repair_tasks.issue_room(self.store, issue_type, basis)
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
                    "issue_id": repair_tasks.issue_id(issue_type, basis), "example_id": example_id,
                    "source": source, "kept": outcome == "kept", "outcome": outcome})
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
            await self._record(run, ps, player_id, "crash", basis, "monitor", ps.get("structured_context") or {},
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
            completed = (result_event and (result_event["payload"].get("result") or {}).get("status") == "completed") or any(
                e["kind"] == "structured_action_outcome" and (e["payload"].get("result") or {}).get("action_id") == action_id
                and e["payload"]["result"].get("status") == "completed" for e in recent)
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

    def forget_run(self, run_id):
        for counters in (self.loop_counts, self.bug_reports):
            for key in [k for k in counters if k[0] == run_id]:
                del counters[key]
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

    def decision_context(self, run, player, observation):
        ps = run["players"][player["id"]]
        recent = self.store.events(run["id"], after=max(0, self.store.count_events(run["id"])-120), limit=120)
        own = [e for e in recent if e["payload"].get("player_id") == player["id"] and e["kind"] in
               {"decision_selected", "structured_action_intent", "action_result", "structured_action_outcome", "goal_changed"}][-20:]
        team = [e for e in recent if e["kind"] == "team_message"][-12:] if player.get("cooperation") == "team" else []
        configuration_id, snapshot = run_configuration(run)
        context = {"play_mode": run["plan"]["play_mode"], "player": player, "goal": player["goal"],
            "game_controls": (run.get("game_controls") or {}).get("controls") or (snapshot or {}).get("instructions", ""), "configuration_id": configuration_id,
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
            self._forget_turn((run_id, player_id))
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
        self._forget_turn((run_id, player_id))
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
        self._forget_turn((run_id, player_id))
        return True

    async def _reconcile_pending(self, run_id, player_id, ps, generation):
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
        self.store.event(run_id, "structured_action_outcome", {"player_id": player_id, "result": result})
        if result.get("status", "unknown") == "unknown":
            self.status(run_id, player_id, "error", "The last action outcome is unknown; input is held for review.", generation, action_id=self.pending[key])
            await self.coordinator.investigate(run_id, "unknown_execution", "Structured action acknowledgement could not be reconciled; fresh review required", [player_id])
            return True
        self.clear_pending(run_id, player_id, generation)
        self.last_decisions.pop(key, None)
        if result.get("status") in {"failed", "rejected", "expired", "cancelled"}:
            self.request_replan(run_id, player_id, generation, "The last action failed; reassess prerequisites and approach.")
        return False

    async def turn(self, run_id, player_id):
        key = (run_id, player_id)
        run, ps = self.binding(run_id, player_id)
        generation = ps["generation"]
        if self.generations.get(key) != generation:
            if ps.get("pending_action"):
                self.clear_pending(run_id, player_id, generation)  # Held/rebound input cannot still be pending.
            self.pending.pop(key, None)
            self._forget_turn(key)
            self.loop_counts.pop(key, None)
            self.bug_reports.pop(key, None)
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
            if key in self.pending and await self._reconcile_pending(run_id, player_id, ps, generation):
                return
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
            context["bug_already_reported"] = self.bug_reports.get(key) == signature
            self.store.event(run_id, "structured_decision_context", {"player_id": player_id, "context": context,
                "full_observation": observation, "source_revision": run["build"].get("source_revision")})
            self.last_decisions[key] = (signature, time.time(), generation)
            label = PROVIDERS[provider_of(run.get("model") or run["plan"].get("model", MODEL))]["label"]
            self.status(run_id, player_id, "thinking", f"Waiting for {label} to choose the next action.", generation)
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
            result = await self.coordinator.ai.player_turn(context, dispatch, current, run_id)
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
                await self._loop_backstop(run_id, player_id, generation, signature, observation, result)
                if isinstance(result, dict) and result.get("status") == "bug_reported":
                    self.bug_reports[key] = signature
                    await self.report_bug(run_id, player_id, result.get("report") or {}, observation)
        except ProviderError as exc:
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
