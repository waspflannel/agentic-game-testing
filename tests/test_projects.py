"""Project/migration fixtures only: no controller, credentials, provider or game."""
import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3

import pytest

from ashad.projects import MIGRATION_ID, Projects
from ashad.store import Store


@pytest.fixture
def workspace(tmp_path):
    store = Store(tmp_path / "source")
    yield store, Projects(store)
    store.close()


def grouping(*build_ids, project_id="game-a", name="Same game name"):
    return {"project_id": project_id, "name": name, "build_ids": list(build_ids)}


def seed(store):
    store.put("settings", {"id": "main", "concurrent_sessions": 12})
    for build_id in ("build-a", "build-b"):
        store.put("builds", {"id": build_id, "name": "Same build name", "source_revision": "rev-1",
                             "artifact_sha256": "fixture-artifact", "command": ["fixture-only"],
                             "manifest_path": "C:/FIXTURE/build/manifest.json"})
    store.put("agents", {"id": "agent-a", "name": "Same agent name", "build_id": "build-a",
                         "instructions": "Original immutable fixture manual", "model": "fixture-model"})
    store.put("agents", {"id": "agent-orphan", "build_id": "missing-build", "instructions": "Keep me"})
    run = {"id": "run-a", "state": "Ended", "agent_id": "agent-a", "players": {
        "bot": {"bound": True, "launched": False, "teardown_confirmed": True}},
        "plan": {"build_id": "build-a", "title": "Same title", "players": [{"id": "bot"}]},
        "approved_plan": {"build_id": "build-a", "primary_goal": "Frozen goal"},
        "agent_profile": store.require("agents", "agent-a"), "build": store.require("builds", "build-a")}
    store.put("runs", run)
    store.put("runs", {"id": "run-missing", "state": "Ended", "plan": {"build_id": "missing-build"}})
    store.put("runs", {"id": "run-orphan", "state": "Ended", "plan": {"title": "Same title"}})
    store.put("runs", {"id": "run-ambiguous", "state": "Ended", "plan": {"build_id": "build-a"},
                       "build": {"id": "build-b"}})
    store.put("usage", {"id": "usage-a", "run_id": "run-a", "cost_usd": 1.25})
    store.put("workers", {"id": "worker-a", "credential_ref": "worker:fixture-only"})
    store.put("accounts", {"id": "account-a", "credential_ref": "account:fixture-only"})
    store.command("historical", "cmd-a", {"run_id": "run-a"})
    store.event("run-a", "fixture_event", {"outcome": "unknown"})
    (store.run_dir("run-a") / "frozen-plan.json").write_bytes(b'{ "build_id" : "build-a", "goal" : "Frozen goal" }\n')
    (store.run_dir("run-a") / "fixture-frame.png").write_bytes(b"fixture image bytes; not gameplay")
    return [grouping("build-a"), grouping("build-b", project_id="game-b")]


def migrate(projects, groups, tmp_path, **options):
    preview = projects.preview(groups)
    return projects.apply(groups, preview_token=preview["preview_token"], backup_dir=tmp_path / "backup",
                          confirmed=True, **options)


def test_create_normalizes_names_deduplicates_requests_and_keeps_duplicate_names_distinct(workspace):
    store, projects = workspace
    first = projects.create("  Identical name  ", "request-a")
    assert projects.create("Identical name", "request-a") == first
    second = projects.create("Identical name", "request-b")
    assert first["id"] != second["id"]
    assert first["created_at"] == first["updated_at"]
    with pytest.raises(ValueError, match="different payload"):
        projects.create("Changed", "request-a")
    assert len(store.list("projects")) == 2


@pytest.mark.parametrize("name", ["", "  ", None, 1, "x" * 201, "name\nline", "name\x00", "name\x7f"])
def test_project_name_validation(workspace, name):
    store, projects = workspace
    with pytest.raises(ValueError, match="name"):
        projects.create(name, "request")
    assert store.list("projects") == []


@pytest.mark.parametrize("request_id", ["", "../path", "x" * 101, None, 12])
def test_project_request_validation(workspace, request_id):
    store, projects = workspace
    with pytest.raises(ValueError, match="Request ID"):
        projects.create("Game", request_id)
    assert not store.list("projects")


