"""Speed report for one ASHAD run: per-step turn timing, model calls and hypothesis evidence.

  python3 -m ashad.speed_report [--run <run_id> | --compare <run_a> <run_b>] [--data <controller dir>] [--out <dir>]

Read-only; numbers only, no game text or credentials.
"""
import argparse
import json
import math
import os
import platform
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def default_data():
    if "ASHAD_DATA" in os.environ:
        return Path(os.environ["ASHAD_DATA"])
    if sys.platform == "win32":
        return Path(os.environ["APPDATA"]) / "ASHAD" / "controller"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/ASHAD/controller"
    return Path.home() / ".config/ASHAD/controller"


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    # Nearest rank: the smallest sample with at least q of the samples at or below it.
    return ordered[max(0, math.ceil(q * n) - 1)]


def spread(values):
    if not values:
        return {"count": 0, "total": None, "mean": None, "p50": None, "p95": None, "max": None}
    total = sum(values)
    return {
        "count": len(values),
        "total": round(total, 6),
        "mean": round(total / len(values), 6),
        "p50": round(percentile(values, 0.5), 6),
        "p95": round(percentile(values, 0.95), 6),
        "max": round(max(values), 6),
    }


def _ratio(part, whole):
    if not whole:
        return None
    return round(part / whole, 6)


def load(data, run_id):
    data = Path(data)
    db = data / "ashad.sqlite3"
    if not db.is_file():
        raise SystemExit(f"no ASHAD database at {db}; pass --data <controller dir> or set ASHAD_DATA")
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        if run_id:
            row = connection.execute("SELECT data FROM records WHERE kind='runs' AND id=?", (run_id,)).fetchone()
            if row is None:
                raise SystemExit(f"no run {run_id} in {db}; omit --run to use the latest run")
        else:
            row = connection.execute("SELECT data FROM records WHERE kind='runs' ORDER BY updated DESC LIMIT 1").fetchone()
            if row is None:
                raise SystemExit(f"no runs in {db}; start an instance in ASHAD first")
        run = json.loads(row[0])
        rows = connection.execute("SELECT data FROM events WHERE run_id=? ORDER BY seq", (run["id"],)).fetchall()
    finally:
        connection.close()
    events = [{**json.loads(raw), "raw_bytes": len(raw.encode())} for (raw,) in rows]
    if not any(event["kind"] == "agent_turn_timing" for event in events):
        raise SystemExit("no timing events; rebuild ASHAD from this branch and start a new instance")
    return run, events


def _timings(events):
    return [event["payload"] for event in events if event["kind"] == "agent_turn_timing"]


def _step_summary(timings):
    values = {}
    for row in timings:
        for step, seconds in (row.get("seconds") or {}).items():
            values.setdefault(step, []).append(seconds)
    turn_total = sum(row.get("total_seconds") or 0 for row in timings)
    steps = {}
    for step, samples in values.items():
        entry = spread(samples)
        entry["calls"] = sum((row.get("calls") or {}).get(step, 0) for row in timings)
        entry["share"] = _ratio(entry["total"] or 0, turn_total)
        if step.startswith("tool:"):
            entry["nested_in"] = "decide or act"
        steps[step] = entry
    # tool: time sits inside decide or act, so it is not added again.
    accounted = sum(entry["total"] or 0 for step, entry in steps.items() if not step.startswith("tool:"))
    steps["unaccounted"] = round(turn_total - accounted, 6)
    store_rows = [row["store"] for row in timings if isinstance(row.get("store"), dict)]
    store = spread([row["seconds"] for row in store_rows if "seconds" in row])
    store["bytes_total"] = sum(row.get("bytes", 0) for row in store_rows)
    store["writes_total"] = sum(row.get("writes", 0) for row in store_rows)
    steps["store"] = store
    return steps, turn_total


def _max_concurrent(payloads):
    """Peak overlap. An end sorts before a start at the same time, so back-to-back calls stay at 1."""
    points = []
    for payload in payloads:
        start = payload["timestamp"]
        points.append((start, 1))
        points.append((start + payload["latency_seconds"], -1))
    points.sort()
    current = peak = 0
    for _, delta in points:
        current += delta
        peak = max(peak, current)
    return peak


