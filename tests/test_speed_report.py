"""Speed report over a seeded store; no provider, token, network or game."""
import json

import pytest

from ashad.speed_report import main
from ashad.store import Store


def _timing(outcome, seconds, total_seconds, gap_seconds, **extra):
    row = {
        "outcome": outcome,
        "seconds": seconds,
        "total_seconds": total_seconds,
        "gap_seconds": gap_seconds,
        "tick_seconds": 0.02,
        "capture_seconds": 0.3,
        "store": {"seconds": 0.01, "bytes": 500, "writes": 3},
    }
    row.update(extra)
    return row


def seed(tmp_path, runs=("run",), *, events=True, action_at=1001.0):
    store = Store(tmp_path)
    for rid in runs:
        store.put("runs", {"id": rid, "state": "Running", "model": "fake-model", "plan": {}, "players": {"p": {}}})
    if events:
        rid = runs[-1]
        store.event(rid, "agent_turn_timing", _timing(
            "decided",
            {"observe": 0.1, "decision_context": 0.05, "decide": 2.0, "tool:perform_action": 0.2},
            2.2, None,
            bytes={"observation": 9000, "prompt_observation": 7000, "decision_context": 12000},
            context_truncated=False,
        ))
        store.event(rid, "agent_turn_timing", _timing("waiting", {"observe": 0.1}, 0.1, 1.1))
        store.event(rid, "agent_turn_timing", _timing(
            "planning",
            {"observe": 0.1, "plan_model": 3.0, "plan_validate": 0.01},
            3.2, 0.9,
            calls={"plan_model": 2},
            counts={"plan_rejected": 1},
        ))
        store.event(rid, "provider_request_finished", {
            "purpose": "player", "question_names": ["target"], "timestamp": 1000.0,
            "latency_seconds": 1.0, "input_bytes": 8000, "status": "completed",
        })
        store.event(rid, "provider_request_finished", {
            "purpose": "player", "question_names": ["action"], "timestamp": action_at,
            "latency_seconds": 1.0, "input_bytes": 8000, "status": "completed",
        })
        store.event(rid, "subgoal_plan_rejected", {})
    store.close()


def _report(out):
    files = list(out.glob("*.json"))
    assert len(files) == 1
    return files[0], json.loads(files[0].read_text())


def test_report_prints_steps_and_four_hypotheses_and_saves_json(tmp_path, capsys):
    seed(tmp_path)
    out = tmp_path / "out"
    main(["--data", str(tmp_path), "--out", str(out)])
    stdout = capsys.readouterr().out
    for text in ("observe", "H1 serial model calls", "H2 observation payloads", "H3 validator retries", "H4 polling waits"):
        assert text in stdout
    path, report = _report(out)
    assert stdout.strip().splitlines()[-1] == f"speed report: {path}"
    assert {name: report["turns"][name]["count"] for name in ("decided", "waiting", "planning")} == {"decided": 1, "waiting": 1, "planning": 1}
    assert "player/target" in report["model_calls"] and "player/action" in report["model_calls"]
    assert report["hypotheses"]["serial_model_calls"]["max_concurrent"] == 1
    assert report["hypotheses"]["validator_retries"]["plan_rejected"] == 1
    assert report["hypotheses"]["validator_retries"]["rejected_events"] == 1


def test_overlapping_model_calls_report_concurrency_two(tmp_path):
    seed(tmp_path, action_at=1000.5)
    out = tmp_path / "out"
    main(["--data", str(tmp_path), "--out", str(out)])
    _, report = _report(out)
    assert report["hypotheses"]["serial_model_calls"]["max_concurrent"] == 2


def test_run_flag_selects_that_run_and_latest_is_default(tmp_path):
    seed(tmp_path, runs=("old", "new"))
    store = Store(tmp_path)
    store.event("old", "agent_turn_timing", _timing("waiting", {"observe": 0.1}, 0.1, None))
    store.close()
    out = tmp_path / "out"
    main(["--data", str(tmp_path), "--out", str(out)])
    assert any(path.name.startswith("new-") for path in out.glob("*.json"))
    main(["--data", str(tmp_path), "--run", "old", "--out", str(out)])
    assert any(path.name.startswith("old-") for path in out.glob("*.json"))


@pytest.mark.parametrize("case, expected", [
    ("missing", "no ASHAD database"),
    ("unknown", "no run nope"),
    ("untimed", "no timing events; rebuild ASHAD"),
])
def test_missing_database_unknown_run_and_untimed_run_exit_with_guidance(tmp_path, case, expected):
    argv = ["--data", str(tmp_path), "--out", str(tmp_path / "out")]
    if case == "unknown":
        seed(tmp_path)
        argv += ["--run", "nope"]
    elif case == "untimed":
        seed(tmp_path, events=False)
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert expected in str(exc.value.code)


def test_report_leaves_the_data_directory_unchanged(tmp_path):
    seed(tmp_path)
    db = tmp_path / "ashad.sqlite3"
    ledger = tmp_path / "runs" / "run" / "ledger.jsonl"
    before = (db.read_bytes(), db.stat().st_mtime_ns, ledger.read_bytes())
    main(["--data", str(tmp_path), "--out", str(tmp_path / "out")])
    assert (db.read_bytes(), db.stat().st_mtime_ns, ledger.read_bytes()) == before
