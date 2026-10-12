"""Instance-owned backward planning. Model proposals are not completion evidence."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

# ponytail: retain at most 64 goals; archive verified branches if live objectives outgrow this ceiling.
MAX_GOALS = 64
MAX_DEPTH = 8
ACTION_REVIEW_INTERVAL = 8
MAX_UNPRODUCTIVE_REVIEWS = 3
MAX_OBSERVATION_AGE = 5


def stale(stamp, now=None):
    """One freshness rule. Callers pass controller receipt time when they have it; game clocks may be skewed."""
    return type(stamp) not in (int, float) or not math.isfinite(stamp) or (now or time.time()) - stamp > MAX_OBSERVATION_AGE


def numbers(text):
    return {int(n) for n in re.findall(r"\b[1-9][0-9]*\b", text)}


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Evidence(Contract):
    source: Literal["observation", "controls", "objective"]
    path: str = Field(max_length=300, description="JSON pointer within observation; empty for controls/objective.")
    quote: str = Field(min_length=1, max_length=1200, description="Exact excerpt from the referenced value, not an invented fact.")


class Match(Contract):
    field: str = Field(min_length=1, max_length=100)
    value: str | bool | int | float


class Completion(Contract):
    kind: Literal["unknown", "quest_completed", "inventory_quantity", "observed_count", "world_ready"]
    description: str = Field(min_length=1, max_length=600)
    target: str = Field(max_length=200, description="Observed quest/item ID; empty for other kinds.")
    quantity: int = Field(ge=1, le=100000)
    collection: Literal["", "entities", "tiles", "recipes"]
    matches: list[Match] = Field(max_length=6, description="All exact field/value matches must hold on each counted record.")

    @model_validator(mode="after")
    def check_shape(self):
        if self.kind in {"quest_completed", "inventory_quantity"}:
            if not self.target or self.collection or self.matches:
                raise ValueError("Quest/inventory checks require only a target and quantity")
        elif self.target:
            raise ValueError("This completion check does not accept a target")
        if self.kind == "observed_count":
            if not self.collection or not self.matches:
                raise ValueError("Observed counts require a collection and exact matches")
            allowed = {
                "tiles": {"id", "ground", "state", "plant", "growth_stage", "tillable", "waterable", "harvestable"},
                "recipes": {"id", "name", "output", "count"},
                "entities": {"id", "kind", "label", "parent"},
            }[self.collection]
            for match in self.matches:
                if match.field not in allowed and not (self.collection == "entities" and match.field.startswith("state.") and len(match.field.split(".")) == 2):
                    raise ValueError("Completion check uses an unsupported field")
                if isinstance(match.value, float) and not math.isfinite(match.value):
                    raise ValueError("Completion values must be finite")
            if self.collection == "entities" and not any(m.field in {"id", "kind", "label", "parent"} for m in self.matches):
                raise ValueError("Entity checks must identify the relevant entity or kind")
        elif self.collection or self.matches:
            raise ValueError("Only observed_count accepts collection matches")
        return self


class ProposedGoal(Contract):
    key: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,50}$")
    parent_key: str = Field(max_length=50, description="Empty attaches to focus; otherwise an earlier proposed key.")
    objective: str = Field(min_length=1, max_length=600)
    completion: Completion
    evidence: list[Evidence] = Field(min_length=1, max_length=4)
    actionable: bool = Field(description="True only for a bounded achievable goal with currently available ordinary controls; false means decompose later.")


class PlanningDecision(Contract):
    goal_id: str = Field(min_length=1, max_length=100)
    mode: Literal["expand", "act", "blocked", "retire"]
    completion: Completion
    evidence: list[Evidence] = Field(min_length=1, max_length=4)
    reason: str = Field(min_length=1, max_length=1000)
    subgoals: list[ProposedGoal] = Field(max_length=12)

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
    return Completion(kind="unknown", description=description, target="", quantity=1, collection="", matches=[]).model_dump()


def new_plan(primary_goal, player_id, run_id=None):
    return {"version": 1, "primary_goal": primary_goal, "player_id": player_id, "run_id": run_id, "root_id": "root", "revision": 0,
            "active_goal_id": "root", "status": "pending", "reason": "Waiting for a fresh game observation.",
            "world_id": None, "observation_generation": None, "goals": [{
                "id": "root", "parent_id": None, "objective": primary_goal,
                "completion": unknown("Primary-objective acceptance is manual; verify concrete outcomes in its child goals."),
                "status": "pending", "actionable": False, "evidence": [], "attempts": 0, "reviews": 0}]}


def goal_path(plan, goal_id=None):
    nodes = {g["id"]: g for g in plan["goals"]}
    path, current = [], nodes.get(goal_id or plan.get("active_goal_id"))
    while current and len(path) <= MAX_DEPTH:
        path.append(current)
        current = nodes.get(current.get("parent_id"))
    return list(reversed(path))


def active_goal(plan):
    return next((g for g in plan["goals"] if g["id"] == plan.get("active_goal_id")), None)


def pointer(value, path):
    if not path.startswith("/"):
        raise ValueError("Evidence must reference a JSON pointer in the supplied observation")
    for part in path[1:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdecimal() and int(part) < len(value):
            value = value[int(part)]
        else:
            raise ValueError("Evidence refers to a missing observation field")
    return value


def string_leaves(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from string_leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from string_leaves(item)


def grounded_evidence(references, context):
    excerpts = []
    for ref in references:
        if ref["source"] == "observation":
            value = pointer(context["observation"], ref["path"])
        else:
            if ref["path"]:
                raise ValueError("Only observation evidence accepts a path")
            value = context["game_controls"] if ref["source"] == "controls" else context["goal"]
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        compact = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if ref["quote"] not in text and ref["quote"] not in compact and not any(ref["quote"] in leaf for leaf in string_leaves(value)):
            raise ValueError("Planner evidence quote does not occur in its cited source")
        excerpts.append(text)
    if not any(ref["source"] == "observation" for ref in references):
        raise ValueError("Every proposed goal needs a current observation reference")
    return "\n".join(excerpts)


def explicit_quantities(objective, condition):
    """Counts written before the counted thing ("10 wheat"), not identifiers ("plot 3", "tile-3")."""
    names = {w for w in re.split(r"[_\-\s]+", condition["target"].lower()) if w}
    names |= {condition["collection"]} - {""}
    names |= {m["value"].lower() for m in condition.get("matches", []) if isinstance(m["value"], str) and m["value"]}
    found = set()
    for n, word in re.findall(r"\b([1-9][0-9]*)\b\s*([A-Za-z][A-Za-z_-]*)", objective):
        word = word.lower()
        if any(word.startswith(name) or name.startswith(word) for name in names):
            found.add(int(n))
    return found


def validate_quantity(condition, references, context, objective, parent):
    if condition["kind"] not in {"inventory_quantity", "observed_count"}:
        return
    quantity, target = condition["quantity"], condition["target"]
    explicit = explicit_quantities(objective, condition)
    if explicit and quantity < max(explicit):
        raise ValueError("Completion threshold is below the goal's explicit quantity")
    supported = {1} | numbers(context["goal"])
    if parent and parent["kind"] in {"inventory_quantity", "observed_count"}:
        supported.add(parent["quantity"])
    recipes = []
    def collect(value):
        if isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, dict):
            if "ingredients" in value and "output" in value:
                recipes.append(value)
            if target and (value.get("item_id") == target or value.get("output") == target):
                count = value.get("count")
                if type(count) is int and count > 0:
                    supported.add(count)
            for key, item in value.items():
                if key in {"description", "requirements"} and isinstance(item, str):
                    supported.update(numbers(item))
                elif isinstance(item, (dict, list)):
                    collect(item)
    for ref in references:
        if ref["source"] == "observation":
            value = pointer(context["observation"], ref["path"])
            collect(value)
            if isinstance(value, str):
                supported.update(numbers(value))
            elif ref["path"].endswith("/count") and type(value) is int and value > 0:
                supported.add(value)
        supported.update(numbers(ref["quote"]))
    # Scale a cited recipe's ingredient requirement to its parent's still-missing output.
    if target and parent and parent["kind"] == "inventory_quantity":
        _, held = check_completion(parent, context["observation"])
        required_quantities = []
        for recipe in recipes:
            output_count = recipe.get("count")
            if recipe.get("output") != parent["target"] or type(output_count) is not int or output_count <= 0 or held is None:
                continue
            batches = math.ceil(max(0, parent["quantity"] - held) / output_count)
            ingredients = recipe["ingredients"] if isinstance(recipe["ingredients"], list) else []
            for ingredient in ingredients:
                if not isinstance(ingredient, dict):
                    continue
                count = ingredient.get("count")
                if ingredient.get("item_id") == target and type(count) is int and count > 0:
                    supported.add(batches * count)
                    required_quantities.append(batches * count)
        if required_quantities and quantity < min(required_quantities):
            raise ValueError("Ingredient goal is below the cited recipe's requirement for its parent")
    if quantity not in supported:
        raise ValueError("Completion quantity lacks cited requirement, recipe, parent, or primary-objective support")


def validate_condition(condition, references, context, previous=None, *, objective="", parent=None):
    text = grounded_evidence(references, context)
    # Do not let a revision quietly lower ten wheat to one, or replace a quest with a button click.
    if previous and previous["kind"] != "unknown":
        old_check = json.dumps({k: v for k, v in previous.items() if k != "description"}, sort_keys=True)
        new_check = json.dumps({k: v for k, v in condition.items() if k != "description"}, sort_keys=True)
        if old_check != new_check:
            raise ValueError("A grounded goal's completion condition cannot be weakened by replanning")
        return
    if condition["kind"] == "world_ready" and context["observation"].get("ready") is True:
        raise ValueError("World readiness is only a setup goal while the world is not yet ready")
    validate_quantity(condition, references, context, objective, parent)
    target = condition.get("target")
    if target and target not in text:
        raise ValueError("Completion target is not supported by the cited evidence")
    if condition["kind"] == "quest_completed":
        state = context["observation"].get("state", {})
        known = {q.get("id") for q in state.get("objectives", [])} | set(state.get("completed_quests", []))
        if target not in known:
            raise ValueError("A quest completion target must have been observed, not invented from a walkthrough")
    for match in condition.get("matches", []):
        value = match["value"]
        if isinstance(value, str) and value and value not in text:
            raise ValueError("Expected state value is not supported by the cited evidence")


def records(observation, collection):
    if collection == "tiles":
        return observation.get("state", {}).get("tiles")
    if collection == "entities":
        return observation.get("entities")
    if collection == "recipes":
        entities = observation.get("entities")
        if not isinstance(entities, list):
            return None
        return [recipe for entity in entities for recipe in entity.get("state", {}).get("recipes", [])]
    return None


def check_completion(condition, observation):
    """Return (satisfied, measured value). Missing facts never count as success."""
    kind = condition["kind"]
    if kind == "unknown":
        return False, None
    if kind == "world_ready":
        ready = observation.get("ready") is True
        return ready, int(ready)
    # Inventory, quests, tiles and entities may be stale during load/disconnect.
    if observation.get("ready") is not True:
        return False, None
    state = observation.get("state", {})
    if kind == "quest_completed":
        quests = state.get("completed_quests")
        if not isinstance(quests, list):
            return False, None
        done = condition["target"] in quests
        return done, int(done)
    if kind == "inventory_quantity":
        player = state.get("player", {})
        inventory, hotbar = player.get("inventory"), player.get("hotbar")
        if not isinstance(inventory, list) or not isinstance(hotbar, list):
            return False, None
        value = sum(item["count"] for item in inventory + hotbar if item.get("item_id") == condition["target"]
                    and type(item.get("count")) in (int, float) and math.isfinite(item["count"]) and item["count"] >= 0)
    else:
        values = records(observation, condition["collection"])
        if values is None:
            return False, None
        def matches(record):
            for expected in condition["matches"]:
                actual = record
                for field in expected["field"].split("."):
                    if not isinstance(actual, dict) or field not in actual:
                        return False
                    actual = actual[field]
                if type(actual) is not type(expected["value"]) or actual != expected["value"]:
                    return False
            return True
        value = sum(matches(record) for record in values)
    return value >= condition["quantity"], value


def observation_ref(observation):
    return {k: observation.get(k) for k in ("observation_id", "timestamp", "world_id", "generation")}


def planning_scene(observation):
    """A plan inferred for a transient screen must not land after that screen changes."""
    controls = frozenset(e.get("id") for e in observation.get("entities", []) if e.get("kind") == "ui" and e.get("actions"))
    return observation.get("world_id"), observation.get("generation"), observation.get("ready"), controls


def knowledge_signature(plan, observation):
    """New knowledge matters; leaving a station's view must not repeatedly replan."""
    quests = [{k: q.get(k) for k in ("id", "name", "description", "requirements")} for q in observation.get("state", {}).get("objectives", [])]
    for quest in quests:
        quest["requirements"] = [r.get("description") for r in quest.get("requirements") or []]
    seen = set(plan.get("known_recipe_signatures", []))
    seen.update(hashlib.sha256(json.dumps(r, sort_keys=True).encode()).hexdigest() for r in records(observation, "recipes") or [])
    plan["known_recipe_signatures"] = sorted(seen)[:128]
    return hashlib.sha256(json.dumps([quests, plan["known_recipe_signatures"]], sort_keys=True).encode()).hexdigest()