def _repeat_breakdown(events):
    stops = [event["payload"] for event in events if event["kind"] == "repeat_stopped" and (event["payload"].get("actions") or 0) > 1]
    return {"stops": len(stops), "actions": sum(stop["actions"] for stop in stops),
            "by_control_reason": dict(Counter(f"{stop.get('control')}/{stop.get('reason', 'unknown')}" for stop in stops))}


def _routing(events):
    by_route = {}
    for event in events:
        if event["kind"] == "decision_routed":
            by_route.setdefault(event["payload"]["route"], []).append(event["payload"])
    total = sum(len(payloads) for payloads in by_route.values())
    report = {}
    for route, payloads in by_route.items():
        latencies = [payload["latency_ms"] for payload in payloads]
        actions = sum(payload["actions"] for payload in payloads)
        report[route] = {"count": len(payloads), "share": _ratio(len(payloads), total), "latency_ms": spread(latencies), "actions": actions,
                         "s_per_action": _ratio(sum(latencies) / 1000, actions),
                         "fallbacks": dict(Counter(payload["fallback"] for payload in payloads if payload.get("fallback")))}
    return report


def _jev_menus(events):
    """Jev action-menu sizes on decision_routed, and the top 3 options of each low-confidence fallback."""
    payloads = [event["payload"] for event in events if event["kind"] == "decision_routed" and "menu_size" in event["payload"]]
    sizes = [payload["menu_size"] for payload in payloads]
    return {"menu_size": {"mean": _ratio(sum(sizes), len(sizes)), "max": max(sizes, default=None)},
            "low_confidence_top": [payload.get("top") for payload in payloads if payload.get("fallback") == "low_confidence"]}


def _attempts(events):
    """Leaf attempts by route, and broken leaves with how many became gaps rather than re-plans."""
    attempts = [event["payload"] for event in events if event["kind"] == "subgoal_attempt"]
    broken = [event["payload"] for event in events if event["kind"] == "subgoal_broken"]
    return ({"count": len(attempts), "by_route": dict(Counter(payload.get("route") for payload in attempts))},
            {"count": len(broken), "gaps": sum(1 for payload in broken if payload.get("outcome") == "gap")})


CACHE_TOKENS = ("cache_read_input_tokens", "cache_creation_input_tokens", "input_tokens")


def _model_calls(events):
    grouped = {}
    payloads = []
    usage = None
    for event in events:
        # _request writes each call's inference_usage event right before its provider_request_finished, with no await between.
        if event["kind"] == "inference_usage":
            usage = event["payload"].get("usage")
            continue
        if event["kind"] != "provider_request_finished":
            continue
        payload = event["payload"]
        payloads.append(payload)
        key = payload["purpose"] + "/" + "/".join(payload.get("question_names") or [])
        bucket = grouped.setdefault(key, {"latency": [], "input_bytes": [], "status": {}, "tokens": dict.fromkeys(CACHE_TOKENS, 0)})
        bucket["latency"].append(payload["latency_seconds"])
        bucket["input_bytes"].append(payload["input_bytes"])
        status = payload.get("status") or "unknown"
        bucket["status"][status] = bucket["status"].get(status, 0) + 1
        for name in CACHE_TOKENS:
            value = usage.get(name) if isinstance(usage, dict) else None
            if type(value) in (int, float):
                bucket["tokens"][name] += value
        usage = None
    report = {}
    for key, bucket in grouped.items():
        report[key] = {
            "latency": spread(bucket["latency"]),
            "input_bytes": {
                "mean": round(sum(bucket["input_bytes"]) / len(bucket["input_bytes"]), 6),
                "max": max(bucket["input_bytes"]),
            },
            "status": bucket["status"],
            "tokens": bucket["tokens"],
        }
    report["max_concurrent"] = _max_concurrent(payloads)
    return report


def _column(rows, name):
    return [row[name] for row in rows if row.get(name) is not None]


def _gaps(timings):
    return {
        "gap_seconds": spread(_column(timings, "gap_seconds")),
        "tick_seconds": spread(_column(timings, "tick_seconds")),
        "capture_seconds": spread(_column(timings, "capture_seconds")),
    }


def _ledger(events, data, run_id):
    kinds = {}
    for event in events:
        bucket = kinds.setdefault(event["kind"], {"kind": event["kind"], "bytes": 0, "count": 0})
        bucket["bytes"] += event["raw_bytes"]
        bucket["count"] += 1
    try:
        file_bytes = os.stat(Path(data) / "runs" / run_id / "ledger.jsonl").st_size
    except OSError:
        file_bytes = None
    return {
        "event_bytes": sum(event["raw_bytes"] for event in events),
        "top_kinds": sorted(kinds.values(), key=lambda item: item["bytes"], reverse=True)[:10],
        "file_bytes": file_bytes,
    }