def test_concurrent_retries_commit_one_project_and_conflicting_requests_do_not_overwrite(workspace):
    store, projects = workspace
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: projects.create("One", "same-request"), range(8)))
    assert len({result["id"] for result in results}) == len(store.list("projects")) == 1


def test_membership_derived_from_build_and_rejects_cross_project_or_absent_records(workspace):
    store, projects = workspace
    first, second = projects.create("One", "one"), projects.create("Two", "two")
    store.put("builds", {"id": "build-a", "project_id": first["id"]})
    store.put("agents", {"id": "agent-a", "build_id": "build-a"})
    assert projects.project_for_agent("agent-a") == first["id"]
    assert projects.project_for_build("build-a", first["id"]) == first["id"]
    with pytest.raises(ValueError, match="selected project"):
        projects.project_for_agent("agent-a", second["id"])
    store.put("agents", {"id": "agent-a", "build_id": "build-a", "project_id": second["id"]})
    with pytest.raises(ValueError, match="different projects"):
        projects.project_for_agent("agent-a")
    with pytest.raises(ValueError, match="Unknown builds"):
        projects.project_for_build("absent")
    store.put("builds", {"id": "legacy"})
    with pytest.raises(ValueError, match="no project"):
        projects.project_for_build("legacy")
    assert projects.project_for_build("legacy", allow_unassigned=True) is None
    store.put("agents", {"id": "legacy-agent", "build_id": "legacy", "project_id": first["id"]})
    with pytest.raises(ValueError, match="different projects"):
        projects.project_for_agent("legacy-agent", allow_unassigned=True)


@pytest.mark.parametrize("field,value", [("artifact_sha256", "changed"), ("source_revision", "rev-2"),
                                        ("engine_sha256", "changed-engine"), ("command", ["other-executable"])])
def test_existing_build_identity_is_immutable_even_without_references(workspace, field, value):
    store, projects = workspace
    project = projects.create("Game", "request")
    original = {"id": "build-a", "name": "First", "source_revision": "rev-1", "artifact_sha256": "hash", "command": ["fixture"]}
    saved = projects.register_build(original, project["id"])
    assert projects.register_build({**original, "name": "Renamed"}, project["id"])["name"] == "Renamed"
    with pytest.raises(ValueError, match="new build ID"):
        projects.register_build({**original, field: value}, project["id"])
    assert store.require("builds", "build-a")["artifact_sha256"] == saved["artifact_sha256"]
    assert projects.register_build({**original, "id": "new-build", field: value}, project["id"])["id"] == "new-build"


def test_registration_rejects_cross_project_references_and_reassignment(workspace):
    store, projects = workspace
    first, second = projects.create("One", "one"), projects.create("Two", "two")
    build = {"id": "build-a", "source_revision": "r"}
    with pytest.raises(ValueError, match="Select a project"):
        projects.register_build(build)
    store.put("builds", build)
    store.put("agents", {"id": "agent-a", "build_id": "build-a", "project_id": second["id"]})
    with pytest.raises(ValueError, match="Referenced agent"):
        projects.register_build(build, first["id"])
    projects.register_build(build, second["id"])
    with pytest.raises(ValueError, match="cannot move"):
        projects.register_build(build, first["id"])


def test_preview_never_writes_infers_names_or_discards_orphaned_history(workspace):
    store, projects = workspace
    groups = seed(store)
    before = projects._token()
    preview = projects.preview(groups)
    assert projects._token() == before
    assert not preview["blockers"]
    assert "not a complete workspace backup" in preview["backup_scope"]
    assert preview["assignments"]["builds"] == {"build-a": "game-a", "build-b": "game-b"}
    assert preview["assignments"]["runs"] == {"run-a": "game-a"}
    assert {item["id"] for item in preview["unassigned"]["runs"]} == {"run-missing", "run-orphan", "run-ambiguous"}
    assert [item["id"] for item in preview["unassigned"]["agents"]] == ["agent-orphan"]
    assert projects.preview([])["assignments"]["runs"] == {}
    assert store.list("projects") == []


def test_missing_current_build_can_use_explicit_retained_stable_id(workspace, tmp_path):
    store, projects = workspace
    seed(store)
    groups = [grouping("missing-build")]
    preview = projects.preview(groups)
    assert preview["assignments"]["runs"] == {"run-missing": "game-a"}
    migrate(projects, groups, tmp_path)
    assert store.require("runs", "run-missing")["project_id"] == "game-a"
    assert store.get("builds", "missing-build") is None
    with pytest.raises(ValueError, match="Unknown builds"):
        projects.project_for_agent("agent-orphan")


