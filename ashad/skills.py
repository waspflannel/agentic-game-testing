"""Skill library from wins: a leaf's steps as a general pattern over its completion fact's args, shared by every configuration of a project."""
from __future__ import annotations

import hashlib
import json
import time

from . import facts, repeat

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
    return {"id": key, "name": record["name"], "kind": "decision" if record.get("scope") == "decision" else "subgoal",
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
    prefix = f"{project_id}:{shape(check)}:"
    # substr, not LIKE: fact names contain _, a LIKE wildcard.
    with store.lock:
        rows = store.db.execute("SELECT id, data FROM records WHERE kind='skills' AND substr(id, 1, ?) = ?", (len(prefix), prefix)).fetchall()
    found = [(key, record) for key, record in ((key, json.loads(data)) for key, data in rows)
             if record["steps"] and record["fails"] <= record["wins"]]
    return sorted(found, key=lambda item: -item[1]["wins"])


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