def _present(rows, *keys):
    found = []
    for row in rows:
        value = row
        for key in keys:
            if not isinstance(value, dict) or key not in value:
                value = None
                break
            value = value[key]
        if value is not None:
            found.append(value)
    return found


def _ends(values):
    if not values:
        return {"p50": None, "max": None}
    return {"p50": percentile(values, 0.5), "max": max(values)}


def _outcome_time(timings, outcome):
    rows = [row for row in timings if row.get("outcome") == outcome]
    return {"count": len(rows), "total_seconds": round(sum(row.get("total_seconds") or 0 for row in rows), 6)}


def _action_status(payload):
    result = payload.get("result")
    if isinstance(result, dict) and result.get("status"):
        return result["status"]
    return "unknown"


def _usage_known(usage):
    """Mirrors usage.js rowTokens: total_tokens, or both input and output tokens."""
    usage = usage if isinstance(usage, dict) else {}
    number = lambda value: type(value) in (int, float)
    return number(usage.get("total_tokens")) or (number(usage.get("input_tokens")) and number(usage.get("output_tokens")))


def _hypotheses(timings, events, steps, calls, gaps, turn_total, event_bytes):
    decided = sum(1 for row in timings if row.get("outcome") == "decided")
    player = [event["payload"] for event in events if event["kind"] == "provider_request_finished" and event["payload"].get("purpose") == "player"]
    player_seconds = sum(payload.get("latency_seconds") or 0 for payload in player)
    inputs = [event["payload"]["input_bytes"] for event in events if event["kind"] == "provider_request_finished" and "input_bytes" in event["payload"]]
    plan_seconds = sum(entry["total"] or 0 for name, entry in steps.items() if isinstance(entry, dict) and name.startswith("plan_"))
    context_bytes = sum(event["raw_bytes"] for event in events if event["kind"] == "structured_decision_context")
    store_seconds = sum((row.get("store") or {}).get("seconds") or 0 for row in timings)
    store_bytes = sum((row.get("store") or {}).get("bytes") or 0 for row in timings)
    # tool: steps inside decide are subtracted; tool:act: steps sit inside act.
    model_seconds = (steps.get("decide") or {}).get("total") or 0
    model_seconds -= sum(entry["total"] or 0 for name, entry in steps.items() if name.startswith("tool:") and not name.startswith("tool:act:"))
    turns = len(timings) or None
    gap_total = gaps["gap_seconds"]["total"] or 0
    tick_values = _column(timings, "tick_seconds")
    outcomes = [event for event in events if event["kind"] == "structured_action_outcome"]
    # The worker's settle timings; v7/v8 game bridges also put a frame count under "settle" on statuses read without the worker's wait.
    settles = [s for s in ((e["payload"].get("result") or {}).get("settle") for e in outcomes) if isinstance(s, dict)]
    action_results = dict(Counter(_action_status(event["payload"]) for event in events if event["kind"] == "action_result"))
    planning = [event["payload"] for event in events if event["kind"] == "provider_request_finished" and event["payload"].get("purpose") == "planning"]
    goals = len({event["payload"]["plan"]["primary_goal"] for event in events if event["kind"] == "subgoal_plan_updated"}) or 1
    return {
        "planner": {
            "opus_calls": len(planning),
            "opus_seconds": round(sum(payload.get("latency_seconds") or 0 for payload in planning), 6),
            "goals": goals,
            "calls_per_goal": _ratio(len(planning), goals),
            "routes": dict(Counter(event["payload"]["route"] for event in events if event["kind"] == "plan_routed")),
            "pattern_rejected": sum(1 for event in events if event["kind"] == "plan_pattern_rejected"),
        },
        "serial_model_calls": {
            "model_seconds_share": _ratio(model_seconds, turn_total),
            "model_calls_per_decided_turn": _ratio(len(player), decided),
            "model_seconds_per_decided_turn": _ratio(player_seconds, decided),
            "max_concurrent": calls["max_concurrent"],
            "fallbacks": dict(Counter(event["payload"]["fallback"] for event in events if event["kind"] == "decision_selected" and event["payload"].get("fallback"))),
            "usage_missing": sum(1 for event in events if event["kind"] == "inference_usage" and not _usage_known(event["payload"].get("usage"))),
        },
        "observation_payloads": {
            "observation": _ends(_present(timings, "bytes", "observation")),
            "prompt_observation": _ends(_present(timings, "bytes", "prompt_observation")),
            "decision_context": _ends(_present(timings, "bytes", "decision_context")),
            "input_bytes": _ends(inputs),
            "truncated_turns": sum(1 for row in timings if row.get("context_truncated") is True),
            "evidence_truncated_turns": sum(1 for event in events if event["kind"] == "decision_selected" and event["payload"].get("evidence_truncated") is True),
            "list_entities_calls": sum((row.get("calls") or {}).get("tool:list_entities", 0) for row in timings),
            "store_seconds_per_turn": _ratio(store_seconds, turns),
            "store_bytes_per_turn": _ratio(store_bytes, turns),
            "decision_context_ledger_share": _ratio(context_bytes, event_bytes),
        },
        "validator_retries": {
            "planning_turns": sum(1 for row in timings if row.get("outcome") == "planning"),
            "planner_attempts": sum((row.get("calls") or {}).get("plan_model", 0) for row in timings),
            "repeat_actions": sum((row.get("counts") or {}).get("repeat_actions", 0) + (row.get("counts") or {}).get("macro_actions", 0) for row in timings),
            "repeat_stops": dict(Counter(event["payload"].get("reason", "unknown") for event in events if event["kind"] in ("repeat_stopped", "macro_stopped"))),
            "repeat_breakdown": _repeat_breakdown(events),
            "skill_replays": sum(1 for event in events if event["kind"] in ("skill_replayed", "recipe_replayed")),
            "skill_failures": sum(1 for event in events if event["kind"] in ("skill_failed", "recipe_failed")),
            "decision_replays": sum(1 for event in events if event["kind"] == "decision_replayed"),
            "decision_failures": sum(1 for event in events if event["kind"] == "decision_failed"),
            "decision_saves": sum(1 for event in events if event["kind"] == "decision_saved"),
            "batch_prefixes": len({(e["payload"].get("bytes") or {}).get("prefix_sha") for e in events if e["kind"] == "decision_selected"} - {None}),
            "decision_skips": sum(1 for event in events if event["kind"] == "replay_skipped"),
            "plan_triggers": dict(Counter(row["plan_trigger"] for row in timings if row.get("plan_trigger"))),
            "goals_by_fact": dict(Counter(g["completion"]["fact"] for g in next(
                (e["payload"]["plan"]["goals"] for e in reversed(events) if e["kind"] == "subgoal_plan_updated"), []))),
            "plan_rejected": sum((row.get("counts") or {}).get("plan_rejected", 0) for row in timings),
            "rejected_events": sum(1 for event in events if event["kind"] == "subgoal_plan_rejected"),
            "plan_seconds_share": _ratio(plan_seconds, turn_total),
            "action_results": action_results,
            "action_outcomes": dict(Counter((event["payload"].get("result") or {}).get("status", "unknown") for event in events if event["kind"] == "structured_action_outcome")),
            "action_effects": dict(Counter((event["payload"].get("result") or {}).get("effect") or "none" for event in events if event["kind"] == "structured_action_outcome")),
            "failed_with_effect": sum(1 for e in outcomes if (e["payload"].get("result") or {}).get("game_status") == "failed"),
            "settle_poll_seconds": round(sum(s.get("poll_seconds", 0) for s in settles), 6),
            "settle_observe_seconds": round(sum(s.get("observe_seconds", 0) for s in settles), 6),
            "turn_failures": dict(Counter(event["payload"].get("category", "unknown") for event in events if event["kind"] == "agent_turn_failed")),
        },
        "polling_waits": {
            "gap_share": _ratio(gap_total, turn_total + gap_total),
            "waiting": _outcome_time(timings, "waiting"),
            "unchanged": _outcome_time(timings, "unchanged"),
            "pending_action": _outcome_time(timings, "pending_action"),
            "tick_p50": None if not tick_values else round(percentile(tick_values, 0.5), 6),
            "tick_p95": None if not tick_values else round(percentile(tick_values, 0.95), 6),
        },
    }