def test_migration_backup_preserves_raw_evidence_snapshots_counts_and_historical_concurrency(workspace, tmp_path):
    store, projects = workspace
    groups = seed(store)
    kinds = ["builds", "agents", "runs", "settings", "usage", "workers", "accounts"]
    before = {kind: {record["id"]: record for record in store.list(kind)} for kind in kinds}
    ledger = store.run_dir("run-a") / "ledger.jsonl"
    raw_ledger = ledger.read_bytes()
    evidence = {str(path.relative_to(store.root)): path.read_bytes() for path in (store.root / "runs").rglob("*") if path.is_file()}
    raw_events = [tuple(row) for row in store.db.execute("SELECT * FROM events")]
    codex = tmp_path / "codex-fixture"
    codex.mkdir()
    (codex / "auth.json").write_text('"DO-NOT-COPY-CODEX-CREDENTIAL"')
    registration = tmp_path / "workers-fixture.json"
    registration.write_text('{"token":"DO-NOT-READ-REGISTRATION-CREDENTIAL"}')
    result = migrate(projects, groups, tmp_path, codex_home=codex, registration_paths=[registration])
    assert result["version"] == 1
    backup = tmp_path / "backup"
    inventory = json.loads((backup / "inventory.json").read_text(encoding="utf-8"))
    assert inventory["codex_home"] == {"path": str(codex.resolve()), "exists": True, "copied": False}
    assert inventory["registration_paths"][0]["copied"] is False
    assert "DO-NOT-" not in (backup / "inventory.json").read_text()
    assert not (backup / "codex-fixture").exists()
    assert not (backup / "workers-fixture.json").exists()
    assert "not copied" in " ".join(inventory["limitations"])
    for relative, content in evidence.items():
        assert (store.root / relative).read_bytes() == (backup / relative).read_bytes() == content
    assert hashlib.sha256(ledger.read_bytes()).digest() == hashlib.sha256(raw_ledger).digest()
    assert [tuple(row) for row in store.db.execute("SELECT * FROM events")] == raw_events
    for kind, records in before.items():
        current = {record["id"]: record for record in store.list(kind)}
        assert current.keys() == records.keys()
        for record_id, original in records.items():
            assert {key: value for key, value in current[record_id].items() if key != "project_id"} == original
    assert store.require("settings", "main")["concurrent_sessions"] == 12
    assert store.command("historical", "cmd-a") == {"run_id": "run-a"}
    with sqlite3.connect(backup / "ashad.sqlite3") as saved:
        assert saved.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert saved.execute("SELECT count(*) FROM records WHERE kind='projects'").fetchone()[0] == 0
        old_run = json.loads(saved.execute("SELECT data FROM records WHERE kind='runs' AND id='run-a'").fetchone()[0])
        assert old_run == before["runs"]["run-a"]
        assert saved.execute("SELECT count(*) FROM events").fetchone()[0] == len(raw_events)
    unchanged = projects._token()
    assert migrate(projects, list(reversed(groups)), tmp_path) == result
    assert projects._token() == unchanged
    assert len(store.list("projects")) == 2
    assert projects.preview(groups)["already_applied"] is True


@pytest.mark.parametrize("state", ["Queued", "Starting", "Running", "Paused", "Investigating", "Stopping", "Unknown"])
def test_migration_requires_idle_lifecycle(workspace, tmp_path, state):
    store, projects = workspace
    groups = seed(store)
    store.put("runs", {"id": "busy", "state": state})
    with pytest.raises(ValueError, match="Migration blocked"):
        migrate(projects, groups, tmp_path)
    assert not (tmp_path / "backup").exists()
    assert not store.list("projects")


@pytest.mark.parametrize("ownership", [{"players": {"p": {"bound": True}}}, {"players": {"p": {"launched": True}}},
    {"players": {"p": {"phase": "teardown_unknown"}}}, {"players": {"p": {"teardown_confirmed": False}}},
    {"human_control": {"player_id": "p"}}, {"external_control": True}])
