"""Project-scoped issues: grouping, files, close/reopen, API and screenshot notes. Fakes and temporary data only."""
import json

import pytest

from ashad import repair_tasks
from ashad.repair_tasks import issue_directory, issue_filename, record_issue, publish_issues
from ashad.store import Store
from test_controller import MemoryCredentials, environment  # noqa: F401
from test_instance_issues import PLAYER, act, api, call, events, issue_files, running  # noqa: F401
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


def test_project_issues_run_filter_sees_every_hit_of_a_long_run(tmp_path, monkeypatch):
    store = Store(tmp_path)
    monkeypatch.setattr(repair_tasks, "RUN_ISSUE_EVENTS", 1)
    for n, verb in enumerate(["ui_drag", "ui_click"]):
        record_issue(store, "unsupported_action", basis(verb=verb), example(example_id=f"uae_{n}", project_id="p1"))
    for record in store.list("issues"):
        store.event("run-1", "unsupported_action_recorded", {"issue_id": record["id"]})
    assert len(repair_tasks.project_issues(store, "p1", "run-1")) == 2
    assert repair_tasks.project_issues(store, "p1", "run-other") == []

def test_issue_detail_screenshot_notes(tmp_path):
    store = Store(tmp_path)
    store.put("runs", {"id": "ended", "state": "Ended"})
    store.put("runs", {"id": "active", "state": "Running"})
    frame = "pinned/x/f.jpg"
    retained = store.run_dir("ended") / "workers" / "player-1" / frame
    retained.parent.mkdir(parents=True)
    retained.write_bytes(b"jpg")
    pinned = {"status": "pinned", "frames": [frame]}
    for item in [example(example_id="unpinned", run_id="ended"),
                 example(example_id="gone", run_id="unknown", screenshot=pinned),
                 example(example_id="running", run_id="active", screenshot=pinned),
                 example(example_id="kept", run_id="ended", screenshot=pinned),
                 example(example_id="lost", run_id="ended", player_id="player-2", screenshot=pinned)]:
        record_issue(store, "unsupported_action", basis(), item)
    [record] = store.list("issues")

    detail = repair_tasks.issue_detail(store, record["id"])
    shots = {e["example_id"]: e["screenshot"] for e in detail["examples"]}
    assert {k: shot["note"] for k, shot in shots.items()} == {
        "unpinned": "unavailable", "gone": "deleted", "running": "pending", "kept": "retained", "lost": "missing"}
    assert shots["kept"]["url"] == "/api/runs/ended/files/workers/player-1/pinned/x/f.jpg"
    assert all(shot["url"] is None for k, shot in shots.items() if k != "kept")
    assert "published_revision" not in detail
    assert detail["file"] == str(repair_tasks.issue_path(store, record))
    assert repair_tasks.issue_detail(store, "issue_nope") is None


@pytest.mark.asyncio
async def test_hit_records_the_runs_project(environment, monkeypatch):
    env = await running(environment, monkeypatch, reject_verbs={"interact"})
    await act(env)
    [record] = issue_files(env.store)
    assert record["project_id"] == "project-1"
    [recorded] = events(env, "unsupported_action_recorded")
    assert recorded["payload"]["issue_id"] == record["issue_id"]
    assert (issue_directory(env.store) / "project-1" / issue_filename(record)).exists()


@pytest.mark.asyncio
async def test_recurrence_reopens_with_event_and_pin(environment, monkeypatch):
    monkeypatch.setattr(repair_tasks, "MAX_EXAMPLES", 1)
    env = await running(environment, monkeypatch, reject_verbs={"interact"})
    await act(env)
    [record] = issue_files(env.store)
    repair_tasks.set_issue_status(env.store, record["issue_id"], "closed")
    env.desktop.calls.clear()
    await act(env)
    [record] = issue_files(env.store)
    assert record["status"] == "open"
    example_id = record["examples"][-1]["example_id"]
    [reopened] = events(env, "issue_reopened")
    assert reopened["payload"] == {"player_id": PLAYER, "issue_id": record["issue_id"], "example_id": example_id}
    pins = [data for path, data in env.desktop.calls if path == "/pin"]
    assert [p["incident_id"] for p in pins] == [example_id]