def _environment():
    head = None
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True)
        if result.returncode == 0:
            head = result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        head = None
    return {
        "controller_head": head,
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "note": "store counters are process-wide; benchmark with one player",
    }


def summarize(run, events, data):
    timings = _timings(events)
    steps, turn_total = _step_summary(timings)
    calls = _model_calls(events)
    gaps = _gaps(timings)
    ledger = _ledger(events, data, run["id"])
    first, last = events[0]["timestamp"], events[-1]["timestamp"]
    turns = {}
    for row in timings:
        turns.setdefault(row["outcome"], []).append(row.get("total_seconds") or 0)
    routing = _routing(events)
    attempts, broken = _attempts(events)
    return {
        "run": {
            "id": run["id"],
            "model": run.get("model") or run["plan"].get("model"),
            "decision_engine": run["plan"].get("decision_engine", "jev_hybrid"),
            "state": run["state"],
            "players": len(run["players"]),
            "first_event_at": first,
            "last_event_at": last,
            "wall_seconds": round(last - first, 6),
            "s_per_action": _ratio(last - first, sum(entry["actions"] for entry in routing.values())),
            "active_seconds": round(turn_total, 6),
            "goal_reached": any(event["kind"] == "goal_reached" for event in events),
            "goals_verified": sum(len(event["payload"]["goal_ids"]) for event in events if event["kind"] == "subgoals_verified"),
        },
        "turns": {
            outcome: {"count": len(samples), "total_seconds": round(sum(samples), 6), "mean_seconds": round(sum(samples) / len(samples), 6)}
            for outcome, samples in turns.items()
        },
        "steps": steps,
        "gaps": gaps,
        "model_calls": calls,
        "routing": routing,
        "attempts": attempts,
        "broken": broken,
        "jev_menus": _jev_menus(events),
        "ledger": ledger,
        "hypotheses": _hypotheses(timings, events, steps, calls, gaps, turn_total, ledger["event_bytes"]),
        "environment": _environment(),
    }


