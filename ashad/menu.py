"""The game-shaped option menu: every option is one complete action bound to an observed target; ai.py only asks an engine to pick from it."""
from __future__ import annotations

import copy
import json
import math
import re


GAME_VERBS = ("move_to", "interact", "input", "ui_click", "ui_drag", "ui_text", "ui_scroll", "fish_follow")  # worker/models.py GameAction.verb
STRUCTURED_TOOLS = ("observe", "explain_objective", "list_entities", "inspect_entity", "list_actions",
                    "get_action_status", "cancel_action", "read_changes", "wait_for_change", "perform_action")
SNAPSHOT_CHARS = 28000  # The per-subgoal snapshot and the view its delta is compared in.
FARM_FACTS = ("tiles_tilled", "tiles_watered", "crops_planted", "crops_harvestable", "crop_growth_stage_min")
BACK = re.compile(r"(?<![a-z])back(button)?(?![a-z])", re.I)  # "BACK", "BackButton", "go_back"; not "Backpack" or "feedback".
RESUME = re.compile(r"(?<![a-z])resume(?![a-z])", re.I)


def point(value):
    return isinstance(value, list) and len(value) == 2 and all(type(n) in (int, float) and math.isfinite(n) for n in value)


def names(entity):
    return f"{entity.get('label', '')} {(entity.get('state') or {}).get('node_name', '')}"


def pause_menu_open(observation):
    """The game's pause menu shows: a clickable Resume control is observed (Agronauts' RESUME GAME)."""
    return any("ui_click" in e.get("actions", []) and RESUME.search(names(e)) for e in observation.get("entities", []))


def action_groups(context):
    """Bind every option to one complete, observed action before inference.

    Outside a pause menu, exit_menu and Back controls are not offered: there exit_menu opens the pause menu and Back leaves the flow."""
    observation = context["observation"]
    state = observation.get("state", {})
    player_position = state.get("player", {}).get("position")
    paused = pause_menu_open(observation)
    entities = [e for e in observation.get("entities", []) if isinstance(e.get("id"), str) and type(e.get("version")) is int
                and (paused or not BACK.search(names(e)))]
    inputs = {n for n in observation.get("inputs", []) if paused or not (n == "exit_menu" or BACK.search(n))}
    groups = []

    def action(verb, parameters, target=None, label=""):
        args = {"verb": verb, "parameters": parameters}
        if target:
            args.update(target_id=target["id"], target_version=target["version"])
        return {"tool": "perform_action", "args": args, "description": label or f"{verb}: {parameters}"}

    for entity in entities:
        actions = []
        advertised = entity.get("actions", [])
        label = str(entity.get("label", entity["id"]))[:200]
        detail = entity.get("state") or {}
        if detail.get("node_name"):
            label = f"{detail['node_name']} ({label})"[:240]
        position = entity.get("position")
        if "move_to" in advertised and point(position) and point(player_position):
            distance = math.dist(player_position, position)
            if distance <= 480:
                actions.append(action("move_to", {}, entity, f"Move within interaction range of {label}"))
            else:
                waypoint = [round(a + (b-a) * 400 / distance, 2) for a, b in zip(player_position, position)]
                actions.append(action("move_to", {"position": waypoint}, label=f"Move 400 pixels toward observed {label}"))
        if "interact" in advertised and state.get("player", {}).get("closest_target") == entity["id"]:
            actions.append(action("interact", {}, entity, f"Interact with highlighted {label}"))
        if "fish_follow" in advertised:
            actions.append(action("fish_follow", {}, entity, "Follow the visible fishing wheel with normal directional input"))
        if "ui_click" in advertised:
            actions.append(action("ui_click", {"button": "left"}, entity, f"Left-click {label}"))
            actions.append(action("ui_click", {"button": "right"}, entity, f"Right-click {label}"))
        if "ui_scroll" in advertised:
            for direction in (-1, 1):
                actions.append(action("ui_scroll", {"direction": direction}, entity, f"Scroll {label} {'up' if direction < 0 else 'down'}"))
        if "ui_text" in advertised:
            values = {"player name": context.get("player", {}).get("name"), "player identity": context.get("player", {}).get("identity"),
                      "world name": context.get("world_namespace"), "host address": context.get("transport", {}).get("host_address"),
                      "port": context.get("transport", {}).get("port"), "seed": context.get("seed")}
            limit = detail.get("max_length", 0)
            limit = min(128, limit) if type(limit) is int and limit > 0 else 128
            offered = set()
            for source, value in values.items():
                text = str(value)[:limit] if value is not None else ""
                if text and text != detail.get("text") and text not in offered:
                    offered.add(text)
                    actions.append(action("ui_text", {"text": text}, entity,
                                          f"Replace {label} with configured {source} {text!r}; typing does not click Confirm"))
        if "use_item" in inputs and point(position):
            actions.append(action("input", {"name": "use_item", "world_position": position, "duration_ms": 500}, entity,
                                  f"Use equipped item aimed at observed {label}; check equipped tool and range first"))
        if actions or "ui_drag" in advertised:
            detail = {k: entity[k] for k in ("kind", "tooltip", "state") if k in entity}
            description = f"{label[:100]} in {entity.get('parent', '')} at {entity.get('position', entity.get('rect', ''))}; {json.dumps(detail, ensure_ascii=False)[:300]}"
            groups.append({"description": description, "evidence": entity, "actions": actions, "subject": label,
                           "drag_source": entity if "ui_drag" in advertised else None})

    for tile in state.get("tiles", []):
        position = tile.get("position")
        if not point(position):
            continue
        actions = []
        if tile.get("ground") and point(player_position) and math.dist(player_position, position) <= 480:
            actions.append(action("move_to", {"position": position}, label=f"Move to observed ground tile {position}"))
        for name in ("use_item", "fish"):
            if name in inputs:
                actions.append(action("input", {"name": name, "world_position": position, "duration_ms": 500},
                                      label=f"{name} at observed tile {position}; use only with the appropriate equipped item"))
        if actions:
            groups.append({"description": f"Visible tile: {json.dumps(tile)}", "subject": str(position), "actions": actions, "tile": tile})

    ordinary = [action("input", {"name": name, "duration_ms": 160}, label=f"Press advertised input {name}") for name in sorted(inputs)]
    if ordinary:
        groups.append({"description": "Normal keyboard inputs: " + ", ".join(sorted(inputs)), "actions": ordinary})
    groups.append({"description": "Wait for production, growth, animation, loading, votes, or state changes", "actions": [
        {"tool": "wait_for_change", "args": {"seconds": seconds}, "description": f"Wait up to {seconds} seconds for meaningful state change"} for seconds in (2, 10, 30)]})
    if not context.get("bug_already_reported"):
        groups.append({"description": "The game itself misbehaved: an offered action completed but the game then contradicts what it showed or promised, or it renders or responds wrongly",
                       "actions": [{"tool": "report_game_bug", "args": {}, "description": "Report a suspected game bug (not a missing action)"}]})
    groups.append({"description": "No suitable supported action; request operator instructions instead of silently getting stuck", "actions": [
        {"tool": "needs_instructions", "args": {}, "description": "Pause: the available action candidates do not cover this situation"}]})
    return groups


