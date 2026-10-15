"""Instance-owned backward planning. Proposals are accepted unless they contradict observed state; only measure() verifies success against fact_catalog values."""
from __future__ import annotations

import copy
import json
import math
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from . import facts

# ponytail: retain at most 64 goals; archive verified branches if live objectives outgrow this ceiling.
MAX_GOALS = 64
MAX_DEPTH = 8
STUCK_ACTIONS = 6
STUCK_PLANS = 2
STUCK_REPEATS = 2
MAX_OBSERVATION_AGE = 5


def stale(stamp, now=None):
    """One freshness rule. Callers pass controller receipt time when they have it; game clocks may be skewed."""
    return type(stamp) not in (int, float) or not math.isfinite(stamp) or (now or time.time()) - stamp > MAX_OBSERVATION_AGE


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Arg(Contract):
    name: str = Field(min_length=1, max_length=40)
    value: str | int


class Check(Contract):
    fact: str = Field(min_length=1, max_length=60, description="A fact_catalog name, or unknown")
    args: list[Arg] = Field(max_length=3)
    op: Literal[">=", "==", "<="]
    value: int | bool = Field(description="Target; bool facts use ==")
    description: str = Field(min_length=1, max_length=600)


class ProposedGoal(Contract):
    key: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,50}$")
    parent_key: str = Field(max_length=50, description="Empty attaches to focus; otherwise an earlier proposed key.")
    objective: str = Field(min_length=1, max_length=600)
    completion: Check
    actionable: bool = Field(description="True only for a bounded achievable goal with currently available ordinary controls; false means decompose later.")


class Step(Contract):
    control: str = Field(min_length=1, max_length=40, description="A macro name, an advertised input name or a game verb")
    item: str = Field(max_length=60, description="The hotbar item_id the step needs; empty if none")
    count: int = Field(ge=0, le=99, description="Repeats; 0 means until the check is met")
    precondition: str = Field(max_length=200, description="The manual's precondition text or a catalog fact")
    verifies: str = Field(min_length=1, max_length=60, description="A fact_catalog name, or unknown")


class PlanningDecision(Contract):
    goal_id: str = Field(min_length=1, max_length=100)
    mode: Literal["expand", "act", "blocked", "retire", "complete"]
    completion: Check
    reason: str = Field(min_length=1, max_length=1000)
    subgoals: list[ProposedGoal] = Field(max_length=12)
    first_actions: list[str] = Field(max_length=5, description="Option IDs T.A from action_options for the new active leaf, in order; [] when none fit or no action_options were given.")
    steps: list[Step] = Field(max_length=8, description="The act leaf's ordered steps; [] when not act.")

    @model_validator(mode="after")
    def check_expansion(self):
        if bool(self.subgoals) != (self.mode == "expand"):
            raise ValueError("Only expand supplies nonempty subgoals")
        seen = set()
        for goal in self.subgoals:
            if goal.key in seen or (goal.parent_key and goal.parent_key not in seen):
                raise ValueError("Subgoals must be unique and parent-before-child; cycles are forbidden")
            seen.add(goal.key)
        parents = {g.parent_key for g in self.subgoals}
        if any(g.actionable and g.key in parents for g in self.subgoals):
            raise ValueError("A goal with unfinished prerequisites is not actionable")
        return self


def unknown(description="Success is not yet grounded in an observable game condition."):
    return Check(fact="unknown", args=[], op=">=", value=1, description=description).model_dump()


def new_plan(primary_goal, player_id, run_id=None):
    return {"version": 1, "primary_goal": primary_goal, "player_id": player_id, "run_id": run_id, "root_id": "root", "revision": 0,
            "active_goal_id": "root", "status": "pending", "reason": "Waiting for a fresh game observation.",
            "world_id": None, "observation_generation": None, "goals": [{
                "id": "root", "parent_id": None, "objective": primary_goal,
                "completion": unknown("Primary-objective acceptance is manual; verify concrete outcomes in its child goals."),
                "status": "pending", "actionable": False, "attempts": 0, "reviews": 0}]}


def goal_path(plan, goal_id=None):
    nodes = {g["id"]: g for g in plan["goals"]}
    path, current = [], nodes.get(goal_id or plan.get("active_goal_id"))
    while current and len(path) <= MAX_DEPTH:
        path.append(current)
        current = nodes.get(current.get("parent_id"))
    return list(reversed(path))


def active_goal(plan):
    return next((g for g in plan["goals"] if g["id"] == plan.get("active_goal_id")), None)


def completion_claim(plan):
    """The planner's waiting or last claim that the primary objective is reached, kept on the root."""
    return next(g for g in plan["goals"] if g["id"] == plan["root_id"]).get("completion_claim")


