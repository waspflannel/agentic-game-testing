"""Speed report for one ASHAD run: per-step turn timing, model calls and hypothesis evidence.

  python3 -m ashad.speed_report [--run <run_id>] [--data <controller dir>] [--out <dir>]

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
            entry["nested_in"] = "decide"
        steps[step] = entry
    # tool: time sits inside decide, so it is not added again.
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


def _model_calls(events):
    grouped = {}
    payloads = []
    for event in events:
        if event["kind"] != "provider_request_finished":
            continue
        payload = event["payload"]
        payloads.append(payload)
        key = payload["purpose"] + "/" + "/".join(payload.get("question_names") or [])
        bucket = grouped.setdefault(key, {"latency": [], "input_bytes": [], "status": {}})
        bucket["latency"].append(payload["latency_seconds"])
        bucket["input_bytes"].append(payload["input_bytes"])
        status = payload.get("status") or "unknown"
        bucket["status"][status] = bucket["status"].get(status, 0) + 1
    report = {}
    for key, bucket in grouped.items():
        report[key] = {
            "latency": spread(bucket["latency"]),
            "input_bytes": {
                "mean": round(sum(bucket["input_bytes"]) / len(bucket["input_bytes"]), 6),
                "max": max(bucket["input_bytes"]),
            },
            "status": bucket["status"],
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
    # tool: steps run inside the decide timer; subtract them so H1 counts model time only.
    model_seconds = (steps.get("decide") or {}).get("total") or 0
    model_seconds -= sum(entry["total"] or 0 for name, entry in steps.items() if name.startswith("tool:"))
    turns = len(timings) or None
    gap_total = gaps["gap_seconds"]["total"] or 0
    tick_values = _column(timings, "tick_seconds")
    action_results = dict(Counter(_action_status(event["payload"]) for event in events if event["kind"] == "action_result"))
    return {
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
            "plan_triggers": dict(Counter(row["plan_trigger"] for row in timings if row.get("plan_trigger"))),
            "plan_rejected": sum((row.get("counts") or {}).get("plan_rejected", 0) for row in timings),
            "rejected_events": sum(1 for event in events if event["kind"] == "subgoal_plan_rejected"),
            "plan_seconds_share": _ratio(plan_seconds, turn_total),
            "action_results": action_results,
            "action_outcomes": dict(Counter((event["payload"].get("result") or {}).get("status", "unknown") for event in events if event["kind"] == "structured_action_outcome")),
            "action_effects": dict(Counter((event["payload"].get("result") or {}).get("effect") or "none" for event in events if event["kind"] == "structured_action_outcome")),
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
    return {
        "run": {
            "id": run["id"],
            "model": run.get("model") or run["plan"].get("model"),
            "state": run["state"],
            "players": len(run["players"]),
            "first_event_at": first,
            "last_event_at": last,
            "wall_seconds": round(last - first, 6),
        },
        "turns": {
            outcome: {"count": len(samples), "total_seconds": round(sum(samples), 6), "mean_seconds": round(sum(samples) / len(samples), 6)}
            for outcome, samples in turns.items()
        },
        "steps": steps,
        "gaps": gaps,
        "model_calls": calls,
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


def print_report(summary, path):
    run = summary["run"]
    turn_count = sum(item["count"] for item in summary["turns"].values())
    print(f"run {run['id']}  model {run['model']}  turns {turn_count}  wall {run['wall_seconds']}s")
    print("step calls total_s mean_s p50_s p95_s max_s share")
    steps = summary["steps"]
    for name in _ordered_steps(steps):
        entry = steps[name]
        label = f"  {name}" if name.startswith("tool:") else name
        print(" ".join([label, _fmt(entry.get("calls")), _fmt(entry.get("total")), _fmt(entry.get("mean")), _fmt(entry.get("p50")), _fmt(entry.get("p95")), _fmt(entry.get("max")), _fmt(entry.get("share"))]))
    print(f"unaccounted - {_fmt(steps['unaccounted'])} - - - - -")
    store = steps["store"]
    print(" ".join(["store", "-", _fmt(store.get("total")), _fmt(store.get("mean")), _fmt(store.get("p50")), _fmt(store.get("p95")), _fmt(store.get("max")), "-"]))
    print("call count mean_s p50_s p95_s max_s mean_in_bytes")
    for name, entry in summary["model_calls"].items():
        if name == "max_concurrent":
            continue
        latency = entry["latency"]
        print(" ".join([name, _fmt(latency["count"]), _fmt(latency["mean"]), _fmt(latency["p50"]), _fmt(latency["p95"]), _fmt(latency["max"]), _fmt(entry["input_bytes"]["mean"])]))
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
    parser.add_argument("--run")
    parser.add_argument("--out", type=Path, default=REPO / "tmp" / "speed-report")
    args = parser.parse_args(argv)
    run, events = load(args.data, args.run)
    summary = summarize(run, events, args.data)
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"{run['id']}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(summary, indent=2) + "\n")
    print_report(summary, path)


if __name__ == "__main__":
    main()
