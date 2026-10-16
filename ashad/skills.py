"""Skill library from wins: a leaf's steps as a general pattern over its completion fact's args, shared by every configuration of a project."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time

from . import facts, repeat, subgoals

MAX_WON_BUILDS = 20

# Verb-style display names by the fact that proves the skill; facts not listed fall back to the fact name.
VERBS = {
    "item_count": "take(<item>)",
    "tiles_tilled": "till()",
    "tiles_watered": "water()",
    "crops_planted": "plant(<seed>)",
    "crops_harvestable": "harvest(<plant_id>)",
    "delivered_to": "deliver(<item>,<target>)",
}


def shape(check):
    """The completion's fact, arg names and operator, e.g. crops_planted(plant_id)>=; arg values and targets vary between leaves."""
    return f"{check['fact']}({','.join(sorted(a['name'] for a in check['args']))}){check['op']}"


def target_kind(step):
    """The kind a step's targets name: kind=<x>, tile for tile conditions, else none."""
    targets = step.get("targets", "")
    if targets.startswith("state="):
        return "tile"
    return next((c[5:] for c in targets.split() if c.startswith("kind=")), "") or "none"


def key_suffix(steps):
    """Control and target kind of the skill's main step (the first that is not a hotbar select)."""
    main = next((s for s in steps if s["control"] != "select"), steps[0])
    return f"{main['control']}:{target_kind(main)}"


def skill_key(project_id, check, steps):
    return f"{project_id}:{shape(check)}:{key_suffix(steps)}"


def name(check):
    """Display name, e.g. plant(<seed>) for crops_planted, else the fact form crops_planted(<plant_id>)>=."""
    return VERBS.get(check["fact"]) or f"{check['fact']}({', '.join('<' + a + '>' for a in facts.FACTS[check['fact']][0])}){check['op']}"


def bind(steps, check, replay=False, measured=None):
    """Steps with the check's string arg values as {arg} placeholders in item and targets values (save), or substituted back (replay).

    On save, an item or targets condition value equal to a value becomes {arg} and an item starting with value_ keeps its suffix
    (wheat_seed with item_id=wheat becomes {item_id}_seed). On replay, a step verifying the check's own fact
    gets the count still needed: the target minus the value measured at activation, or 0 (until the check is
    met) when that was not readable."""
    args = {a["name"]: a["value"] for a in check["args"] if isinstance(a["value"], str) and a["value"]}

    def placeholder(text):
        for arg, value in args.items():
            if text == value:
                return "{" + arg + "}"
            if text.startswith(value + "_"):
                return "{" + arg + "}" + text[len(value):]
        return text

    bound = []
    for step in steps:
        step = dict(step)
        if replay:
            for arg, value in args.items():
                step["item"] = step["item"].replace("{" + arg + "}", value)
                step["targets"] = step.get("targets", "").replace("{" + arg + "}", value)
            if step["verifies"] == check["fact"] and not isinstance(check["value"], bool):
                step["count"] = min(99, abs(int(check["value"]) - int(measured))) if isinstance(measured, int) else 0
        else:
            step["item"] = placeholder(step["item"])
            step["targets"] = " ".join(k + eq + placeholder(v) for k, eq, v in (c.partition("=") for c in step.get("targets", "").split()))
        bound.append(step)
    return bound


def from_intents(intents):
    """Skill steps from the leaf's dispatched intents (repeat.step_for), for a leaf the planner gave no steps.

    Moves are dropped: they only reach a target, which the repeat or decider picks again. A hotbar select is folded
    into the repeat step after it, since a repeat selects its own item."""
    intents = [step for step in intents if step["control"] != "move_to"]
    steps, selected = [], ""
    for index, step in enumerate(intents):
        if step["control"] == "select":
            selected = step["item"]
            if index + 1 < len(intents) and intents[index + 1].get("targets"):
                continue
        elif step.get("targets") and not step["item"]:
            step = {**step, "item": selected}
        steps.append(step)
    return repeat.steps_from(steps)


def tally(record, won, build_id, run_id):
    """Count one result and remember the builds a skill has worked on, so a later failure on a new build reads as a likely regression."""
    regression = not won and regressed(record, build_id)
    record["wins" if won else "fails"] += 1
    builds = record.setdefault("won_builds", [])
    if won and build_id and build_id not in builds:
        builds.append(build_id)
        del builds[:-MAX_WON_BUILDS]
    record["last_result"] = {"result": "won" if won else "failed", "build_id": build_id, "run_id": run_id,
                             "at": time.time(), "regression": regression}
    record["updated"] = time.time()
    return regression


def regressed(record, build_id):
    """Worked on some build, never on this one."""
    return bool(build_id and record.get("won_builds") and build_id not in record["won_builds"])


def summary(key, record):
    """The Skills page row for a stored skill record under its store key."""
    return {"id": key, "name": record["name"], "kind": record.get("scope") or "subgoal",
            "uses": record["wins"] + record["fails"], "wins": record["wins"], "fails": record["fails"],
            "last_result": record.get("last_result"), "updated": record.get("updated")}