def _observed_targets(observation):
    entities, seen = [], set()
    for entity in observation.get("entities", []):
        if isinstance(entity.get("id"), str) and entity["id"] not in seen and len(entities) < 60:
            seen.add(entity["id"])
            entities.append(entity)
    return entities


def capability_summary(observation):
    """What the interface offered at the stuck moment, for the issue record."""
    return {"structured_tools": list(STRUCTURED_TOOLS), "game_verbs": list(GAME_VERBS),
            "observed_inputs": list(observation.get("inputs", [])),
            "candidate_actions": [action["description"][:300] for group in action_groups({"observation": observation}) for action in group["actions"]][:60]}


def prompt_observation(observation, max_chars=28000):
    result = copy.deepcopy(observation)
    entities = result.pop("entities", [])
    result.pop("known_locations", None)
    result["entities"] = []
    # Interactive targets before decorative labels, and slots holding an item before empty ones, so a
    # late display (a ship's copy of the hotbar) is not cut behind a grid of empty slots. Preserve full state in evidence.
    for entity in sorted(entities, key=lambda e: (not e.get("actions"), not (e.get("state") or {}).get("item_id"))):
        if len(json.dumps(result)) + len(json.dumps(entity)) > max_chars:
            result["complete"] = False
            result["context_truncated"] = True
            break
        result["entities"].append(entity)
    return result


def option_key(action):
    """Identity of an offered action across observations; versions and descriptions change while the target stays the same."""
    args = action.get("args", {})
    return json.dumps([action["tool"], args.get("verb"), args.get("target_id", ""), args.get("parameters", {})], sort_keys=True)


def resolve(context, key):
    """The action offered on this observation with the given option_key, or None when it is no longer offered."""
    return next((a for g in action_groups(context) for a in g["actions"] if option_key(a) == key), None)


