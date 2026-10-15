"""Recipes from wins: a completed leaf's steps, saved under its completion shape and replayed on the next leaf of that shape."""
from __future__ import annotations

import time

from . import macros


def shape(check):
    """The completion's fact, arg names and operator, e.g. crops_planted(plant_id)>=; arg values and targets vary between leaves."""
    return f"{check['fact']}({','.join(sorted(a['name'] for a in check['args']))}){check['op']}"


def bind(steps, check, replay=False, measured=None):
    """Steps with the check's arg values as {arg} placeholders in item (save), or substituted back (replay).

    On replay, a step verifying the check's own fact gets the count still needed: the target minus the value
    measured at activation, or 0 (until the check is met) when that was not readable."""
    args = {a["name"]: str(a["value"]) for a in check["args"]}
    bound = []
    for step in steps:
        step = dict(step)
        if replay:
            step["item"] = next((value for name, value in args.items() if step["item"] == "{" + name + "}"), step["item"])
            if step["verifies"] == check["fact"] and not isinstance(check["value"], bool):
                step["count"] = min(99, abs(int(check["value"]) - int(measured))) if isinstance(measured, int) else 0
        else:
            step["item"] = next(("{" + name + "}" for name, value in args.items() if step["item"] == value), step["item"])
        bound.append(step)
    return bound


def from_intents(intents):
    """Recipe steps from the leaf's dispatched intents (macros.step_for), for a leaf the planner gave no steps.

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


def save(store, configuration_id, check, steps, won):
    """Count a win (replacing the steps when given) or a failure for the check's shape; returns the record."""
    key = f"{configuration_id}:{shape(check)}"
    record = store.get("recipes", key) or {"shape": shape(check), "steps": [], "wins": 0, "fails": 0}
    if steps is not None:
        record["steps"] = bind(steps, check)
    record["wins" if won else "fails"] += 1
    record["updated"] = time.time()
    return store.put("recipes", record, key)


def find(store, configuration_id, check):
    """The usable recipe for the check's shape: it has steps and has not failed more often than it won; else None."""
    record = store.get("recipes", f"{configuration_id}:{shape(check)}")
    return record if record and record["steps"] and record["fails"] <= record["wins"] else None