@pytest.mark.asyncio
async def test_close_and_reopen_persist_to_file(environment, monkeypatch, api):
    env = await running(environment, monkeypatch, reject_verbs={"interact"})
    await act(env)
    [record] = issue_files(env.store)
    path = f"/api/issues/{record['issue_id']}/status"

    def on_disk():
        return issue_files(env.store)[0]

    response = await call(api, "POST", path, status="closed")
    assert response.status_code == 200
    assert response.json()["status"] == "closed"
    assert on_disk()["status"] == "closed"
    assert [h["by"] for h in on_disk()["status_history"]] == ["user"]
    assert (await call(api, "POST", path, status="closed")).status_code == 200
    assert len(on_disk()["status_history"]) == 1
    assert (await call(api, "POST", path, status="open")).status_code == 200
    assert on_disk()["status"] == "open"


@pytest.mark.asyncio
async def test_bad_status_is_422_and_unknown_issue_is_404(api):
    store = api.state.store
    record_issue(store, "unsupported_action", basis(), example())
    [record] = store.list("issues")
    path = f"/api/issues/{record['id']}/status"
    assert (await call(api, "POST", path, status="fixed")).status_code == 422
    assert (await call(api, "POST", path, status="closed", x=1)).status_code == 422
    assert (await call(api, "POST", "/api/issues/issue_nope/status", status="closed")).status_code == 404
    assert (await call(api, "GET", "/api/issues/issue_nope")).status_code == 404


@pytest.mark.asyncio
async def test_project_issues_endpoint_filters_by_run_and_project(api):
    store = api.state.store
    store.put("projects", {"id": "project-1", "name": "One"})
    for n, (verb, run, project, at) in enumerate([("ui_drag", "run-1", "project-1", "2026-10-11T00:00:00+00:00"),
                                                  ("ui_click", "run-2", "project-1", "2026-10-12T00:00:00+00:00"),
                                                  ("ui_key", "run-3", "p2", "2026-10-13T00:00:00+00:00")]):
        record_issue(store, "unsupported_action", basis(verb=verb),
                     example(example_id=f"uae_{n}", run_id=run, project_id=project, at=at))
    for record in store.list("issues"):
        store.event(record["first_seen"]["run_id"], "unsupported_action_recorded", {"issue_id": record["id"]})
    listed = (await call(api, "GET", "/api/projects/project-1/issues")).json()["issues"]
    assert [i["last_run_id"] for i in listed] == ["run-2", "run-1"]
    only = (await call(api, "GET", "/api/projects/project-1/issues?run_id=run-1")).json()["issues"]
    assert [i["last_run_id"] for i in only] == ["run-1"]


@pytest.mark.asyncio
async def test_unknown_project_is_404(api):
    assert (await call(api, "GET", "/api/projects/nope/issues")).status_code == 404


@pytest.mark.asyncio
async def test_retained_screenshot_url_is_served(api):
    store = api.state.store
    store.put("runs", {"id": "run-1", "state": "Ended"})
    frame = "pinned/uae_1/f.jpg"
    folder = store.run_dir("run-1") / "workers" / "player-1" / "pinned" / "uae_1"
    folder.mkdir(parents=True)
    (folder / "f.jpg").write_bytes(b"jpg")
    record_issue(store, "unsupported_action", basis(),
                 example(screenshot={"status": "pinned", "frames": [frame]}))
    [record] = store.list("issues")
    detail = (await call(api, "GET", f"/api/issues/{record['id']}")).json()
    shot = detail["examples"][0]["screenshot"]
    assert shot["note"] == "retained"
    assert (await call(api, "GET", shot["url"])).status_code == 200


@pytest.mark.asyncio
async def test_delete_project_removes_its_issues_and_folder_only(environment):
    store, coord, _plan, _desktops = environment
    store.put("projects", {"id": "p2", "name": "Other"})
    for project in ("project-1", "p2"):
        record_issue(store, "unsupported_action", basis(), example(project_id=project))
    publish_issues(store)
    keep = next(r for r in store.list("issues") if r["project_id"] == "p2")
    result = await coord.delete_project("project-1")
    assert result["files_removed"] is True
    assert [r["project_id"] for r in store.list("issues")] == ["p2"]
    assert not (issue_directory(store) / "project-1").exists()
    assert (issue_directory(store) / "p2" / issue_filename(keep)).is_file()
