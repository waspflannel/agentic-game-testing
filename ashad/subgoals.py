"""Instance-owned backward planning. Proposals are accepted unless they contradict observed state; only measure() verifies success against fact_catalog values."""
from __future__ import annotations

import copy
import math
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from . import facts

# ponytail: retain at most 64 goals; archive verified branches if live objectives outgrow this ceiling.
MAX_GOALS = 64
MAX_DEPTH = 8
BUDGET = 6  # Turns without progress on one leaf before it is broken.
ENTER = "enter"  # The built-in first stage's id.
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


class Step(Contract):
    control: str = Field(min_length=1, max_length=40, description="An advertised input name or a game verb")
    item: str = Field(max_length=60, description="The hotbar item_id the step needs; empty if none")
    count: int = Field(ge=0, le=99, description="Repeats; 0 means until the check is met")
    precondition: str = Field(max_length=200, description="The manual's precondition text or a catalog fact")
    verifies: str = Field(min_length=1, max_length=60, description="A fact_catalog name, or unknown")
    targets: str = Field(max_length=160, description="Space-separated field=value conditions every target of a repeated step matches (observed tile or entity fields; empty value means empty or missing), nearest first; empty for a single action")


class ProposedGoal(Contract):
    key: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,50}$")
    parent_key: str = Field(max_length=50, description="Empty attaches to focus; otherwise an earlier proposed key.")
    objective: str = Field(min_length=1, max_length=600)
    completion: Check
    actionable: bool = Field(description="True only for a bounded achievable goal with currently available ordinary controls; false means decompose later.")
    steps: list[Step] = Field(max_length=8, description="The stage's ordered steps; [] for a goal that will be expanded later.")


class PlanningDecision(Contract):
    goal_id: str = Field(min_length=1, max_length=100)
    mode: Literal["expand", "act", "blocked", "retire"]
    completion: Check
    reason: str = Field(min_length=1, max_length=1000)
    subgoals: list[ProposedGoal] = Field(max_length=12)
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
    """The root, whose check the planner sets, and the built-in first stage: get into a playable world. The root is planned
    once that stage is done, so the planner sees every fact the world reports instead of a menu's few."""
    return {"version": 1, "primary_goal": primary_goal, "player_id": player_id, "run_id": run_id, "root_id": "root", "revision": 0,
            "active_goal_id": "root", "status": "pending", "reason": "Waiting for a fresh game observation.",
            "world_id": None, "observation_generation": None, "goals": [{
                "id": "root", "parent_id": None, "objective": primary_goal,
                "completion": unknown("Set when the goal is planned in a playable world."),
                "status": "pending", "actionable": False, "attempts": 0}, {
                "id": ENTER, "parent_id": "root", "objective": "Get into the game world so the player can act.",
                "completion": Check(fact="world_ready", args=[], op="==", value=True,
                                    description="world_ready is true: the world is loaded and the player can act.").model_dump(),
                "status": "pending", "actionable": True, "attempts": 0}]}