def save(store, project_id, check, steps, won, build_id=None, run_id=None, key=None):
    """Count a win (replacing the steps when given) or a failure for the skill at key, by default the steps' key; returns the record."""
    if steps is None and key is None:
        raise ValueError("A failure needs the skill key")
    # Keyed by the bound steps, as stored, so a target named by a check arg keys like the skill's other values.
    key = key or skill_key(project_id, check, bind(steps, check))
    effect = "rises" if check["op"] == ">=" else "falls" if check["op"] == "<=" else "reaches the value"
    record = store.get("skills", key) or {
        "project_id": project_id, "name": name(check), "params": list(facts.FACTS[check["fact"]][0]),
        "effect": f"{check['fact']} {effect}", "verifies": check["fact"], "preconditions": [], "steps": [], "wins": 0, "fails": 0,
    }
    # A win by hand-picked actions never replaces steps that run a repeat: the repeat replays in one turn, the hand steps do not.
    if steps is not None and (any(s.get("targets") for s in steps) or not any(s.get("targets") for s in record["steps"])):
        record["steps"] = bind(steps, check)
        record["preconditions"] = list(dict.fromkeys(s["precondition"] for s in record["steps"] if s.get("precondition")))
    tally(record, won, build_id, run_id)
    return store.put("skills", record, key)


def find(store, project_id, check):
    """The usable skills for the check's shape in the project as [(key, record)], most wins first: each has steps and has not failed more often than it won."""
    found = [(key, record) for key, record in keyed(store, f"{project_id}:{shape(check)}:") if record["steps"] and record["fails"] <= record["wins"]]
    return sorted(found, key=lambda item: -item[1]["wins"])


def keyed(store, prefix):
    """Every skill record whose key starts with prefix, as (key, record)."""
    # substr, not LIKE: fact names contain _, a LIKE wildcard.
    with store.lock:
        rows = store.db.execute("SELECT id, data FROM records WHERE kind='skills' AND substr(id, 1, ?) = ?", (len(prefix), prefix)).fetchall()
    return [(key, json.loads(data)) for key, data in rows]


def signature(check, observation, held):
    """Where the player is, in terms that repeat across runs: the subgoal's shape, the actionable UI controls by node, the held item and the highlighted target's kind; no positions or ids."""
    entities = observation.get("entities", [])
    closest = (observation.get("state", {}).get("player") or {}).get("closest_target")
    return {"subgoal": shape(check),
            "screen": sorted({f"{e.get('parent', '')}/{(e.get('state') or {}).get('node_name') or str(e.get('label', ''))[:40]}"
                              for e in entities if e.get("kind") == "ui" and e.get("actions")}),
            "held": held or "",
            "target_kind": next((str(e.get("kind", "")) for e in entities if closest and e.get("id") == closest), "")}


def decision_key(project_id, sig):
    """The store key of a decision skill: the project plus a hash of the signature (a shape key always contains '(', so they never collide)."""
    return f"{project_id}:decision:" + hashlib.sha256(json.dumps(sig, sort_keys=True).encode()).hexdigest()[:16]


def save_decision(store, project_id, sig, check, steps, won, build_id=None, run_id=None):
    """Count a win (replacing the steps when given) or a failure for the decision skill of this situation; returns the record."""
    key = decision_key(project_id, sig)
    record = store.get("skills", key) or {"scope": "decision", "project_id": project_id, "name": "", "signature": sig, "steps": [], "wins": 0, "fails": 0}
    if won and steps:
        record["name"] = (f"{sig['subgoal']} on {len(sig['screen'])} controls: " + ", ".join(f"{s['control']} {s['targets']}".strip() for s in steps))[:160]
        record["steps"] = bind(steps, check)
    tally(record, won, build_id, run_id)
    return store.put("skills", record, key)


def find_decision(store, project_id, sig):
    """The usable decision skill for this situation: it has steps and has not failed more often than it won; else None."""
    record = store.get("skills", decision_key(project_id, sig))
    return record if record and record["steps"] and record["fails"] <= record["wins"] else None


def library(store, project_id):
    """The project's usable skills, subgoal and decision, as (key, record), most wins first; Jev picks among them. Plan patterns are not skills to replay."""
    with store.lock:
        rows = store.db.execute("SELECT id, data FROM records WHERE kind='skills' AND json_extract(data,'$.project_id')=?", (project_id,)).fetchall()
    usable = [(key, record) for key, record in ((k, json.loads(d)) for k, d in rows)
              if record.get("scope") != "plan" and record["steps"] and record["fails"] <= record["wins"]]
    return sorted(usable, key=lambda kr: -kr[1]["wins"])


def describe(record, chars):
    """The skill as text for Jev, cut to chars: a decision skill (it has a signature) by its screen, a subgoal skill by effect, needs and steps."""
    if "signature" in record:
        return f"{record['name']}. Screen: {', '.join(record['signature']['screen'])}"[:chars]
    steps = "; ".join(f"{s['control']} {s.get('item', '')} {s.get('targets', '')}".strip() for s in record["steps"])
    return f"{record['name']}: {record['effect']}. Needs: {', '.join(record['preconditions']) or 'nothing'}. Steps: {steps}"[:chars]