def select_active(plan):
    nodes = {g["id"]: g for g in plan["goals"]}
    for goal in nodes.values():
        if goal["status"] == "active":
            goal["status"] = "pending"
    def descend(goal):
        if goal["status"] == "completed":
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
        # Same instance, different world: old achievement evidence cannot prove anything here.
        fresh = new_plan(plan["primary_goal"], plan["player_id"], plan.get("run_id"))
        fresh["revision"] = plan["revision"] + 1
        plan.clear()
        plan.update(fresh)
    rebound = plan.get("observation_generation") not in (None, binding)
    resumed = plan.get("control_generation") not in (None, control_generation)
    if rebound or resumed:
        for goal in plan["goals"]:
            if goal["status"] != "completed" and not goal.get("retired"):
                goal.update(actionable=False, status="pending", attempts=0, reviews=0)
                goal.pop("blocked_reason", None)
                goal.pop("progress_high_water", None)
        plan["reason"] = "Control or game binding changed; reassess from fresh observations."
    # Unloading emits an empty world ID. Keep the last actual world so A -> menu -> B cannot reuse A's proof.
    plan.update(world_id=world or previous_world, observation_generation=binding, control_generation=control_generation)
    completed = []
    for goal in plan["goals"]:
        if goal["status"] == "completed" or goal.get("retired"):
            continue
        # Generation-local entity IDs must not validate a new game's binding.
        if rebound and goal["completion"]["kind"] == "observed_count":
            goal["completion"] = unknown("Rebind this observed-state check to the current game generation.")
        done, value = check_completion(goal["completion"], observation)
        previous_best = goal.get("progress_high_water")
        if value is not None and (previous_best is None or value > previous_best):
            goal.update(attempts=0, reviews=0, progress_at=now, progress_high_water=value)
        goal["measured"] = value
        if done:
            goal.update(status="completed", completion_evidence={**observation_ref(observation), "value": value})
            goal.pop("blocked_reason", None)
            completed.append(goal["id"])
    for goal_id in completed:
        for ancestor in goal_path(plan, goal_id)[:-1]:
            ancestor.update(reviews=0, attempts=0, progress_at=now)
    for goal in plan["goals"]:
        if goal["status"] != "completed" and any(g["status"] == "completed" for g in goal_path(plan, goal["id"])[:-1]):
            goal.update(retired=True, status="blocked", blocked_reason="No longer needed: the parent goal was verified directly.")
    previous_active = plan.get("active_goal_id")
    selected = select_active(plan)
    if selected and selected["id"] != previous_active:
        selected["activated_at"] = now
        # A sibling may have lost its prerequisites while another goal was being pursued.
        selected["actionable"] = False
    # Loading screens are not new knowledge; new knowledge replans without resetting the no-progress cap.
    if observation.get("ready") is True:
        signature = knowledge_signature(plan, observation)
        if selected and plan.get("knowledge_signature") not in (None, signature):
            selected["actionable"] = False
            plan["reason"] = "Observed objectives or recipes changed; reassess the active goal."
        plan["knowledge_signature"] = signature
    plan["last_observation"] = observation_ref(observation)
    if completed:
        plan["revision"] += 1
    return completed