def _fmt(value):
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)


def _ordered_steps(steps):
    names = [name for name in steps if name not in ("unaccounted", "store") and not name.startswith("tool:")]
    tools = [name for name in steps if name.startswith("tool:")]
    ordered = []
    for name in names:
        ordered.append(name)
        if name == "decide":
            ordered.extend(tools)
            tools = []
    ordered.extend(tools)
    return ordered


def print_compare(a, b, path):
    routes = list(dict.fromkeys([*a["routing"], *b["routing"]]))
    attempt_routes = list(dict.fromkeys([*a["attempts"]["by_route"], *b["attempts"]["by_route"]]))
    rows = [("engine", lambda s: s["run"]["decision_engine"]),
            *((f"route:{name} share", lambda s, name=name: (s["routing"].get(name) or {}).get("share")) for name in routes),
            ("s/action", lambda s: s["run"]["s_per_action"]),
            ("active_s", lambda s: s["run"]["active_seconds"]),
            ("wall_s", lambda s: s["run"]["wall_seconds"]),
            ("goal_reached", lambda s: s["run"]["goal_reached"]),
            ("goals_verified", lambda s: s["run"]["goals_verified"]),
            ("turns", lambda s: sum(item["count"] for item in s["turns"].values())),
            ("attempts", lambda s: s["attempts"]["count"]),
            *((f"attempts:{name}", lambda s, name=name: s["attempts"]["by_route"].get(name, 0)) for name in attempt_routes),
            ("broken", lambda s: s["broken"]["count"]),
            ("broken gaps", lambda s: s["broken"]["gaps"]),
            ("jev menu mean", lambda s: s["jev_menus"]["menu_size"]["mean"]),
            ("jev menu max", lambda s: s["jev_menus"]["menu_size"]["max"]),
            ("planner calls", lambda s: s["hypotheses"]["planner"]["opus_calls"]),
            ("planner seconds", lambda s: s["hypotheses"]["planner"]["opus_seconds"])]
    print(f"field {a['run']['id']} {b['run']['id']}")
    for label, value in rows:
        print(f"{label} {_fmt(value(a))} {_fmt(value(b))}")
    print(f"speed report: {path}")


