"""Code macros for repeated farming steps: each next action is one the observation offers, so a macro needs no new verbs."""
from __future__ import annotations

import math

from . import ai


# name: (tile predicate, verifying catalog fact, tool kind)
MACROS = {
    "till_area": (lambda tile: tile.get("tillable") and tile.get("state") == "Default" and not tile.get("plant"), "tiles_tilled", "hoe"),  # Core loops: till
    "plant_all": (lambda tile: tile.get("state") in ("Tilled", "Watered") and not tile.get("plant"), "crops_planted", "seed"),  # Core loops: select seed, plant
    "water_all": (lambda tile: tile.get("waterable") and tile.get("state") == "Tilled", "tiles_watered", "watering_can"),  # Core loops: water
    "harvest_all": (lambda tile: tile.get("harvestable"), "crops_harvestable", "hand"),  # Core loops: harvest; crops_harvestable falls while item_count rises
}
USE_RANGE = 64  # Pixels from the player within which use_item is sent without moving onto the tile first.


def select_input(observation, item_id):
    """The slot_N input of the hotbar entry holding item_id, or None when the hotbar does not hold it."""
    for entry in observation.get("state", {}).get("player", {}).get("hotbar") or []:
        if isinstance(entry, dict) and entry.get("item_id") == item_id and isinstance(entry.get("input"), str):
            return entry["input"]
    return None


def _offered(context, verb, parameters):
    """The action action_groups offers with these parameters on this observation, or None."""
    return ai.resolve(context, ai.option_key({"tool": "perform_action", "args": {"verb": verb, "parameters": parameters}}))


def next_action(macro, step, context, selected, tried):
    """(action, why) for the macro's next action; tried holds tile positions as tuples already used this run."""
    observation = context["observation"]
    state = observation.get("state", {})
    if not selected and step.get("item"):  # A step with no item (e.g. harvest by hand) has nothing to select.
        slot = select_input(observation, step.get("item"))
        action = slot and _offered(context, "input", {"name": slot, "duration_ms": 160})  # Same parameters as ai.action_groups' ordinary inputs.
        return (action, "select") if action else (None, "not_offered")
    here = state.get("player", {}).get("position")
    if not ai.point(here):
        return None, "not_offered"
    matches = [t for t in state.get("tiles", []) if ai.point(t.get("position")) and MACROS[macro][0](t) and tuple(t["position"]) not in tried]
    if not matches:
        return None, "not_offered"
    position = min(matches, key=lambda t: math.dist(here, t["position"]))["position"]
    if math.dist(here, position) > USE_RANGE:
        action, why = _offered(context, "move_to", {"position": position}), "move"
    else:
        action, why = _offered(context, "input", {"name": "use_item", "world_position": position, "duration_ms": 500}), "use"
    return (action, why) if action else (None, "not_offered")


def step_for(payload, observation, goal_fact=""):
    """The step one dispatched perform_action payload took, read against the observation it was chosen from.

    A tile several macros match is read as the macro verifying goal_fact, else the first in MACROS order."""
    parameters = payload.get("parameters") or {}
    name = parameters.get("name")
    step = {"control": name if payload.get("verb") == "input" else payload.get("verb", ""), "item": "", "count": 1, "precondition": "", "verifies": "unknown"}
    if name == "use_item" and not payload.get("target_id"):
        tile = next((t for t in observation.get("state", {}).get("tiles", []) if t.get("position") == parameters.get("world_position")), None)
        # A tilled empty tile matches both plant_all and water_all; the goal's own fact decides which the use was.
        found = [m for m, (matches, _, _) in MACROS.items() if tile and matches(tile)]
        macro = next((m for m in found if MACROS[m][1] == goal_fact), found[0] if found else None)
        if macro:
            step.update(control=macro, verifies=MACROS[macro][1])
    elif isinstance(name, str) and name.startswith("slot_"):
        hotbar = observation.get("state", {}).get("player", {}).get("hotbar") or []
        item = next((e.get("item_id") for e in hotbar if isinstance(e, dict) and e.get("input") == name), "")
        step.update(control="select", item=item if isinstance(item, str) else "")
    return step


def steps_from(intents):
    """Collapse runs of the same step (control and item) into one step, summing count."""
    steps = []
    for step in intents:
        if steps and (steps[-1]["control"], steps[-1]["item"]) == (step["control"], step["item"]):
            steps[-1]["count"] += step["count"]
        else:
            steps.append(dict(step))
    return steps