def held_counts(observation):
    """{item_id: count} over inventory and hotbar, ignoring malformed counts and items without a string item_id; None when either list is missing."""
    player = observation.get("state", {}).get("player", {})
    inventory, hotbar = player.get("inventory"), player.get("hotbar")
    if not isinstance(inventory, list) or not isinstance(hotbar, list):
        return None
    counts = {}
    for item in inventory + hotbar:
        count, item_id = item.get("count"), item.get("item_id")
        if isinstance(item_id, str) and type(count) in (int, float) and math.isfinite(count) and count >= 0:
            counts[item_id] = counts.get(item_id, 0) + count
    return counts


def toward(check, old, new):
    """True when a measured value moved closer to the check's target."""
    if old is None or new is None or old == new:
        return False
    target, old, new = int(check["value"]), int(old), int(new)
    return {">=": new > old, "<=": new < old, "==": abs(new - target) < abs(old - target)}[check["op"]]


def credit_progress(goal, now):
    """Progress clears the goal's no-progress counters and repeat tracking."""
    goal.update(attempts=0, reviews=0, progress_at=now)
    for key in ("recent_actions", "repeating", "repeats"):
        goal.pop(key, None)


def measure(check, observation):
    """Return (satisfied, measured value). Missing facts never count as success."""
    fact = check.get("fact", "unknown")
    if fact == "unknown":
        return False, None
    # Inventory, quests and farm facts may be stale during load/disconnect.
    if fact not in ("world_ready", "player_can_act") and observation.get("ready") is not True:
        return False, None
    value = facts.read(facts.values(observation), fact, facts.args_of(check))
    return (False, None) if value is None else (facts.met(check, value), value)


def observation_ref(observation):
    return {k: observation.get(k) for k in ("observation_id", "timestamp", "world_id", "generation")}


def planning_scene(observation):
    """A plan inferred for a transient screen must not land after that screen changes."""
    controls = frozenset(e.get("id") for e in observation.get("entities", []) if e.get("kind") == "ui" and e.get("actions"))
    return observation.get("world_id"), observation.get("generation"), observation.get("ready"), controls


def knowledge_signature(observation):
    """New quests matter; leaving a station's view must not repeatedly replan."""
    state = observation.get("state", {})
    return sorted({q.get("id") for q in state.get("objectives", [])} | set(state.get("completed_quests", [])))


def select_active(plan):
    nodes = {g["id"]: g for g in plan["goals"]}
    for goal in nodes.values():
        if goal["status"] == "active":
            goal["status"] = "pending"
    def descend(goal):
        if goal["status"] in {"completed", "gave_up"}:
            return None
        if goal["status"] == "blocked":
            return goal
        children = [g for g in nodes.values() if g.get("parent_id") == goal["id"] and not g.get("retired")]
        for child in children:
            candidate = descend(child)
            if candidate:
                return candidate
        return goal
    selected = descend(nodes[plan["root_id"]])  # The manual-acceptance root never completes.
    plan["active_goal_id"] = selected["id"]
    if selected["status"] == "blocked":
        plan.update(status="blocked", reason=selected.get("blocked_reason", "This goal needs help."))
    else:
        selected["status"] = "active"
        plan["status"] = "active"
    return selected


def next_leaf(plan):
    """A copy of the plan as reconcile would leave it once the active goal completes, or None when the copy's
    next goal is the root or blocked. The input plan is not changed."""
    ahead = copy.deepcopy(plan)
    active_goal(ahead)["status"] = "completed"
    ahead["revision"] += 1  # reconcile counts the completion as a revision.
    selected = select_active(ahead)
    if selected["id"] == ahead["root_id"] or selected["status"] == "blocked":
        return None
    selected["actionable"] = False  # reconcile makes a newly active goal plan again.
    return ahead