def print_report(summary, path):
    run = summary["run"]
    turn_count = sum(item["count"] for item in summary["turns"].values())
    print(f"run {run['id']}  model {run['model']}  engine {run['decision_engine']}  s/action {_fmt(run['s_per_action'])}  turns {turn_count}  wall {run['wall_seconds']}s"
          f"  active {run['active_seconds']}s  goal_reached {'yes' if run['goal_reached'] else 'no'}  verified {run['goals_verified']}")
    print("step calls total_s mean_s p50_s p95_s max_s share")
    steps = summary["steps"]
    for name in _ordered_steps(steps):
        entry = steps[name]
        label = f"  {name}" if name.startswith("tool:") else name
        print(" ".join([label, _fmt(entry.get("calls")), _fmt(entry.get("total")), _fmt(entry.get("mean")), _fmt(entry.get("p50")), _fmt(entry.get("p95")), _fmt(entry.get("max")), _fmt(entry.get("share"))]))
    print(f"unaccounted - {_fmt(steps['unaccounted'])} - - - - -")
    store = steps["store"]
    print(" ".join(["store", "-", _fmt(store.get("total")), _fmt(store.get("mean")), _fmt(store.get("p50")), _fmt(store.get("p95")), _fmt(store.get("max")), "-"]))
    print("call count mean_s p50_s p95_s max_s mean_in_bytes cache_read_tokens cache_write_tokens input_tokens")
    for name, entry in summary["model_calls"].items():
        if name == "max_concurrent":
            continue
        latency = entry["latency"]
        print(" ".join([name, _fmt(latency["count"]), _fmt(latency["mean"]), _fmt(latency["p50"]), _fmt(latency["p95"]), _fmt(latency["max"]), _fmt(entry["input_bytes"]["mean"]),
            *(_fmt(entry["tokens"][name]) for name in CACHE_TOKENS)]))
    print("route count share mean_ms p50_ms p95_ms actions s_per_action")
    for route, entry in summary["routing"].items():
        latency = entry["latency_ms"]
        print(" ".join([route, _fmt(entry["count"]), _fmt(entry["share"]), _fmt(latency["mean"]), _fmt(latency["p50"]), _fmt(latency["p95"]),
                        _fmt(entry["actions"]), _fmt(entry["s_per_action"])]))
    fallbacks = [f"{route}:{reason}={count}" for route, entry in summary["routing"].items() for reason, count in entry["fallbacks"].items()]
    if fallbacks:
        print("fallbacks " + " ".join(fallbacks))
    attempts, broken = summary["attempts"], summary["broken"]
    print(f"attempts {attempts['count']} by_route {attempts['by_route']}  broken {broken['count']} gaps {broken['gaps']}")
    menus = summary["jev_menus"]
    print(f"jev menu size mean {_fmt(menus['menu_size']['mean'])} max {_fmt(menus['menu_size']['max'])}")
    for top in menus["low_confidence_top"]:
        print("  low_confidence top: " + "; ".join(f"{option['option']} p={option['p']}" for option in top or []))
    planner = summary["hypotheses"]["planner"]
    print(f"planner opus_calls {planner['opus_calls']}  seconds {_fmt(planner['opus_seconds'])}  per_goal {_fmt(planner['calls_per_goal'])}"
          f"  routes {planner['routes']}  pattern_rejected {planner['pattern_rejected']}")
    for heading, key in (
        ("H1 serial model calls", "serial_model_calls"),
        ("H2 observation payloads", "observation_payloads"),
        ("H3 validator retries", "validator_retries"),
        ("H4 polling waits", "polling_waits"),
    ):
        print(heading)
        for field, value in summary["hypotheses"][key].items():
            print(f"  {field}: {value}")
    print(f"speed report: {path}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Speed report for one ASHAD run: per-step turn timing, model calls and hypothesis evidence.")
    parser.add_argument("--data", type=Path, default=default_data())
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--run")
    group.add_argument("--compare", nargs=2, metavar=("RUN_A", "RUN_B"))
    parser.add_argument("--out", type=Path, default=REPO / "tmp" / "speed-report")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    stamp = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    if args.compare:
        a, b = (summarize(*load(args.data, run_id), args.data) for run_id in args.compare)
        path = args.out / f"compare-{a['run']['id']}-{b['run']['id']}-{stamp}.json"
        path.write_text(json.dumps({"a": a, "b": b}, indent=2) + "\n")
        print_compare(a, b, path)
        return
    run, events = load(args.data, args.run)
    summary = summarize(run, events, args.data)
    path = args.out / f"{run['id']}-{stamp}.json"
    path.write_text(json.dumps(summary, indent=2) + "\n")
    print_report(summary, path)


if __name__ == "__main__":
    main()