def relevant_groups(groups, context):
    """At most 40 targets that matter to the active goal, then the fixed groups and a way to see every target."""
    player = context["observation"].get("state", {}).get("player", {})
    here = player.get("position")
    distance = lambda position: math.dist(here, position) if point(here) and point(position) else math.inf
    fact = ((context.get("current_goal") or {}).get("completion") or {}).get("fact")
    # Item, plant, quest and target ids named by the goal path's checks, and the items the player can equip.
    goal_ids = {a.get("value") for g in context.get("goal_path", []) for a in (g.get("completion") or {}).get("args", [])}
    held = {item.get("item_id") for item in player.get("hotbar") or [] if isinstance(item, dict)}
    entities = [g for g in groups if g.get("evidence")]
    tiles = [g for g in groups if g.get("tile")]
    fixed = [g for g in groups if not g.get("evidence") and not g.get("tile")]
    ui = [g for g in entities if g["evidence"].get("kind") == "ui" and g["evidence"].get("actions")]

    def naming(wanted):
        wanted = {w for w in wanted if isinstance(w, str) and w}
        return [g for g in entities if any(w in f"{g['evidence']['id']} {g['evidence'].get('label', '')} {json.dumps(g['evidence'].get('state'))}" for w in wanted)]
    near_tiles = sorted(tiles, key=lambda g: distance(g["tile"]["position"]))[:24] if fact in FARM_FACTS else []
    nearest = sorted(entities, key=lambda g: distance(g["evidence"].get("position")))
    from . import repeat  # Local: repeat imports this module.
    targets = next((s["targets"] for s in (context.get("current_goal") or {}).get("steps") or [] if s.get("targets")), "")
    if targets:  # The leaf's repeat step only needs its own targets and the closest entities.
        near_tiles = sorted((g for g in tiles + entities if repeat.matches(repeat.view(g), targets)),
                            key=lambda g: distance(repeat.view(g).get("position")))[:12]
        nearest = nearest[:8]
    # Targets naming the goal's own ids come first: a UI listed in tree order (money digits, the HUD hotbar)
    # must not crowd out the one slot that can make the delivery. First place wins; each group once.
    chosen = list({id(g): g for g in naming(goal_ids) + ui + naming(held) + near_tiles + nearest}.values())[:40]
    show_all = {"tool": "show_all_targets", "args": {}, "description": "Show every observed target"}
    return chosen + fixed + [{"description": "Target not listed: show every observed target", "actions": [show_all]}]


def listed(group, index):
    """Target name, then its actions as index.action IDs saying "it" for that name, then target detail: trimming cuts detail, never identity."""
    if group.get("drag_source"):
        return f"{index}.0) Drag source; its destinations are offered after you choose it. {group['description']}"
    subject = group.get("subject")
    text = f"Target {subject[:100]}. Actions:" if subject else "Actions:"
    for i, a in enumerate(group["actions"]):
        described = re.sub(rf"(?<!\w){re.escape(subject)}(?!\w)", "it", a["description"]) if subject else a["description"]
        text += f" {index}.{i}) {described};"
    return text + " Detail: " + group["description"]


def batch_options(context):
    """(groups, listings): the goal-relevant action groups and one listing per group, in the same order."""
    groups = relevant_groups(action_groups(context), context)
    return groups, [listed(g, i) for i, g in enumerate(groups)]


def observation_delta(snapshot, observation):
    """What changed since the subgoal snapshot: changed state and player keys, tiles by position, and entities by id. No snapshot means everything is new."""
    snapshot = snapshot or {}

    def changed(old, new):
        return {k: new.get(k) for k in sorted(old.keys() | new.keys()) if json.dumps(old.get(k), sort_keys=True) != json.dumps(new.get(k), sort_keys=True)}

    def keyed(source):
        # Versions change on every tick and mean nothing to the model.
        return {e["id"]: {k: v for k, v in e.items() if k != "version"} for e in source.get("entities", []) if isinstance(e.get("id"), str)}

    def tiles(source):
        return {json.dumps(t.get("position")): t for t in source.get("state", {}).get("tiles", [])}

    old_state, new_state = snapshot.get("state", {}), observation.get("state", {})
    state = changed({k: v for k, v in old_state.items() if k not in ("player", "tiles")}, {k: v for k, v in new_state.items() if k not in ("player", "tiles")})
    player = changed(old_state.get("player", {}), new_state.get("player", {}))
    if player:
        state["player"] = player
    before, after = keyed(snapshot), keyed(observation)
    old_tiles, new_tiles = tiles(snapshot), tiles(observation)
    return {"state": state,
            "tiles_changed": [tile for at, tile in new_tiles.items() if old_tiles.get(at) != tile],
            "tiles_removed": [old_tiles[at].get("position") for at in old_tiles if at not in new_tiles],
            "entities_added": [entity for id, entity in after.items() if id not in before],
            "entities_removed": [id for id in before if id not in after],
            "entities_changed": {id: changed(before[id], entity) for id, entity in after.items() if id in before and before[id] != entity}}


def numeric_option_id(option_id, groups):
    """Map a `<target>.<n>` or `<target>.<verb>` ID to `T.A` when exactly one listed group matches the target and action; else None."""
    name, _, last = option_id.strip("`'\" ").rpartition(".")
    # The name may itself end in a dotted number (entity id, label, float coordinate); else `Label.<group>.<n>` drops the stray group number.
    for target in (name, re.sub(r"\.\d+$", "", name)):
        owners = [i for i, g in enumerate(groups) if target in (g.get("subject"), (g.get("evidence") or {}).get("id"), (g.get("evidence") or {}).get("label"))]
        if len(owners) == 1:
            break
    else:
        return None
    group = groups[owners[0]]
    if group.get("drag_source"):  # Listed with only its .0 option.
        return f"{owners[0]}.0"
    if last.isdigit():
        return f"{owners[0]}.{last}"
    found = [i for i, a in enumerate(group["actions"]) if last == (a["args"].get("verb") if a["tool"] == "perform_action" else a["tool"])]
    return f"{owners[0]}.{found[0]}" if len(found) == 1 else None