def plan_key(project_id, stages):
    """The store key of a plan pattern: the project plus a hash of its stages' check shapes, stable however the planner words the steps."""
    return f"{project_id}:plan:" + hashlib.sha256(json.dumps([shape(s["completion"]) for s in stages]).encode()).hexdigest()[:16]


def count_of(text):
    """The goal's count: the one distinct integer in text, else None ('Plant 5 wheat' is 5; '2 rows of 5' has no count)."""
    numbers = {int(n) for n in re.findall(r"\d+", text)}
    return numbers.pop() if len(numbers) == 1 else None


def pattern_stage(stage, args, count):
    """A won stage as a pattern: arg values become {arg}, the goal's count {count} in text and in the check's value,
    or +{count} when the stage asked for count more than the value at planning; other values stay literal."""
    def placeholder(text):
        for arg, value in sorted(args.items(), key=lambda kv: -len(kv[1])):  # wheat_seed before wheat
            text = text.replace(value, "{" + arg + "}")
        return text if count is None else re.sub(rf"\b{count}\b", "{count}", text)

    check = stage["completion"]
    completion = {**check, "description": placeholder(check["description"]),
                  "args": [{**a, "value": "{" + a["name"] + "}"} if isinstance(a["value"], str) and a["value"] else a for a in check["args"]]}
    value, planned = check["value"], stage.get("planned_value")
    if count is not None and type(value) is int:
        if value == count:
            completion["value"] = "{count}"
        elif type(planned) is int and value - planned == count:
            completion["value"] = "+{count}"
    return {"objective": placeholder(stage["objective"]), "completion": completion,
            "steps": bind(stage.get("steps") or [], check), "actionable": stage["actionable"]}


def save_plan(store, project_id, objective, stages, won, build_id=None, run_id=None, key=None):
    """Count a win (replacing the stages) or a failure for the plan pattern at key, by default the stages' key.

    Returns the record, or None when there is nothing to count: a failure of a deleted pattern, or a win whose stages
    name two values for one arg (a pattern binds one value per arg)."""
    key = key or plan_key(project_id, stages)
    record = store.get("skills", key)
    if won:
        args = {}
        for stage in stages:
            for arg in stage["completion"]["args"]:
                if isinstance(arg["value"], str) and arg["value"] and args.setdefault(arg["name"], arg["value"]) != arg["value"]:
                    return None
        count = count_of(objective)
        record = record or {"scope": "plan", "project_id": project_id, "wins": 0, "fails": 0}
        record["name"] = "plan: " + " → ".join(name(s["completion"]) if s["completion"]["fact"] in facts.FACTS else s["objective"][:40] for s in stages)
        record["params"] = sorted(args)
        record["stages"] = [pattern_stage(stage, args, count) for stage in stages]
        record["uses_count"] = "{count}" in json.dumps(record["stages"], ensure_ascii=False)
    elif record is None:
        return None
    tally(record, won, build_id, run_id)
    return store.put("skills", record, key)


def instantiate(record, args, count, values):
    """The pattern as a root expand decision for this goal: placeholders filled from args and count, +{count} read on values.

    Shaped like PlanningDecision; apply_decision validates it. A +{count} stage whose fact is not readable raises ValueError."""
    def fill(text):
        for arg, value in args.items():
            text = text.replace("{" + arg + "}", value)
        return text.replace("{count}", str(count))

    proposed = []
    for index, stage in enumerate(record["stages"]):
        completion = copy.deepcopy(stage["completion"])
        completion["description"] = fill(completion["description"])
        for arg in completion["args"]:
            if isinstance(arg["value"], str):
                arg["value"] = fill(arg["value"])
        if completion["value"] == "{count}":
            completion["value"] = count
        elif completion["value"] == "+{count}":
            current = facts.read(values, completion["fact"], facts.args_of(completion))
            if type(current) is not int or count is None:
                raise ValueError(f"Stage {index} needs {completion['fact']} now plus the count, and it is not readable")
            completion["value"] = current + count
        proposed.append({"key": f"s{index}", "parent_key": "", "objective": fill(stage["objective"]), "completion": completion,
                         "actionable": stage["actionable"], "steps": bind(stage["steps"], completion, replay=True)})
    return {"goal_id": "root", "mode": "expand", "completion": subgoals.unknown(), "reason": "Cached plan " + record["name"],
            "subgoals": proposed, "steps": []}


def plan_patterns(store, project_id):
    """The project's usable plan patterns as [(key, record)], most wins first: each has stages and has not failed more often than it won."""
    usable = [(key, record) for key, record in keyed(store, f"{project_id}:plan:") if record["stages"] and record["fails"] <= record["wins"]]
    return sorted(usable, key=lambda item: -item[1]["wins"])
