"""Typed fact catalog: the only facts a planner completion check may name, and how to read and compare them."""
from __future__ import annotations

import re

# stock: changes only by the player's own actions; state: rises and falls on its own (tiles decay, harvest removes plants). item_count is the held count (inventory plus hotbar), not a gathered total.
FACTS = {
    "world_ready": ((), "bool", "state"),
    "player_can_act": ((), "bool", "state"),
    "item_count": (("item_id",), "int", "stock"),
    "quest_completed": (("quest_id",), "bool", "stock"),
    "quest_requirement_progress": (("quest_id", "index"), "int|bool", "stock"),
    "tiles_tilled": ((), "int", "state"),
    "tiles_watered": ((), "int", "state"),
    "crops_planted": (("plant_id",), "int", "state"),
    "crops_harvestable": (("plant_id",), "int", "state"),
    "crop_growth_stage_min": (("plant_id",), "int", "state"),
    "delivered_to": (("target", "item_id"), "int", "stock"),
}
ZERO_DEFAULT = {"item_count", "delivered_to", "crops_planted", "crops_harvestable"}
PLANT_FACTS = ("crops_planted", "crops_harvestable", "crop_growth_stage_min")
BRIDGE_SCHEMA = 2
# Planners see delivered_to as container_contents (what a target container holds now, not items the player took back); stored checks and bridge values keep delivered_to.
SHOWN = {"delivered_to": "container_contents"}
STORED = {shown: name for name, shown in SHOWN.items()}


def args_of(check):
    return {a["name"]: a["value"] for a in check.get("args", [])}


def _progress(value):
    if isinstance(value, (bool, int)):
        return value
    if value == "checked" or value == "unchecked":
        return value == "checked"
    match = re.fullmatch(r"\s*(\d+)\s*/\s*\d+\s*", value) if isinstance(value, str) else None
    return int(match.group(1)) if match else None


def values(observation):
    """{fact: value tree}: the bridge's facts when it sends schema 2, else what the observation alone can prove."""
    state = observation.get("state", {})
    bridge = state.get("facts")
    if isinstance(bridge, dict) and bridge.get("schema_version") == BRIDGE_SCHEMA:
        result = {k: v for k, v in bridge.items() if k != "schema_version"}
    else:
        result = _observed(observation, state)
    # world_ready means playable: the world loaded, a player is present and is not known to be unable to act. The bridge
    # reports a loaded world while a full-screen flow such as character creation is still up; there player_can_act is false.
    result["world_ready"] = (observation.get("ready") is True and result.get("world_ready") is True
                             and isinstance(state.get("player"), dict) and result.get("player_can_act") is not False)
    if "player_can_act" in result:
        result["player_can_act"] = result["world_ready"]  # a false player_can_act already made world_ready false
    return result


def _observed(observation, state):
    from .subgoals import held_counts  # subgoals imports facts

    result = {"world_ready": observation.get("ready") is True}
    activity = state.get("player", {}).get("activity")
    if isinstance(activity, str):
        result["player_can_act"] = result["world_ready"] and activity != "input_disabled"
    held = held_counts(observation)
    if held is not None:
        result["item_count"] = held
    quests = [q for q in state.get("objectives", []) if isinstance(q.get("id"), str)]
    result["quest_completed"] = {q["id"]: False for q in quests}
    result["quest_completed"].update({q: True for q in state.get("completed_quests", [])})
    result["quest_requirement_progress"] = {q["id"]: [_progress(r.get("progress")) for r in q.get("requirements") or []] for q in quests}
    return result


def read(values, fact, args):
    """The fact's current value for args; None when not readable, 0 for absent keys of count facts."""
    fact = STORED.get(fact, fact)
    shown = SHOWN.get(fact, fact)
    if fact not in values and shown not in values:
        return None
    value = values[fact] if fact in values else values[shown]
    for name in FACTS[fact][0]:
        key = args.get(name)
        if isinstance(value, dict) and key in value:
            value = value[key]
        elif isinstance(value, list) and type(key) is int and 0 <= key < len(value):
            value = value[key]
        else:
            return 0 if fact in ZERO_DEFAULT else None
    return value


def arg_values(current, item_ids):
    """The legal values of each arg kind, from the run's item ids and the current fact values (at a menu, before any fact is readable, from the item ids alone)."""
    delivered = current.get("delivered_to", current.get(SHOWN["delivered_to"], {}))
    return {
        "item_id": sorted(set(item_ids) | set(current.get("item_count", {})) | {i for items in delivered.values() for i in items}),
        "quest_id": sorted(set(current.get("quest_completed", {})) | set(current.get("quest_requirement_progress", {}))),
        "plant_id": sorted({p for fact in PLANT_FACTS for p in current.get(fact, {})} | {i for i in item_ids if i.endswith("_seed")}),
        "target": list(delivered),
        "index": "integer index into the quest's requirements",
    }


def catalog(observation, item_ids):
    """The planner-facing catalog under shown names: readable facts, the legal values of each arg, and current values."""
    current = values(observation)
    ids = arg_values(current, item_ids)
    facts = {SHOWN.get(name, name): {"args": list(args), "type": kind, "tag": tag} for name, (args, kind, tag) in FACTS.items() if name in current}
    used = {a for f in facts.values() for a in f["args"]}
    return {"facts": facts, "args": {k: v for k, v in ids.items() if k in used}, "values": {SHOWN.get(k, k): v for k, v in current.items()}}


def validate(check, catalog, location):
    def fail(why):
        raise ValueError(f"{why} (at {location})")

    # A shown name is stored as its fact name, so measuring and saved patterns read one key.
    check["fact"] = STORED.get(check["fact"], check["fact"])
    fact, args = SHOWN.get(check["fact"], check["fact"]), check.get("args", [])
    if fact == "unknown":
        if args:
            fail("unknown takes no args")
        return
    if fact not in catalog["facts"]:
        fail(f"fact {fact!r} is not in fact_catalog")
    names = [a["name"] for a in args]
    if sorted(names) != sorted(catalog["facts"][fact]["args"]):
        fail(f"{fact} needs args {catalog['facts'][fact]['args']}, got {names}")
    chosen = args_of(check)
    for name, value in chosen.items():
        if name == "index":
            count = len(catalog["values"][fact].get(chosen.get("quest_id"), []))
            if type(value) is not int or not 0 <= value < count:
                fail(f"index {value!r} is not an integer below {count}")
        elif value not in catalog["args"].get(name, []):
            fail(f"{name} {value!r} is not in fact_catalog args")
    kind, value = catalog["facts"][fact]["type"], check["value"]
    if kind == "int|bool":
        current = read(catalog["values"], fact, chosen)
        kind = "bool" if type(current) is bool else "int"
    if kind == "bool" and (type(value) is not bool or check["op"] != "=="):
        fail(f"{fact} needs a bool value with op ==")
    if kind == "int" and type(value) is not int:
        fail(f"{fact} needs an integer value")


def met(check, value):
    target = int(check["value"])
    return {">=": int(value) >= target, "==": int(value) == target, "<=": int(value) <= target}[check["op"]]
