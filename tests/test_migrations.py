"""Startup migrations: temporary stores only; every migration must be safe to run twice."""
from ashad.coordinator import Coordinator
from ashad.migrations import retire_fixer
from ashad.store import Store


def test_retire_fixer_ends_fixing_runs_once(tmp_path):
    store = Store(tmp_path)
    store.put("runs", {"id": "r-fix", "state": "Fixing", "ended_at": 123.0, "loop_id": "loop_1"})
    store.put("runs", {"id": "r-run", "state": "Running"})
    store.put("runs", {"id": "r-end", "state": "Ended", "ending_reason": "user_stopped"})
    store.put("loops", {"id": "loop_1"})
    store.put("fixer_usage", {"id": "u1"})
    store.command("loop", "c1", {"x": 1})
    retire_fixer(store)
    retire_fixer(store)
    fixed = store.require("runs", "r-fix")
    assert fixed["state"] == "Ended" and fixed["ending_reason"] == "fix_loop_removed" and fixed["ended_at"] == 123.0
    assert store.require("runs", "r-run") == {"id": "r-run", "state": "Running"}
    assert store.require("runs", "r-end") == {"id": "r-end", "state": "Ended", "ending_reason": "user_stopped"}
    ended = [e for e in store.events("r-fix") if e["kind"] == "run_ended"]
    assert len(ended) == 1
    assert ended[0]["payload"] == {"reason": "fix_loop_removed", "from": "Fixing"}
    assert store.list("loops") == [] and store.list("fixer_usage") == []
    assert store.command("loop", "c1") is None


def test_retire_fixer_sets_missing_ended_at(tmp_path):
    store = Store(tmp_path)
    store.put("runs", {"id": "r-fix", "state": "Fixing"})
    retire_fixer(store)
    assert isinstance(store.require("runs", "r-fix")["ended_at"], float)


def test_retired_fixing_run_releases_the_slot(tmp_path):
    store = Store(tmp_path)
    store.put("runs", {"id": "r-fix", "state": "Fixing", "players": {"p1": {"teardown_confirmed": True}}})
    retire_fixer(store)
    assert "r-fix" not in [o["run_id"] for o in Coordinator(store, None, None).occupied_instances()]


def test_retire_fixer_drops_fixer_minutes_setting(tmp_path):
    store = Store(tmp_path)
    store.put("settings", {"id": "main", "concurrent_sessions": 1, "fixer_daily_minutes": 90})
    retire_fixer(store)
    retire_fixer(store)
    assert store.require("settings", "main") == {"id": "main", "concurrent_sessions": 1}


def test_retire_fixer_without_settings_is_a_noop(tmp_path):
    store = Store(tmp_path)
    retire_fixer(store)
    retire_fixer(store)
