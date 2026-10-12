"""Startup migrations: temporary stores only; every migration must be safe to run twice."""
import json

from ashad.coordinator import Coordinator
from ashad.migrations import retire_fixer, retire_saves
from ashad.repair_tasks import issue_directory
from ashad.store import Store
from test_api_boundaries import offline_app


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


def test_retire_saves_deletes_rows_and_strips_issue_links(tmp_path):
    store = Store(tmp_path)
    store.put("saves", {"id": "save_1", "run_id": "run-1"})
    store.command("save:run-1", "c1", {"save_id": "save_1"})
    store.command("instance", "keep-me", {"x": 1})
    store.put("issues", {
        "id": "issue_a", "type": "unsupported_action", "signature": "a" * 16, "issue_id": "issue_a", "revision": 3,
        "published_revision": 3,
        "examples": [{"example_id": "e1", "saves": {"before": "save_1", "at_issue": None}}, {"example_id": "e2"}],
        "first_seen": {"at": "t", "save_before": "save_1"},
        "last_seen": {"at": "t", "save_before": None}})
    archive = store.run_dir("run-1") / "saves" / "save_1" / "game.zip"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"zip")
    store.event("run-1", "unsupported_action_recorded", {"issue_id": "issue_a", "save_before": "save_1"})
    retire_saves(store)
    retire_saves(store)
    assert store.list("saves") == []
    assert store.command("save:run-1", "c1") is None
    assert store.command("instance", "keep-me") == {"x": 1}
    issue = store.require("issues", "issue_a")
    assert all("saves" not in example for example in issue["examples"])
    assert "save_before" not in issue["first_seen"] and "save_before" not in issue["last_seen"]
    assert issue["revision"] == 4
    assert archive.exists()
    assert store.events("run-1")[0]["payload"]["save_before"] == "save_1"
    published = list(issue_directory(store).rglob("*.json"))
    assert len(published) == 1
    assert published[0].parent.name == "unassigned"
    assert json.loads(published[0].read_text())["first_seen"] == {"at": "t"}


def test_retire_saves_on_empty_store_is_a_noop(tmp_path):
    store = Store(tmp_path)
    retire_saves(store)
    retire_saves(store)
    assert store.list("issues") == []


def test_startup_runs_retire_saves(tmp_path):
    store = Store(tmp_path)
    store.put("saves", {"id": "save_1", "run_id": "run-1"})
    store.close()
    for _ in range(2):
        app = offline_app(tmp_path)
        try:
            assert app.state.store.list("saves") == []
        finally:
            app.state.store.close()