def test_ended_is_not_proof_of_released_ownership(workspace, tmp_path, ownership):
    store, projects = workspace
    groups = seed(store)
    store.put("runs", {"id": "unresolved", "state": "Ended", **ownership})
    with pytest.raises(ValueError, match="unresolved ownership"):
        migrate(projects, groups, tmp_path)


def test_orphaned_leases_and_provider_operations_block_migration(workspace, tmp_path):
    store, projects = workspace
    groups = seed(store)
    assert store.reserve("missing-owner", [("worker", "worker-a")])
    with pytest.raises(ValueError, match="leases remain"):
        migrate(projects, groups, tmp_path)
    store.release("missing-owner")
    with pytest.raises(ValueError, match="operations are active"):
        migrate(projects, groups, tmp_path, operations_active=True)


def test_confirmation_stale_preview_and_backup_destination_guards(workspace, tmp_path):
    store, projects = workspace
    groups = seed(store)
    preview = projects.preview(groups)
    kwargs = {"preview_token": preview["preview_token"], "backup_dir": tmp_path / "backup"}
    with pytest.raises(ValueError, match="confirmation"):
        projects.apply(groups, **kwargs)
    store.command("other", "changed", {"done": True})
    with pytest.raises(ValueError, match="stale"):
        projects.apply(groups, **kwargs, confirmed=True)
    kwargs["preview_token"] = projects.preview(groups)["preview_token"]
    kwargs["backup_dir"] = store.root / "backup"
    with pytest.raises(ValueError, match="outside"):
        projects.apply(groups, **kwargs, confirmed=True)
    assert not (store.root / "backup").exists()


def test_preview_token_binds_grouping_not_just_source_database(workspace, tmp_path):
    store, projects = workspace
    groups = seed(store)
    preview = projects.preview(groups)
    changed = [grouping("build-a", "build-b", project_id="merged-explicitly")]
    assert projects.preview(changed)["preview_token"] != preview["preview_token"]
    with pytest.raises(ValueError, match="stale"):
        projects.apply(changed, preview_token=preview["preview_token"], backup_dir=tmp_path / "backup", confirmed=True)
    assert not (tmp_path / "backup").exists()


def test_invalid_ambiguous_or_conflicting_groups_do_not_mutate(workspace, tmp_path):
    store, projects = workspace
    groups = seed(store)
    for invalid in [None, {}, [{"project_id": "game"}], [grouping("build-a", "build-a")],
                    [grouping("build-a"), grouping("build-a", project_id="other")]]:
        with pytest.raises(ValueError):
            projects.preview(invalid)
    with pytest.raises(ValueError, match="no stable"):
        migrate(projects, [grouping("unknown-build")], tmp_path)
    migrate(projects, groups, tmp_path)
    changed = copy.deepcopy(groups)
    changed[0]["name"] = "Different"
    with pytest.raises(ValueError, match="cannot rename"):
        projects.preview(changed)
    with pytest.raises(ValueError, match="different grouping"):
        migrate(projects, [grouping("build-a", project_id="another")], tmp_path)


def test_interruption_rolls_back_all_membership_and_retry_reuses_verified_backup(workspace, tmp_path, monkeypatch):
    store, projects = workspace
    groups = seed(store)
    before = projects._token()
    original = store.put
    def interrupted(kind, record, key=None):
        if kind == "migrations":
            raise KeyboardInterrupt("simulated interruption before migration marker commit")
        return original(kind, record, key)
    monkeypatch.setattr(store, "put", interrupted)
    with pytest.raises(KeyboardInterrupt):
        migrate(projects, groups, tmp_path)
    assert projects._token() == before
    assert not store.list("projects")
    assert store.get("migrations", MIGRATION_ID) is None
    inventory = (tmp_path / "backup/inventory.json").read_bytes()
    monkeypatch.setattr(store, "put", original)
    migrate(projects, groups, tmp_path)
    assert (tmp_path / "backup/inventory.json").read_bytes() == inventory
    assert store.require("runs", "run-a")["project_id"] == "game-a"