def reconcile(plan, observation, control_generation):
    """Refresh verified progress without inference, retaining history across pause/restart."""
    now = time.time()
    if stale(observation.get("received_at", observation.get("timestamp")), now):
        raise ValueError("Planner requires a fresh observation")
    if (not observation.get("observation_id") or observation.get("player_id") != plan["player_id"]
            or (plan.get("run_id") and observation.get("run_id") != plan["run_id"])):
        raise ValueError("Planner observation identity mismatch")
    world = observation.get("world_id", "")
    binding = observation.get("generation", "")
    previous_world = plan.get("world_id")
    if previous_world not in (None, "") and world and previous_world != world:
        # Same instance, different world: old achievements cannot prove anything here.
        fresh = new_plan(plan["primary_goal"], plan["player_id"], plan.get("run_id"))
        fresh["revision"] = plan["revision"] + 1
        plan.clear()
        plan.update(fresh)
    rebound = plan.get("observation_generation") not in (None, binding)
    resumed = plan.get("control_generation") not in (None, control_generation)
    if rebound or resumed:
        for goal in plan["goals"]:
            if goal["status"] not in {"completed", "gave_up"} and not goal.get("retired"):
                goal.update(actionable=False, status="pending", attempts=0, reviews=0)
                goal.pop("blocked_reason", None)
                goal.pop("measured", None)
                goal.pop("recent_actions", None)
                goal.pop("repeating", None)
                goal.pop("repeats", None)
        plan["reason"] = "Control or game binding changed; reassess from fresh observations."
    # Unloading emits an empty world ID. Keep the last actual world so A -> menu -> B cannot reuse A's proof.
    plan.update(world_id=world or previous_world, observation_generation=binding, control_generation=control_generation)
    completed = []
    progressed = set()
    for goal in plan["goals"]:
        if goal["status"] == "completed" or goal.get("retired"):
            continue
        done, value = measure(goal["completion"], observation)
        if toward(goal["completion"], goal.get("measured"), value):
            progressed.add(goal["id"])
            credit_progress(goal, now)
        if value is not None:  # A loading screen reads nothing; keep the last value so progress is not erased.
            goal["measured"] = value
        if done:
            goal.update(status="completed", completion_evidence={**observation_ref(observation), "value": value})
            goal.pop("blocked_reason", None)
            completed.append(goal["id"])
    # Progress on a parent's fact (e.g. harvesting toward "10 wheat") is progress for the leaf pursuing it.
    current = active_goal(plan)
    if current and any(g["id"] in progressed for g in goal_path(plan, current["id"])[:-1]):
        credit_progress(current, now)
    for goal_id in completed:
        for ancestor in goal_path(plan, goal_id)[:-1]:
            credit_progress(ancestor, now)
    for goal in plan["goals"]:
        if goal["status"] not in {"completed", "gave_up"} and any(g["status"] == "completed" for g in goal_path(plan, goal["id"])[:-1]):
            goal.update(retired=True, status="blocked", blocked_reason="No longer needed: the parent goal was verified directly.")
    previous_active = plan.get("active_goal_id")
    # A claim or hand-back waits for the operator; re-selecting would move the active goal away from the root.
    waiting = plan["status"] == "goal_reached" or (plan["status"] == "handed_back" and not (rebound or resumed))
    selected = active_goal(plan) if waiting else select_active(plan)
    if selected and selected["id"] != previous_active:
        selected["activated_at"] = now
        # A sibling may have lost its prerequisites while another goal was being pursued.
        selected["actionable"] = False
    # Loading screens are not new knowledge; new knowledge replans without resetting the no-progress cap.
    if observation.get("ready") is True:
        signature = knowledge_signature(observation)
        if selected and plan.get("knowledge_signature") not in (None, signature):
            selected["actionable"] = False
            plan["reason"] = "Observed objectives changed; reassess the active goal."
        plan["knowledge_signature"] = signature
    plan["last_observation"] = observation_ref(observation)
    if completed:
        plan["revision"] += 1
    return completed


def needs_planning(plan):
    goal = active_goal(plan)
    if not goal or plan["status"] == "blocked":
        return False
    if goal.get("repeating"):
        goal["actionable"] = False
        plan["reason"] = f"Repeating the same action without progress ({goal['repeating']}); choose a different target or approach, or report the blocker."
    return not goal.get("actionable")


def stuck(plan, needs_plan) -> str | None:
    goal = active_goal(plan)
    if not goal or plan["status"] != "active":
        return None
    if goal.get("attempts", 0) >= STUCK_ACTIONS:
        return f"{STUCK_ACTIONS} actions without measured progress"
    if goal.get("repeats", 0) >= STUCK_REPEATS:
        return "The same action kept repeating without progress after a replan"
    if needs_plan and goal.get("reviews", 0) >= STUCK_PLANS:
        return f"{STUCK_PLANS} plans without measured progress"
    return None


def block(plan, reason):
    goal = active_goal(plan)
    if goal:
        goal.update(status="blocked", actionable=False, blocked_reason=reason)
    plan.update(status="blocked", reason=reason, revision=plan["revision"] + 1)


