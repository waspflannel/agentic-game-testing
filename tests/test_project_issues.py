"""Project-scoped issues: grouping, files, close/reopen, API and screenshot notes. Fakes and temporary data only."""
import json

from ashad import repair_tasks
from ashad.repair_tasks import issue_directory, issue_filename, record_issue, publish_issues
from ashad.store import Store
from test_unsupported_actions import basis, example


def test_same_signature_in_two_projects_is_two_issues(tmp_path):
    store = Store(tmp_path)
    record_issue(store, "unsupported_action", basis(), example(project_id="p1"))
    record_issue(store, "unsupported_action", basis(), example(project_id="p2"))
    publish_issues(store)
    records = store.list("issues")
    assert len(records) == 2
    assert len({r["signature"] for r in records}) == 1
    assert len({r["id"] for r in records}) == 2
    for record in records:
        assert (issue_directory(store) / record["project_id"] / issue_filename(record)).is_file()


def test_occurrences_group_within_a_project(tmp_path):
    store = Store(tmp_path)
    for n in range(3):
        record_issue(store, "unsupported_action", basis(), example(project_id="p1", example_id=f"uae_{n}"))
    [record] = store.list("issues")
    assert record["occurrences"] == 3


def test_issue_without_project_goes_to_unassigned(tmp_path):
    store = Store(tmp_path)
    record_issue(store, "unsupported_action", basis(), example(project_id=None))
    publish_issues(store)
    [record] = store.list("issues")
    assert record["project_id"] is None
    assert (issue_directory(store) / "unassigned" / issue_filename(record)).is_file()


def test_issue_cap_is_per_project(tmp_path, monkeypatch):
    store = Store(tmp_path)
    monkeypatch.setattr(repair_tasks, "MAX_ISSUES", 1)
    assert record_issue(store, "unsupported_action", basis(), example(project_id="p1")) == "kept"
    other = basis(verb="ui_click")
    assert record_issue(store, "unsupported_action", other, example(project_id="p1")) == "full"
    assert record_issue(store, "unsupported_action", other, example(project_id="p2")) == "kept"
    assert repair_tasks.issue_room(store, "p1", "unsupported_action", other) == "full"


def test_set_status_records_history_and_republishes(tmp_path):
    store = Store(tmp_path)
    record_issue(store, "unsupported_action", basis(), example())
    publish_issues(store)
    [record] = store.list("issues")

    def on_disk():
        return json.loads(repair_tasks.issue_path(store, record).read_text(encoding="utf-8"))

    assert repair_tasks.set_issue_status(store, record["id"], "closed") is True
    shown = on_disk()
    assert shown["status"] == "closed"
    assert [(h["from"], h["to"], h["by"]) for h in shown["status_history"]] == [("open", "closed", "user")]
    revision = store.get("issues", record["id"])["revision"]
    repair_tasks.set_issue_status(store, record["id"], "closed")
    assert len(on_disk()["status_history"]) == 1
    assert store.get("issues", record["id"])["revision"] == revision
    repair_tasks.set_issue_status(store, record["id"], "open")
    assert len(on_disk()["status_history"]) == 2
    assert repair_tasks.set_issue_status(store, "issue_nope", "open") is None


def test_closed_issue_reopens_on_record_and_keeps_example_when_full(tmp_path, monkeypatch):
    store = Store(tmp_path)
    monkeypatch.setattr(repair_tasks, "MAX_EXAMPLES", 1)
    record_issue(store, "unsupported_action", basis(), example(example_id="uae_1"))
    [record] = store.list("issues")
    repair_tasks.set_issue_status(store, record["id"], "closed")
    assert record_issue(store, "unsupported_action", basis(), example(example_id="uae_2", run_id="run-2")) == "kept"
    issue = store.get("issues", record["id"])
    assert issue["status"] == "open"
    assert issue["status_history"][-1]["by"] == "recurrence"
    assert issue["status_history"][-1]["run_id"] == "run-2"
    assert issue["status_history"][-1]["example_id"] == "uae_2"
    assert issue["examples"][-1]["example_id"] == "uae_2"
    assert issue["examples_dropped"] == 1
    assert len(issue["examples"]) == 1


def test_project_issues_lists_only_that_project_newest_first(tmp_path):
    store = Store(tmp_path)
    record_issue(store, "unsupported_action", basis(), example(project_id="p1", at="2026-10-11T00:00:00+00:00"))
    record_issue(store, "unsupported_action", basis(verb="ui_click"),
                 example(project_id="p1", at="2026-10-12T00:00:00+00:00"))
    record_issue(store, "unsupported_action", basis(verb="ui_key"), example(project_id="p2"))
    listed = repair_tasks.project_issues(store, "p1")
    assert [s["title"] for s in listed] == ["Agent could not ui_click on slot #", "Agent could not ui_drag on slot #"]
    assert set(listed[0]) == {"issue_id", "type", "title", "status", "occurrences", "first_at", "last_at",
                              "first_run_id", "last_run_id", "runs", "file"}
    assert listed[0]["last_at"] > listed[1]["last_at"]


def test_issue_detail_screenshot_notes(tmp_path):
    store = Store(tmp_path)
    store.put("runs", {"id": "ended", "state": "Ended"})
    store.put("runs", {"id": "active", "state": "Running"})
    frame = "pinned/x/f.jpg"
    (store.run_dir("ended") / "workers" / "player-1" / "pinned" / "x").mkdir(parents=True)
    (store.run_dir("ended") / "workers" / "player-1" / frame).write_bytes(b"jpg")

    def shot(status="pinned"):
        return {"status": status, "frames": [frame]}

    examples = [
        example(example_id="a", run_id="ended", screenshot=shot("unavailable")),
        example(example_id="b", run_id="gone", screenshot=shot()),
        example(example_id="c", run_id="active", screenshot=shot()),
        example(example_id="d", run_id="ended", screenshot=shot()),
        example(example_id="e", run_id="active", screenshot={"status": "pinned", "frames": []}),
    ]
    for item in examples:
        record_issue(store, "unsupported_action", basis(), item)
    store.put("runs", {"id": "ended", "state": "Ended"})
    (store.run_dir("ended") / "workers" / "player-1" / frame).unlink()
    record = store.list("issues")[0]
    record["examples"] = [repair_tasks.shrink_example(e) for e in examples]
    (store.run_dir("ended") / "workers" / "player-1" / frame).write_bytes(b"jpg")
    store.put("issues", record)
    # "d" is retained; add one more ended example whose file is absent.
    record["examples"].append(repair_tasks.shrink_example(
        example(example_id="f", run_id="ended", player_id="player-2", screenshot=shot())))
    store.put("issues", record)

    detail = repair_tasks.issue_detail(store, record["id"])
    notes = {e["example_id"]: e["screenshot"] for e in detail["examples"]}
    assert [notes[k]["note"] for k in "abcdef"] == ["unavailable", "deleted", "pending", "retained", "unavailable", "missing"]
    assert notes["d"]["url"] == "/api/runs/ended/files/workers/player-1/pinned/x/f.jpg"
    assert notes["a"]["url"] is None
    assert "published_revision" not in detail
    assert detail["file"] == str(repair_tasks.issue_path(store, record))
    assert repair_tasks.issue_detail(store, "issue_nope") is None