def test_backup_failure_changes_no_metadata_and_never_overwrites_partial_backup(workspace, tmp_path, monkeypatch):
    store, projects = workspace
    groups = seed(store)
    before = projects._token()
    def failed_copy(*args):
        raise OSError("fixture backup disk error")
    monkeypatch.setattr("ashad.projects.shutil.copy2", failed_copy)
    with pytest.raises(OSError, match="disk error"):
        migrate(projects, groups, tmp_path)
    assert projects._token() == before
    assert (tmp_path / "backup/ashad.sqlite3").exists()
    with pytest.raises(ValueError, match="incomplete"):
        migrate(projects, groups, tmp_path)


def test_changes_during_backup_abort_before_any_metadata_assignment(workspace, tmp_path, monkeypatch):
    store, projects = workspace
    groups = seed(store)
    backup = projects._backup
    def concurrent_change(*args):
        result = backup(*args)
        store.command("other", "concurrent", {"changed": True})
        return result
    monkeypatch.setattr(projects, "_backup", concurrent_change)
    with pytest.raises(ValueError, match="changed during backup"):
        migrate(projects, groups, tmp_path)
    assert not store.list("projects")
    assert "project_id" not in store.require("runs", "run-a")


def test_passive_status_refresh_preserves_preview_but_durable_state_changes_invalidate_it(workspace, tmp_path):
    store, projects = workspace
    groups = seed(store)
    preview = projects.preview(groups)
    for timestamp in (1, 2, 3):
        store.put("settings", {"timestamp": timestamp}, "coordinator_heartbeat")
        store.put("settings", {"status": "ready", "checked_at": timestamp}, "ai_status")
        assert projects.preview(groups)["preview_token"] == preview["preview_token"]
    result = projects.apply(groups, preview_token=preview["preview_token"], backup_dir=tmp_path / "backup", confirmed=True)
    assert result["assignments"]["runs"]["run-a"] == "game-a"
    current = projects.preview(groups)["preview_token"]
    store.put("settings", {"id": "main", "concurrent_sessions": 11})
    assert projects.preview(groups)["preview_token"] != current
    current = projects.preview(groups)["preview_token"]
    assert store.reserve("new-owner", [("worker", "worker-a")])
    assert projects.preview(groups)["preview_token"] != current
    assert projects.preview(groups)["blockers"]


def test_corrupted_completed_backup_is_rejected_without_committing_migration(workspace, tmp_path, monkeypatch):
    store, projects = workspace
    groups = seed(store)
    original = store.put
    def interrupted(kind, record, key=None):
        if kind == "migrations":
            raise KeyboardInterrupt("fixture interruption")
        return original(kind, record, key)
    monkeypatch.setattr(store, "put", interrupted)
    with pytest.raises(KeyboardInterrupt):
        migrate(projects, groups, tmp_path)
    monkeypatch.setattr(store, "put", original)
    (tmp_path / "backup/runs/run-a/ledger.jsonl").write_bytes(b"corrupted fixture backup")
    with pytest.raises(ValueError, match="verification failed"):
        migrate(projects, groups, tmp_path)
    assert not store.list("projects")


def test_unreadable_evidence_directory_is_not_silently_omitted(workspace, tmp_path, monkeypatch):
    store, projects = workspace
    groups = seed(store)
    def unreadable_walk(root, **options):
        options["onerror"](PermissionError("fixture unreadable evidence"))
        yield root, [], []
    monkeypatch.setattr("ashad.projects.os.walk", unreadable_walk)
    with pytest.raises(PermissionError, match="unreadable evidence"):
        migrate(projects, groups, tmp_path)
    assert not store.list("projects")


@pytest.mark.parametrize("damage", ["empty", "missing-db", "missing-evidence", "duplicate", "traversal", "wrong-root",
                                    "wrong-preview", "wrong-grouping", "wrong-size", "wrong-hash", "invalid-json"])