def give_up(plan, goal_id, reason):
    if goal_id != plan["root_id"]:
        given = {goal_id}
        for goal in plan["goals"]:
            if goal.get("parent_id") in given:
                given.add(goal["id"])
            if goal["id"] in given and goal["status"] != "completed" and not goal.get("retired"):
                goal.update(status="gave_up", actionable=False, blocked_reason=reason)
    selected = select_active(plan)
    if selected["id"] == plan["root_id"]:
        plan.update(status="handed_back", reason=reason)
    else:
        plan["reason"] = reason
    plan["revision"] += 1


def reopen_after_rejected_claim(plan, note):
    """The operator said not done: the root plans again and must verify more before claiming again."""
    root = next(g for g in plan["goals"] if g["id"] == plan["root_id"])
    claim = root.pop("completion_claim")
    plan["completion_rejected"] = {"note": note, "claim_reason": claim["reason"], "verified_count": len(claim["verified_goal_ids"])}
    root["actionable"] = False
    plan.update(status="active", reason="You said the primary goal is not done" + (f": {note}" if note else "."),
                revision=plan["revision"] + 1)


def retire(plan, focus, decision, context):
    """Retirement is not success: the goal and its descendants keep their predicates and leave the active path."""
    retired = {focus["id"]}
    for goal in plan["goals"]:
        if goal.get("parent_id") in retired:
            retired.add(goal["id"])
        if goal["id"] in retired and goal["status"] != "completed":
            goal.update(retired=True, status="blocked", actionable=False, blocked_reason=decision["reason"])
    selected = select_active(plan)
    if selected:
        selected["actionable"] = False


def apply_decision(plan, raw, context, warnings=None):
    """Validate fully before editing a copy; callers commit only while their run is current."""
    warnings = [] if warnings is None else warnings
    if isinstance(raw, dict):
        raw = copy.deepcopy(raw)
        raw["first_actions"] = []  # The controller's optional first batch; never a reason to reject a plan.
        # Steps are optional guidance too: a malformed step or one verified by a fact outside the catalog is dropped, never rejected.
        steps, raw["steps"] = raw.get("steps"), []
        for index, step in enumerate(steps if isinstance(steps, list) else []):
            try:
                step = Step.model_validate(step).model_dump()
            except ValidationError:
                warnings.append(f"steps.{index} dropped: not a valid step")
                continue
            if step["verifies"] != "unknown" and step["verifies"] not in context["fact_catalog"]["facts"]:
                warnings.append(f"steps.{index} dropped: verifies {step['verifies']!r} is not in fact_catalog")
                continue
            if len(raw["steps"]) < 8:
                raw["steps"].append(step)
        mode = raw.get("mode")
        if mode == "expand" and not raw.get("subgoals") and raw.get("goal_id") != plan["root_id"]:
            raw["mode"] = "act"
            warnings.append("expand without subgoals applied as act")
        if mode == "act" and raw.get("subgoals"):
            raw["mode"] = "expand"
            warnings.append("act with subgoals applied as expand")
        if mode in {"blocked", "retire", "complete"} and raw.get("subgoals"):
            raw["subgoals"] = []
            warnings.append(f"{mode} subgoals dropped")
    decision = PlanningDecision.model_validate(raw).model_dump()
    result = copy.deepcopy(plan)
    focus = active_goal(result)
    if not focus or decision["goal_id"] != focus["id"]:
        raise ValueError("Planner response targets a stale or inactive goal")
    if focus["id"] == result["root_id"]:
        if decision["completion"]["fact"] != "unknown":
            raise ValueError("The free-text primary objective requires manual acceptance; give its observable outcomes child goals")
        if decision["mode"] in {"act", "retire"}:
            raise ValueError("Decompose the primary objective into a concrete child goal or explain why planning is blocked")
    if decision["mode"] == "complete":
        if focus["id"] != result["root_id"]:
            raise ValueError("Only the primary objective can be claimed complete; use act or retire for other goals")
        verified = [g["id"] for g in result["goals"] if g.get("parent_id") == focus["id"]
                    and not g.get("retired") and g["status"] == "completed"]
        if not verified:
            raise ValueError("Claim the primary objective complete only after a child goal is verified")
        rejected = result.get("completion_rejected")
        if rejected and len(verified) <= rejected["verified_count"]:
            raise ValueError("The operator said the primary objective is not done; verify another child goal before claiming it again")
        focus["completion_claim"] = {"reason": decision["reason"],
                                     "observation": observation_ref(context["observation"]), "verified_goal_ids": verified}
        result.update(status="goal_reached", reason=decision["reason"], revision=result["revision"] + 1)
        return result
    if decision["mode"] == "retire":
        # Retirement is not success: the focus keeps its own completion; the decision's is ignored.
        focus.pop("steps", None)
        result.update(reason=decision["reason"], revision=result["revision"] + 1)
        retire(result, focus, decision, context)
        return result
    if focus["id"] != result["root_id"]:
        facts.validate(decision["completion"], context["fact_catalog"], "completion")
    # Discovery and expansion are atomic: child quantities depend on this newly validated condition.
    focus["completion"] = decision["completion"]
    if len(result["goals"]) + len(decision["subgoals"]) > MAX_GOALS:
        block(result, "Goal hierarchy reached its bounded size; review the objective before continuing.")
        return result
    generated = {}
    for index, proposed in enumerate(decision["subgoals"]):
        parent = generated[proposed["parent_key"]] if proposed["parent_key"] else focus
        ancestors = goal_path(result, parent["id"])
        if len(ancestors) >= MAX_DEPTH:
            raise ValueError(f"Goal decomposition is too deep; choose an executable goal or report the blocker (at subgoals.{index})")
        if proposed["objective"].strip().casefold() in {g["objective"].strip().casefold() for g in ancestors}:
            raise ValueError(f"Recursive goal repeats an ancestor instead of making progress (at subgoals.{index})")
        facts.validate(proposed["completion"], context["fact_catalog"], f"subgoals.{index}.completion")
        goal = {"id": f"goal-{result['revision'] + 1}-{index}", "parent_id": parent["id"],
                "objective": proposed["objective"], "completion": proposed["completion"],
                "source_observation": observation_ref(context["observation"]),
                "status": "pending", "actionable": proposed["actionable"], "attempts": 0, "reviews": 0}
        result["goals"].append(goal)
        generated[proposed["key"]] = goal
    focus.update(completion=decision["completion"],
                 source_observation=observation_ref(context["observation"]), actionable=decision["mode"] == "act",
                 attempts=0, reviews=focus.get("reviews", 0) + 1)
    focus.pop("recent_actions", None)
    focus.pop("repeating", None)
    if decision["mode"] == "act":
        focus["steps"] = decision["steps"]
    else:
        focus.pop("steps", None)  # Expand and blocked leave no leaf steps to run.
    result.update(reason=decision["reason"], revision=result["revision"] + 1)
    if decision["mode"] == "blocked":
        give_up(result, focus["id"], decision["reason"])
    else:
        select_active(result)
    return result