def needs_planning(plan):
    goal = active_goal(plan)
    if not goal or plan["status"] == "blocked":
        return False
    if goal.get("attempts", 0) >= ACTION_REVIEW_INTERVAL:
        goal["actionable"] = False
        plan["reason"] = "Several actions have not verified this goal; revise prerequisites or approach."
    return not goal.get("actionable")


def block(plan, reason):
    goal = active_goal(plan)
    if goal:
        goal.update(status="blocked", actionable=False, blocked_reason=reason)
    plan.update(status="blocked", reason=reason, revision=plan["revision"] + 1)


def retire(plan, focus, decision, context):
    """Retirement is not success: the goal and its descendants keep their predicates and leave the active path."""
    retired = {focus["id"]}
    for goal in plan["goals"]:
        if goal.get("parent_id") in retired:
            retired.add(goal["id"])
        if goal["id"] in retired and goal["status"] != "completed":
            goal.update(retired=True, status="blocked", actionable=False, blocked_reason=decision["reason"],
                        retirement_evidence={"observation": observation_ref(context["observation"]),
                                             "references": decision["evidence"]})
    selected = select_active(plan)
    if selected:
        selected["actionable"] = False


def apply_decision(plan, raw, context):
    """Validate fully before editing a copy; callers commit only while their run is current."""
    decision = PlanningDecision.model_validate(raw).model_dump()
    result = copy.deepcopy(plan)
    focus = active_goal(result)
    if not focus or decision["goal_id"] != focus["id"]:
        raise ValueError("Planner response targets a stale or inactive goal")
    if focus["id"] == result["root_id"]:
        if decision["completion"]["kind"] != "unknown":
            raise ValueError("The free-text primary objective requires manual acceptance; give its observable outcomes child goals")
        if decision["mode"] in {"act", "retire"}:
            raise ValueError("Decompose the primary objective into a concrete child goal or explain why planning is blocked")
    if decision["mode"] == "retire":
        # Retirement is not success: keep the original predicate and evidence intact.
        grounded_evidence(decision["evidence"], context)
        if any(decision["completion"][k] != focus["completion"][k] for k in ("kind", "target", "quantity", "collection", "matches")):
            raise ValueError("Retirement must preserve the original completion condition")
        result.update(reason=decision["reason"], revision=result["revision"] + 1)
        retire(result, focus, decision, context)
        return result
    ancestors = goal_path(result)
    parent_condition = ancestors[-2]["completion"] if len(ancestors) > 1 else None
    validate_condition(decision["completion"], decision["evidence"], context, focus["completion"],
                       objective=focus["objective"], parent=parent_condition)
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
            raise ValueError("Goal decomposition is too deep; choose an executable goal or report the blocker")
        if proposed["objective"].strip().casefold() in {g["objective"].strip().casefold() for g in ancestors}:
            raise ValueError("Recursive goal repeats an ancestor instead of making progress")
        validate_condition(proposed["completion"], proposed["evidence"], context,
                           objective=proposed["objective"], parent=parent["completion"])
        goal = {"id": f"goal-{result['revision'] + 1}-{index}", "parent_id": parent["id"],
                "objective": proposed["objective"], "completion": proposed["completion"],
                "evidence": proposed["evidence"], "source_observation": observation_ref(context["observation"]),
                "status": "pending", "actionable": proposed["actionable"], "attempts": 0, "reviews": 0}
        result["goals"].append(goal)
        generated[proposed["key"]] = goal
    focus.update(completion=decision["completion"], evidence=decision["evidence"],
                 source_observation=observation_ref(context["observation"]), actionable=decision["mode"] == "act",
                 attempts=0, reviews=focus.get("reviews", 0) + 1)
    result.update(reason=decision["reason"], revision=result["revision"] + 1)
    if decision["mode"] == "blocked" and focus["id"] != result["root_id"]:
        # Ask the parent before the operator: it replans with this reason; only a blocked root pauses the run.
        # Parent review caps bound repeated skips.
        result["reason"] = f"Skipped blocked goal \"{focus['objective']}\": {decision['reason']}"
        retire(result, focus, {**decision, "reason": result["reason"]}, context)
    elif decision["mode"] == "blocked":
        block(result, decision["reason"])
    elif focus["reviews"] > MAX_UNPRODUCTIVE_REVIEWS:
        block(result, "Repeated planning has not produced observable progress. " + decision["reason"])
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


def record_action(plan):
    goal = active_goal(plan)
    if goal:
        goal["attempts"] = goal.get("attempts", 0) + 1


def prompt_plan(plan):
    """Keep the hierarchy in context, not a growing duplicate of the observation ledger."""
    return {"primary_goal": plan["primary_goal"], "active_goal_id": plan["active_goal_id"], "reason": plan["reason"],
            "goals": [{k: g[k] for k in ("id", "parent_id", "objective", "completion", "status", "actionable")}
                      for g in plan["goals"] if not g.get("retired")]}