def test_edited_inventory_cannot_authorize_migration(workspace, tmp_path, damage):
    store, projects = workspace
    groups = seed(store)
    preview = projects.preview(groups)
    backup = projects._backup(tmp_path / "backup", preview, None, ())
    manifest = backup / "inventory.json"
    inventory = json.loads(manifest.read_text())
    entries = inventory["copied_files"]
    if damage == "empty":
        inventory["copied_files"] = []
    elif damage == "missing-db":
        inventory["copied_files"] = [item for item in entries if item["path"] != "ashad.sqlite3"]
    elif damage == "missing-evidence":
        inventory["copied_files"] = entries[:1]
    elif damage == "duplicate":
        entries.append(entries[0])
    elif damage == "traversal":
        entries[0]["path"] = "../source/ashad.sqlite3"
    elif damage == "wrong-root":
        inventory["source_root"] = str(tmp_path / "other-source")
    elif damage == "wrong-preview":
        inventory["preview_token"] = "other"
    elif damage == "wrong-grouping":
        inventory["grouping_hash"] = "other"
    elif damage == "wrong-size":
        entries[0]["bytes"] += 1
    elif damage == "wrong-hash":
        entries[0]["sha256"] = "0" * 64
    manifest.write_text("not-json" if damage == "invalid-json" else json.dumps(inventory))
    with pytest.raises(ValueError, match="verification failed"):
        migrate(projects, groups, tmp_path)
    assert store.list("projects") == []
    assert store.get("migrations", MIGRATION_ID) is None
    assert "project_id" not in store.require("runs", "run-a")


@pytest.mark.parametrize("damage", ["missing-db", "missing-evidence", "extra-backup-file", "added-source-file", "changed-source-file", "changed-db"])
def test_backup_is_compared_with_actual_source_and_complete_file_tree(workspace, tmp_path, damage):
    store, projects = workspace
    groups = seed(store)
    backup = projects._backup(tmp_path / "backup", projects.preview(groups), None, ())
    if damage in {"missing-db", "missing-evidence"}:
        removed = backup / ("ashad.sqlite3" if damage == "missing-db" else "runs/run-a/ledger.jsonl")
        assert removed.resolve().is_relative_to((tmp_path / "backup").resolve())
        removed.unlink()
    elif damage == "extra-backup-file":
        (backup / "unlisted.txt").write_text("unexpected fixture")
    elif damage == "added-source-file":
        (store.run_dir("run-a") / "new-evidence.txt").write_text("new fixture evidence")
    elif damage == "changed-source-file":
        (store.run_dir("run-a") / "frozen-plan.json").write_text("changed fixture evidence")
    else:
        from contextlib import closing
        with closing(sqlite3.connect(backup / "ashad.sqlite3", isolation_level=None)) as database:
            database.execute("DELETE FROM events")
        manifest = backup / "inventory.json"
        inventory = json.loads(manifest.read_text())
        database = backup / "ashad.sqlite3"
        inventory["copied_files"][0].update(sha256=hashlib.sha256(database.read_bytes()).hexdigest(), bytes=database.stat().st_size)
        manifest.write_text(json.dumps(inventory))
    with pytest.raises(ValueError, match="verification failed"):
        migrate(projects, groups, tmp_path)
    assert store.list("projects") == []
    assert store.get("migrations", MIGRATION_ID) is None


def test_backup_is_closed_before_hashing_and_uses_verified_standalone_delete_journal(workspace, tmp_path, monkeypatch):
    from contextlib import closing
    from ashad import projects as module
    store, projects = workspace
    groups = seed(store)
    real_connect, real_hash = sqlite3.connect, module._file_hash
    connections = []
    class TrackedConnection(sqlite3.Connection):
        closed = False
        def close(self):
            super().close()
            self.closed = True
    def connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs, factory=TrackedConnection)
        connections.append(connection)
        return connection
    def checked_hash(path):
        if path.name == "ashad.sqlite3":
            assert connections and all(connection.closed for connection in connections)
        return real_hash(path)
    monkeypatch.setattr(module.sqlite3, "connect", connect)
    monkeypatch.setattr(module, "_file_hash", checked_hash)
    backup = projects._backup(tmp_path / "backup", projects.preview(groups), None, ())
    assert len(connections) == 2 and all(connection.closed for connection in connections)
    with closing(real_connect(backup / "ashad.sqlite3")) as database:
        assert database.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert database.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert store.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert not any((backup / ("ashad.sqlite3" + suffix)).exists() for suffix in ("-wal", "-shm", "-journal"))