def stages(plan):
    """The root's planned stages: its children that are not retired, without the built-in enter stage."""
    return [g for g in plan["goals"] if g.get("parent_id") == plan["root_id"] and not g.get("retired") and g["id"] != ENTER]


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
    """Progress resets the leaf's attempts."""
    goal.update(attempts=0, progress_at=now)


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
    selected = descend(nodes[plan["root_id"]])  # The root never completes: its check passing is a claim for the operator.
    plan["active_goal_id"] = selected["id"]
    if selected["status"] == "blocked":
        plan.update(status="blocked", reason=selected.get("blocked_reason", "This goal needs help."))
    else:
        selected["status"] = "active"
        plan["status"] = "active"
    return selected


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
    # The operator paused and resumed: they may have fixed the situation, so every open goal starts its budget again.
    resumed = plan.get("control_generation") not in (None, control_generation)
    if resumed:
        for goal in plan["goals"]:
            if goal["status"] != "completed":
                goal["attempts"] = 0
    # Unloading emits an empty world ID. Keep the last actual world so A -> menu -> B cannot reuse A's proof.
    plan.update(world_id=world or previous_world, observation_generation=binding, control_generation=control_generation)
    completed = []
    progressed = False
    path = goal_path(plan)
    on_path = {g["id"] for g in path}
    for goal in plan["goals"]:
        if goal["status"] == "completed" or goal.get("retired"):
            continue
        # Stages complete in order: a later stage is measured only once every earlier sibling is completed (this pass
        # included, since siblings are stored in order), so an already-true later check cannot complete it early.
        earlier = [g for g in plan["goals"][:plan["goals"].index(goal)] if g.get("parent_id") == goal.get("parent_id") and not g.get("retired")]
        if goal["id"] not in on_path and any(g["status"] != "completed" for g in earlier):
            continue
        done, value = measure(goal["completion"], observation)
        if goal["id"] in on_path and toward(goal["completion"], goal.get("measured"), value):
            progressed = True
        if value is not None:  # A loading screen reads nothing; keep the last value so progress is not erased.
            goal["measured"] = value
        if done and goal["id"] != plan["root_id"]:  # The root's check passing is a claim for the operator (reached), not a completion.
            goal.update(status="completed", completion_evidence={**observation_ref(observation), "value": value})
            goal.pop("blocked_reason", None)
            completed.append(goal["id"])
    # Progress on any goal of the active path (e.g. harvesting toward "10 wheat") is progress for the leaf pursuing it.
    if progressed and path:
        credit_progress(path[-1], now)
    for goal in plan["goals"]:
        if goal["status"] not in {"completed", "gave_up"} and any(g["status"] == "completed" for g in goal_path(plan, goal["id"])[:-1]):
            goal.update(retired=True, status="blocked", blocked_reason="No longer needed: the parent goal was verified directly.")
    previous_active = plan.get("active_goal_id")
    # A claim or hand-back waits for the operator; re-selecting would move the active goal away from the root.
    waiting = plan["status"] == "goal_reached" or (plan["status"] == "handed_back" and not resumed)
    selected = active_goal(plan) if waiting else select_active(plan)
    if selected and selected["id"] != previous_active:
        selected["activated_at"] = now
    plan["last_observation"] = observation_ref(observation)
    if completed:
        plan["revision"] += 1
    return completed


def needs_planning(plan):
    goal = active_goal(plan)
    return bool(goal) and plan["status"] != "blocked" and not goal.get("actionable")


def broken(plan) -> bool:
    """The active leaf spent its budget of turns without progress."""
    goal = active_goal(plan)
    return (bool(goal) and goal["id"] != plan["root_id"] and not any(g.get("parent_id") == goal["id"] for g in plan["goals"])
            and goal.get("attempts", 0) >= BUDGET)


def record_attempt(plan):
    """One turn without progress on the active goal; returns the new count."""
    goal = active_goal(plan)
    goal["attempts"] = goal.get("attempts", 0) + 1
    return goal["attempts"]


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
                goal.update(status="gave_up", gave_up=True, actionable=False, blocked_reason=reason)
    selected = select_active(plan)
    if selected["id"] == plan["root_id"] and not selected.get("replanned"):
        # The last open subgoal was given up but the goal is unverified: ask the planner once for new subgoals from here.
        selected.update(replanned=True, actionable=False)
        plan.update(reason=reason, trigger="gave_up")
    elif selected["id"] == plan["root_id"]:
        plan.update(status="handed_back", reason=reason)
    else:
        plan.update(reason=reason, trigger="gave_up")
    plan["revision"] += 1


