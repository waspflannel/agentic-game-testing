"""Skill library from wins: a leaf's steps as a general pattern over its completion fact's args, shared by every configuration of a project."""
from __future__ import annotations

import time

from . import facts, macros

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


def name(check):
    """Display name, e.g. plant(<seed>) for crops_planted, else the fact form crops_planted(<plant_id>)>=."""
    return VERBS.get(check["fact"]) or f"{check['fact']}({', '.join('<' + a + '>' for a in facts.FACTS[check['fact']][0])}){check['op']}"


def bind(steps, check, replay=False, measured=None):
    """Steps with the check's string arg values as {arg} placeholders in item (save), or substituted back (replay).

    On save, an item equal to a value becomes {arg} and an item starting with value_ keeps its suffix
    (wheat_seed with item_id=wheat becomes {item_id}_seed). On replay, a step verifying the check's own fact
    gets the count still needed: the target minus the value measured at activation, or 0 (until the check is
    met) when that was not readable."""
    args = {a["name"]: a["value"] for a in check["args"] if isinstance(a["value"], str)}
    bound = []
    for step in steps:
        step = dict(step)
        if replay:
            for arg, value in args.items():
                step["item"] = step["item"].replace("{" + arg + "}", value)
            if step["verifies"] == check["fact"] and not isinstance(check["value"], bool):
                step["count"] = min(99, abs(int(check["value"]) - int(measured))) if isinstance(measured, int) else 0
        else:
            for arg, value in args.items():
                if step["item"] == value:
                    step["item"] = "{" + arg + "}"
                    break
                if step["item"].startswith(value + "_"):
                    step["item"] = "{" + arg + "}" + step["item"][len(value):]
                    break
        bound.append(step)
    return bound


def from_intents(intents):
    """Skill steps from the leaf's dispatched intents (macros.step_for), for a leaf the planner gave no steps.

    Moves are dropped: they only reach a target, which the macro or decider picks again. A hotbar select is folded
    into the macro step after it, since a macro selects its own item."""
    intents = [step for step in intents if step["control"] != "move_to"]
    steps, selected = [], ""
    for index, step in enumerate(intents):
        if step["control"] == "select":
            selected = step["item"]
            if index + 1 < len(intents) and intents[index + 1]["control"] in macros.MACROS:
                continue
        elif step["control"] in macros.MACROS and not step["item"]:
            step = {**step, "item": selected}
        steps.append(step)
    return macros.steps_from(steps)


def save(store, project_id, check, steps, won):
    """Count a win (replacing the steps when given) or a failure for the check's shape in the project; returns the record."""
    key = f"{project_id}:{shape(check)}"
    effect = "rises" if check["op"] == ">=" else "falls" if check["op"] == "<=" else "reaches the value"
    record = store.get("skills", key) or {
        "project_id": project_id, "name": name(check), "params": list(facts.FACTS[check["fact"]][0]),
        "effect": f"{check['fact']} {effect}", "verifies": check["fact"], "preconditions": [], "steps": [], "wins": 0, "fails": 0,
    }
    # A win by hand-picked actions never replaces steps that run a macro: the macro replays in one turn, the hand steps do not.
    if steps is not None and (any(s["control"] in macros.MACROS for s in steps) or not any(s["control"] in macros.MACROS for s in record["steps"])):
        record["steps"] = bind(steps, check)
        record["preconditions"] = list(dict.fromkeys(s["precondition"] for s in record["steps"] if s.get("precondition")))
    record["wins" if won else "fails"] += 1
    record["updated"] = time.time()
    return store.put("skills", record, key)


def find(store, project_id, check):
    """The usable skill for the check's shape in the project: it has steps and has not failed more often than it won; else None."""
    record = store.get("skills", f"{project_id}:{shape(check)}")
    return record if record and record["steps"] and record["fails"] <= record["wins"] else None