@pytest.mark.parametrize("location", ["source-evidence", "backup-database", "backup-manifest"])
def test_hardlinked_backup_or_source_files_are_rejected(workspace, tmp_path, location):
    store, projects = workspace
    groups = seed(store)
    if location == "source-evidence":
        target = tmp_path / "fixture-private.txt"
        target.write_text("fixture-only outside evidence")
        (store.run_dir("run-a") / "linked.txt").hardlink_to(target)
    else:
        backup = projects._backup(tmp_path / "backup", projects.preview(groups), None, ())
        target = backup / ("ashad.sqlite3" if location == "backup-database" else "inventory.json")
        (tmp_path / "hardlink-fixture").hardlink_to(target)
    original = target.read_bytes()
    with pytest.raises(ValueError, match="hardlinked"):
        migrate(projects, groups, tmp_path)
    assert target.read_bytes() == original
    assert not store.list("projects")


@pytest.mark.parametrize("link_type", ["symlink", "reparse"])
@pytest.mark.parametrize("location", ["destination-parent", "source-evidence"])
def test_link_and_reparse_ancestors_are_rejected_before_backup_writes(workspace, tmp_path, monkeypatch, link_type, location):
    from pathlib import Path
    import stat
    from types import SimpleNamespace
    store, projects = workspace
    groups = seed(store)
    destination = tmp_path / "fixture-parent" / "backup"
    destination.parent.mkdir()
    unsafe = destination.parent if location == "destination-parent" else store.root / "runs/run-a"
    original = Path.lstat
    def linked_lstat(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == unsafe:
            return SimpleNamespace(st_mode=stat.S_IFLNK if link_type == "symlink" else info.st_mode,
                                   st_nlink=info.st_nlink, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT if link_type == "reparse" else 0)
        return info
    monkeypatch.setattr(Path, "lstat", linked_lstat)
    with pytest.raises(ValueError, match="links and reparse"):
        projects.apply(groups, preview_token=projects.preview(groups)["preview_token"], backup_dir=destination, confirmed=True)
    assert not destination.exists()
    assert not store.list("projects")


def test_backup_destination_parent_traversal_is_rejected(workspace, tmp_path):
    store, projects = workspace
    groups = seed(store)
    with pytest.raises(ValueError, match="parent traversal"):
        projects.apply(groups, preview_token=projects.preview(groups)["preview_token"],
                       backup_dir=tmp_path / "parent" / ".." / "backup", confirmed=True)
    assert not (tmp_path / "backup").exists()
    assert not store.list("projects")


def test_identical_backup_retry_tolerates_passive_diagnostics_refresh(workspace, tmp_path):
    store, projects = workspace
    groups = seed(store)
    preview = projects.preview(groups)
    backup = projects._backup(tmp_path / "backup", preview, None, ())
    inventory = (backup / "inventory.json").read_bytes()
    store.put("settings", {"timestamp": 999}, "coordinator_heartbeat")
    store.put("settings", {"status": "refreshed"}, "ai_status")
    result = projects.apply(groups, preview_token=preview["preview_token"], backup_dir=backup, confirmed=True)
    assert result["id"] == MIGRATION_ID
    assert (backup / "inventory.json").read_bytes() == inventory


def test_reused_wal_database_is_not_accepted_as_standalone_even_with_matching_hash(workspace, tmp_path):
    from contextlib import closing
    store, projects = workspace
    groups = seed(store)
    backup = projects._backup(tmp_path / "backup", projects.preview(groups), None, ())
    database = backup / "ashad.sqlite3"
    with closing(sqlite3.connect(database, isolation_level=None)) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    manifest = backup / "inventory.json"
    inventory = json.loads(manifest.read_text())
    inventory["copied_files"][0].update(sha256=hashlib.sha256(database.read_bytes()).hexdigest(), bytes=database.stat().st_size)
    manifest.write_text(json.dumps(inventory))
    with pytest.raises(ValueError, match="verification failed"):
        migrate(projects, groups, tmp_path)
    assert not store.list("projects")


def test_boolean_size_in_edited_inventory_is_not_an_integer_measurement(workspace, tmp_path):
    store, projects = workspace
    groups = seed(store)
    (store.run_dir("run-a") / "empty-evidence.txt").write_bytes(b"")
    backup = projects._backup(tmp_path / "backup", projects.preview(groups), None, ())
    manifest = backup / "inventory.json"
    inventory = json.loads(manifest.read_text())
    next(item for item in inventory["copied_files"] if item["path"].endswith("empty-evidence.txt"))["bytes"] = False
    manifest.write_text(json.dumps(inventory))
    with pytest.raises(ValueError, match="invalid file inventory"):
        migrate(projects, groups, tmp_path)
    assert not store.list("projects")