def reached(plan, observation):
    """Why the goal is reached, or None. A goal with a check is reached only when that check passes. A goal the planner
    could give no check is reached when every stage is verified, and after a rejection only with more stages verified."""
    root = next(g for g in plan["goals"] if g["id"] == plan["root_id"])
    if root["completion"]["fact"] != "unknown":
        done, value = measure(root["completion"], observation)
        return f"The goal's check passed: {root['completion']['description']} (value {value})." if done else None
    planned = stages(plan)
    verified = [g for g in planned if g["status"] == "completed"]
    if planned and len(verified) == len(planned) and len(verified) > (plan.get("completion_rejected") or {}).get("verified_count", 0):
        return "Every stage of the plan is verified, but the goal has no check of its own; please confirm it is done."
    return None


def claim(plan, observation, reason):
    """Hand the reached goal to the operator. checked says the goal's own check passed, so the plan may be saved for reuse."""
    root = next(g for g in plan["goals"] if g["id"] == plan["root_id"])
    root["completion_claim"] = {"reason": reason, "observation": observation_ref(observation), "checked": root["completion"]["fact"] != "unknown",
                                "verified_goal_ids": [g["id"] for g in stages(plan) if g["status"] == "completed"]}
    plan.update(status="goal_reached", reason=reason, revision=plan["revision"] + 1)


