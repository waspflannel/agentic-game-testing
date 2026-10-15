"""General repeat: one step's control on every observed target matching its field=value conditions, nearest first, chosen only from offered options."""
from __future__ import annotations

import math

from . import ai


USE_RANGE = 64  # Pixels from the player within which an action aimed at a world_position is sent without moving onto the target first.


def view(group):
    """The fields a target condition reads: the tile, the entity with its state merged in, or None for any other group."""
    if "tile" in group:
        return group["tile"]
    entity = group.get("evidence")
    if entity is None:
        return None
    return {**(entity.get("state") or {}), **{k: v for k, v in entity.items() if k != "state"}}


def key(group):
    """The entity id or tile position that identifies a target across observations."""
    return group["evidence"]["id"] if "evidence" in group else tuple(group["tile"]["position"])


def action_key(action):
    """The target one action aims at, in the same form as key(), or None when it aims at nothing."""
    args = action.get("args", {})
    if args.get("target_id"):
        return args["target_id"]
    position = (args.get("parameters") or {}).get("world_position")
    return tuple(position) if position is not None else None


def _text(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def matches(target, targets):
    """Whether every field=value condition holds; a missing field reads as "", so plant= means no plant. No conditions match nothing."""
    conditions = [c.partition("=") for c in targets.split()]
    return bool(conditions) and all(eq and _text(target.get(k)).lower() == v.lower() for k, eq, v in conditions)


def describe(entity, tile):
    """Conditions for a step rebuilt from one dispatched action, naming the entity's kind (and a UI control's node) or the tile's state, plant and true flags."""
    if entity and entity.get("kind") == "ui":
        # A UI control is the same node across runs, while its id and label are not. A value with whitespace cannot be one field=value token, so it is dropped.
        node = {"parent": _text(entity.get("parent")), "node_name": _text((entity.get("state") or {}).get("node_name"))}
        return "kind=ui" + "".join(f" {k}={v}" for k, v in node.items() if not any(c.isspace() for c in v))
    if entity:
        return f"kind={_text(entity.get('kind'))}"
    flags = "".join(f" {k}=true" for k, v in tile.items() if v is True and k != "ground")
    return f"state={_text(tile.get('state'))} plant={_text(tile.get('plant'))}" + flags


def select_input(observation, item_id):
    """The slot_N input of the hotbar entry holding item_id, or None when the hotbar does not hold it."""
    for entry in observation.get("state", {}).get("player", {}).get("hotbar") or []:
        if isinstance(entry, dict) and entry.get("item_id") == item_id and isinstance(entry.get("input"), str):
            return entry["input"]
    return None


def next_action(step, context, state):
    """(action, why) for the step's next action; state is {"select", "tried", "skipped", "held"}, owned by the caller across one run."""
    observation = context["observation"]
    if step.get("item") and not state["select"] and step["item"] == state.get("held", ""):
        # The held slot is already selected, and re-pressing it is a no-op (#62).
        state["select"] = "held"
    if step.get("item") and not state["select"]:
        slot = select_input(observation, step["item"])
        action = slot and ai.resolve(context, ai.option_key({"tool": "perform_action", "args": {"verb": "input", "parameters": {"name": slot, "duration_ms": 160}}}))
        state["select"] = "pressed" if action else "not_on_hotbar"
        if action:
            state["held"] = step["item"]
            return action, "select"
        # Use whatever is equipped instead; a wrong equipped item shows as two no-effect uses, which stop the run.
    here = observation.get("state", {}).get("player", {}).get("position")
    candidates = []
    for group in ai.action_groups(context):
        target = view(group)
        if not target or key(group) in state["tried"] or not matches(target, step["targets"]):
            continue
        # A UI control has a rect, not a position, so it counts at distance 0, even on a menu with no player yet.
        if target.get("kind") == "ui":
            candidates.append((0, group))
        elif ai.point(target.get("position")) and ai.point(here):
            candidates.append((math.dist(here, target["position"]), group))
    if not candidates and not ai.point(here):
        return None, "not_offered"
    wanted = step.get("parameters") or {}
    for distance, group in sorted(candidates, key=lambda c: c[0]):
        actions = group["actions"]
        # A stored ui_click keeps its button and a ui_scroll its direction.
        use = next((a for a in actions if a["args"].get("verb") != "move_to"
                    and step["control"] in (a["args"].get("verb"), a["args"].get("parameters", {}).get("name"))
                    and all(a["args"].get("parameters", {}).get(k) == v for k, v in wanted.items())), None)
        move = next((a for a in actions if a["args"].get("verb") == "move_to"), None)
        if move and (not use or ("world_position" in use["args"]["parameters"] and distance > USE_RANGE)):
            return move, "move"
        if use:
            return use, "use"
        # A target whose action is not offered here is skipped, not a stop.
        state["tried"].add(key(group))
        state["skipped"] = True
    return None, "not_offered" if state["skipped"] else "done"


def step_for(payload, observation):
    """The step one dispatched perform_action payload took, read against the observation it was chosen from."""
    parameters = payload.get("parameters") or {}
    name = parameters.get("name")
    verb = payload.get("verb", "")
    step = {"control": name if verb == "input" else verb, "item": "", "count": 1, "precondition": "", "verifies": "unknown", "targets": ""}
    if verb in ("ui_click", "ui_scroll"):
        step["parameters"] = {k: v for k, v in parameters.items() if k in ("button", "direction")}
    if isinstance(name, str) and name.startswith("slot_"):
        hotbar = observation.get("state", {}).get("player", {}).get("hotbar") or []
        item = next((e.get("item_id") for e in hotbar if isinstance(e, dict) and e.get("input") == name), "")
        step.update(control="select", item=item if isinstance(item, str) else "")
    elif verb != "move_to":
        entity = next((e for e in observation.get("entities", []) if payload.get("target_id") and e.get("id") == payload["target_id"]), None)
        tile = None if entity else next((t for t in observation.get("state", {}).get("tiles", []) if t.get("position") == parameters.get("world_position")), None)
        if entity or tile:
            step["targets"] = describe(entity, tile)
    return step


def ordinary(step, context):
    """The offered input action of an untargeted step whose control is an input name, not select or a verb; None when not offered."""
    if step["targets"] or step["control"] == "select" or step["control"] in ai.GAME_VERBS:
        return None
    return ai.resolve(context, ai.option_key({"tool": "perform_action", "args": {"verb": "input", "parameters": {"name": step["control"], "duration_ms": 160}}}))


def steps_from(intents):
    """Collapse runs of the same step (control, item and targets) into one step, summing count."""
    steps = []
    for step in intents:
        if steps and (steps[-1]["control"], steps[-1]["item"], steps[-1]["targets"]) == (step["control"], step["item"], step["targets"]):
            steps[-1]["count"] += step["count"]
        else:
            steps.append(dict(step))
    return steps
