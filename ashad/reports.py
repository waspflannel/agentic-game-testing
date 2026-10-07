"""Deterministic reports remain available without provider access."""
from __future__ import annotations
from datetime import datetime, timezone
import json
from .store import redact


PART_EVENTS = 1000


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value else "unknown"


def generate_report(store, run_id):
    run = store.require("runs", run_id)
    plan = run["plan"]
    incidents = [i for i in store.list("incidents") if i["run_id"] == run_id]
    usage = [i for i in store.list("usage") if i.get("run_id") == run_id]
    count = store.count_events(run_id)
    metered = [u for u in usage if u.get("billing") != "subscription"]
    subscription_calls = len(usage) - len(metered)
    cost_summary = f"Recorded calls: {len(usage)}; subscription calls: {subscription_calls}."
    if subscription_calls or run.get("billing") == "subscription":
        cost_summary += " Codex uses ChatGPT subscription limits; no per-request dollar cost is reported."
    if metered:
        cost_summary += (f" Metered API cost: {sum(u['estimated_cost_usd'] for u in metered):.6f} USD."
            if all(u.get("estimated_cost_usd") is not None for u in metered) else " Metered API cost: unknown.")
    limit_summary = ("API dollar ceilings do not apply to subscription usage." if run.get("billing") == "subscription"
        else f"Spend ceiling: {plan['limits']['spend_usd'] if plan['limits']['spend_usd'] is not None else 'unset'} USD.")
    lines = [f"# ASHAD run {run_id}", "", f"{plan['title']}", "", f"State: **{run['state']}**. End reason: **{run.get('ending_reason') or 'not ended'}**.", f"Goal completion: **{run.get('goal_complete', False)}**. Full demo: **{run.get('demo_complete', False)}**. Validity: **{run.get('validity', 'unknown')}**. Evidence: **{run.get('evidence_status', 'unknown')}**.", "", f"Started: {stamp(run.get('started_at'))}. Ended: {stamp(run.get('ended_at'))}.", "", "## Build and setup", "", f"Scenario: {run['scenario_id']} revision {run['scenario_revision']}. Model: {run.get('model')}. Controller: {run.get('controller_version')}.", f"Build: {plan['build_id']}. Source: {run.get('build', {}).get('source_revision', 'unknown')}.", f"Transport: {plan['transport']['mode']}. App ID: {plan['transport'].get('app_id') or 'not applicable'}. World namespace: {run.get('world_namespace', 'unknown')}.", f"Starting policy: {plan['world_setup']['mode']}; characters: {plan['world_setup']['character_policy']}; fixture: {plan['world_setup'].get('fixture_id') or 'none'}.", f"Starting milestones: {json.dumps(run.get('starting_milestones', []))}. Handler: {run.get('progress', {}).get('handler') or 'unknown'}.", "", "A saved-start success is distinct from completion from a fresh world. Repository-informed agents are not novice players. Wall time includes inference, capture, coordination and controller overhead.", "", "## Players and goals", ""]
    lines += [f"Play mode: **{plan.get('play_mode', 'screen')}**. Guidance: **{plan.get('guidance', 'guided')}**. Guided completion is assisted gameplay, not unaided tutorial discovery.", ""]
    for p in plan["players"]:
        actual = run.get("players", {}).get(p["id"], {})
        lines += [f"- {p['name']} (`{p['id']}`), {p['role']}, worker `{actual.get('worker_id', p.get('worker_id'))}`, identity `{actual.get('identity', 'unknown')}`: {p['goal']}. Context: {p['context_policy']}. Final phase: {actual.get('phase', 'unknown')}."]
    lines += ["", "## Agent diagnostics", ""]
    if run.get("pause"):
        pause = run["pause"]
        lines += [f"Warning: {pause.get('reason', 'Unknown pause reason')}",
                  f"Recovery: {pause.get('recovery', 'Inspect the latest action and observation before resuming.')}", ""]
    for player_id, actual in run.get("players", {}).items():
        lines += [f"### {player_id}", "", "```json", json.dumps(redact({
            "agent_status": actual.get("agent_status"), "activity": actual.get("activity"),
            "pending_action": actual.get("pending_action"), "last_state_change_at": actual.get("last_state_change_at")}), indent=2), "```", ""]
    lines += ["", "## Milestones and route", "", "```json", json.dumps(run.get("progress", {}), ensure_ascii=False, indent=2), "```", "", "## Incidents", ""]
    if run.get("source_run_id"):
        lines += [f"This is a {run.get('rerun_kind', 'semantic')} follow-up to `{run['source_run_id']}`. Its evidence and run identity are separate.", ""]
    if not incidents:
        lines.append("No incident was recorded. This does not establish complete bug coverage.")
    for i in incidents:
        lines += [f"### {i['id']}: {i.get('category', 'unresolved')}", "", f"Status: {i['status']}; classification: {i.get('classification', 'unresolved')}; confidence: {i.get('confidence', 'unknown')}.", f"Expected: {i.get('expected', 'unknown')}", f"Observed: {i.get('actual', 'unknown')}", f"Expected source: {i.get('expected_source', 'unknown')}", f"Evidence: {', '.join(i.get('evidence_ids', [])) or 'not available'}", f"Occurrences: {json.dumps(i.get('occurrences', []))}", f"Human history: {json.dumps(i.get('review_history', []))}", ""]
    lines += ["## Usage and limits", "", f"Duration ceiling: {plan['limits']['duration_seconds'] if plan['limits']['duration_seconds'] is not None else 'unset'} seconds. {limit_summary}", cost_summary, "", "```json", json.dumps(usage, indent=2), "```", "", "## Evidence and limitations", "", f"Whole-run ledger: [ledger.jsonl](ledger.jsonl) ({count} indexed entries).", "Pinned frames/clips are linked from the evidence index; missing evidence is not proof of absence.", "Playback of recorded input is not deterministic multiplayer replay. A failure to reproduce does not establish a fix."]
    lines += [f"- {s}" for s in run.get("limitations", [])]
    from urllib.parse import quote
    for artifact in run.get("retained_files", []):
        lines.append(f"- [{artifact['player_id']} · {artifact['name']}]({quote(artifact['name'])}) ({artifact['kind']}, {artifact.get('bytes', 'unknown')} bytes)")
    lines += ["", "## Complete chronological action appendix", "", "All retained events appear below or in linked parts. Operational reasons do not contain private model reasoning.", ""]
    directory = store.run_dir(run_id)
    try:
        previous = {p["part"]: p for p in json.loads((directory / "evidence-index.json").read_text(encoding="utf-8"))["appendix"]}
    except (OSError, ValueError, KeyError, TypeError):
        previous = {}
    index = []
    after, part = 0, 0
    while True:
        part += 1
        name = f"actions-{part:04d}.md"
        cached = previous.get(name)
        if cached and (directory / name).exists() and store.count_events(run_id, after=after, before=cached["last"] + 1) == PART_EVENTS:
            first, after = cached["first"], cached["last"]  # Full parts are append-only history; rewrite only the newest.
        else:
            events = store.events(run_id, after=after, limit=PART_EVENTS)
            if not events:
                break
            appendix = [f"# Action ledger part {part}", ""]
            for event in events:
                appendix += [f"## {event['seq']} {event['id']}", "", f"{stamp(event['timestamp'])} — {event['kind']}", "", "```json", json.dumps(event, ensure_ascii=False, indent=2), "```", ""]
            (directory / name).write_text("\n".join(appendix), encoding="utf-8")
            first, after = events[0]["seq"], events[-1]["seq"]
        lines.append(f"- [{first}–{after} ({name})]({name})")
        index.append({"part": name, "first": first, "last": after})
    index_record = {"run_id": run_id, "ledger": "ledger.jsonl", "entries": count, "appendix": index, "incidents": incidents, "evidence": run.get("evidence", []), "retained_files": run.get("retained_files", []), "gaps": run.get("limitations", []), "status": run.get("evidence_status", "unknown")}
    (directory / "evidence-index.json").write_text(json.dumps(index_record, indent=2), encoding="utf-8")
    (directory / "run.json").write_text(json.dumps(run, indent=2, ensure_ascii=False), encoding="utf-8")
    report = "\n".join(lines) + "\n"
    temporary = directory / "report.tmp"
    temporary.write_text(report, encoding="utf-8")
    temporary.replace(directory / "report.md")
    return report