def rejection_message(exc):
    """One line for the pause banner; the full proposal lives in the event."""
    if isinstance(exc, ValidationError):
        error = exc.errors()[0]
        message = error["msg"].removeprefix("Value error, ")
        location = ".".join(str(part) for part in error["loc"]) or "decision"
        return f"{message} (at {location})"
    return str(exc)


def _rounded(value):
    if isinstance(value, float):
        return round(value)
    if isinstance(value, list):
        return [_rounded(item) for item in value]
    if isinstance(value, dict):
        return {key: _rounded(item) for key, item in value.items()}
    return value


def record_action(plan, action=None):
    goal = active_goal(plan)
    if not goal:
        return
    goal["attempts"] = goal.get("attempts", 0) + 1
    if action is None:
        return
    fingerprint = json.dumps([action.get("verb"), action.get("target_id", ""), _rounded(action.get("parameters") or {})], sort_keys=True)
    label = (action.get("parameters") or {}).get("name") or action.get("verb")
    window = ((goal.get("recent_actions") or []) + [[fingerprint, label]])[-4:]
    goal["recent_actions"] = window
    fingerprints = [item[0] for item in window]
    same = any(fingerprints.count(item) >= 3 for item in fingerprints)
    alternating = (len(fingerprints) == 4 and fingerprints[0] == fingerprints[2]
                   and fingerprints[1] == fingerprints[3] and fingerprints[0] != fingerprints[1])
    if same or alternating:
        if not goal.get("repeating"):
            goal["repeats"] = goal.get("repeats", 0) + 1
        goal["repeating"] = label


def prompt_plan(plan):
    """Keep the hierarchy in context, not a growing duplicate of the observation ledger."""
    full = {g["id"] for g in goal_path(plan)} | {g["id"] for g in plan["goals"] if g.get("parent_id") == plan.get("active_goal_id")}
    shown = {"primary_goal": plan["primary_goal"], "active_goal_id": plan["active_goal_id"], "reason": plan["reason"],
             "goals": [{k: g[k] for k in ("id", "parent_id", "objective", "completion", "status", "actionable")} if g["id"] in full
                       else {"id": g["id"], "parent_id": g["parent_id"], "status": g["status"], "objective": g["objective"][:160]}
                       for g in plan["goals"] if not g.get("retired")]}
    if plan.get("completion_rejected"):
        shown["completion_rejected"] = plan["completion_rejected"]
    return shown