def reopen_after_rejected_claim(plan, note):
    """The operator said not done: the root plans again and must verify more before claiming again. A check that passed
    was not enough, so the root loses it and the planner gives it a new one."""
    root = next(g for g in plan["goals"] if g["id"] == plan["root_id"])
    claim = root.pop("completion_claim")
    plan["completion_rejected"] = {"note": note, "claim_reason": claim["reason"], "verified_count": len(claim["verified_goal_ids"])}
    if claim.get("checked"):
        root["completion"] = unknown("The operator said the goal is not done although its check passed: " + claim["reason"])
    root["actionable"] = False
    plan.update(status="active", trigger="not_done", reason="You said the primary goal is not done" + (f": {note}" if note else "."),
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
    if selected and not selected.get("steps"):
        selected["actionable"] = False


def kept_steps(steps, context, warnings, location):
    """Steps are optional guidance: a malformed step or one verified by a fact outside the catalog is dropped, never rejected."""
    kept = []
    for index, step in enumerate(steps if isinstance(steps, list) else []):
        try:
            step = Step.model_validate(step).model_dump()
        except ValidationError:
            warnings.append(f"{location}.{index} dropped: not a valid step")
            continue
        # The catalog lists shown names; a step keeps the stored name, like its stage's check.
        step["verifies"] = facts.STORED.get(step["verifies"], step["verifies"])
        if step["verifies"] != "unknown" and facts.SHOWN.get(step["verifies"], step["verifies"]) not in context["fact_catalog"]["facts"]:
            warnings.append(f"{location}.{index} dropped: verifies {step['verifies']!r} is not in fact_catalog")
            continue
        if len(kept) < 8:
            kept.append(step)
    return kept


def apply_decision(plan, raw, context, warnings=None):
    """Validate fully before editing a copy; callers commit only while their run is current."""
    warnings = [] if warnings is None else warnings
    if isinstance(raw, dict):
        raw = copy.deepcopy(raw)
        raw["steps"] = kept_steps(raw.get("steps"), context, warnings, "steps")
        for index, proposed in enumerate(raw.get("subgoals") if isinstance(raw.get("subgoals"), list) else []):
            if isinstance(proposed, dict):
                proposed["steps"] = kept_steps(proposed.get("steps"), context, warnings, f"subgoals.{index}.steps")
        mode = raw.get("mode")
        if mode == "expand" and not raw.get("subgoals") and raw.get("goal_id") != plan["root_id"]:
            raw["mode"] = "act"
            warnings.append("expand without subgoals applied as act")
        if mode == "act" and raw.get("subgoals"):
            raw["mode"] = "expand"
            warnings.append("act with subgoals applied as expand")
        if mode in {"blocked", "retire"} and raw.get("subgoals"):
            raw["subgoals"] = []
            warnings.append(f"{mode} subgoals dropped")
    decision = PlanningDecision.model_validate(raw).model_dump()
    result = copy.deepcopy(plan)
    focus = active_goal(result)
    if not focus or decision["goal_id"] != focus["id"]:
        raise ValueError("Planner response targets a stale or inactive goal")
    if focus["id"] == result["root_id"] and decision["mode"] in {"act", "retire"}:
        raise ValueError("Decompose the primary objective into a concrete child goal or explain why planning is blocked")
    if decision["mode"] == "retire":
        # Retirement is not success: the focus keeps its own completion; the decision's is ignored.
        focus.pop("steps", None)
        result.update(reason="Retired: " + decision["reason"], revision=result["revision"] + 1)
        retire(result, focus, decision, context)
        return result
    facts.validate(decision["completion"], context["fact_catalog"], "completion")
    # Discovery and expansion are atomic: child quantities depend on this newly validated condition.
    focus["completion"] = decision["completion"]
    if len(result["goals"]) + len(decision["subgoals"]) > MAX_GOALS:
        block(result, "Goal hierarchy reached its bounded size; review the objective before continuing.")
        return result
    if (focus["id"] == result["root_id"] and decision["completion"]["fact"] != "unknown"
            and any(proposed["completion"]["fact"] == "unknown" for proposed in decision["subgoals"])):
        raise ValueError("The root's check must prove the whole objective: a stage no catalog fact proves means the root's check is unknown, "
                         "or block that stage.")
    generated = {}
    for index, proposed in enumerate(decision["subgoals"]):
        parent = generated[proposed["parent_key"]] if proposed["parent_key"] else focus
        ancestors = goal_path(result, parent["id"])
        if len(ancestors) >= MAX_DEPTH:
            raise ValueError(f"Goal decomposition is too deep; choose an executable goal or report the blocker (at subgoals.{index})")
        if proposed["objective"].strip().casefold() in {g["objective"].strip().casefold() for g in ancestors}:
            raise ValueError(f"Recursive goal repeats an ancestor instead of making progress (at subgoals.{index})")
        check = proposed["completion"]
        facts.validate(check, context["fact_catalog"], f"subgoals.{index}.completion")
        planned_value = facts.read(context["fact_catalog"]["values"], check["fact"], facts.args_of(check))
        # A check true before any work would complete its stage at once; an unread stock count is likely 0, which meets a <= check.
        if planned_value is not None and facts.met(check, planned_value):
            raise ValueError(f"This completion check is already met now (value {planned_value}); use a check that rises with the work, "
                             f"e.g. crops_planted >= N (at subgoals.{index}.completion)")
        if planned_value is None and check["op"] == "<=" and facts.FACTS.get(check["fact"], ((), "", ""))[2] == "stock":
            raise ValueError("A <= check on a stock fact that cannot be read now is likely met before any work; use a check that rises "
                             f"with the work, e.g. crops_planted >= N (at subgoals.{index}.completion)")
        goal = {"id": f"goal-{result['revision'] + 1}-{index}", "parent_id": parent["id"],
                "objective": proposed["objective"], "completion": check,
                "source_observation": observation_ref(context["observation"]),
                "status": "pending", "actionable": proposed["actionable"], "attempts": 0,
                "planned_value": planned_value}
        if proposed["steps"]:
            goal["steps"] = proposed["steps"]
        result["goals"].append(goal)
        generated[proposed["key"]] = goal
    focus.update(completion=decision["completion"],
                 source_observation=observation_ref(context["observation"]), actionable=decision["mode"] == "act",
                 attempts=0, planned_value=facts.read(context["fact_catalog"]["values"], decision["completion"]["fact"], facts.args_of(decision["completion"])))
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


def playable(observation):
    """The world is ready, a player is present and is not known to be unable to act (not a loading, menu, character
    creation or cutscene screen): the world_ready fact."""
    return facts.values(observation)["world_ready"]


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
