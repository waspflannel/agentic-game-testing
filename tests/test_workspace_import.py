"""Workspace import uses fresh controller fixtures only; never live data/keyring."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from ashad import workspace_import as importer
from ashad.process_lock import acquire
from ashad.store import Store


@pytest.fixture
def workspace(tmp_path):
    source = tmp_path / "legacy fixture"
    with closing(Store(source)) as old:
        old.put("settings", {"id": "main", "concurrent_sessions": 8})
        old.put("builds", {"id": "build-1", "manifest_path": str(tmp_path / "external game" / "manifest.json")})
        old.put("agents", {"id": "agent-1", "build_id": "build-1", "instructions": "Immutable manual"})
        old.put("workers", {"id": "worker-1", "credential_ref": "worker:fixture", "enabled": True})
        old.put("runs", {"id": "run-1", "state": "Ended", "players": {}, "plan": {"build_id": "build-1", "primary_goal": "Fixture only"}})
        old.put("usage", {"id": "usage-1", "run_id": "run-1", "estimated_cost_usd": 1.25})
        old.event("run-1", "ended", {"reason": "fixture"})
        old.command("instance", "request-1", {"run_id": "run-1"})
        (old.run_dir("run-1") / "evidence.bin").write_bytes(bytes(range(256)))
    target = Store(tmp_path / "empty desktop fixture")
    target.put("settings", {"id": "main", "concurrent_sessions": 1})
    arguments = {"source_root": source, "backup_dir": tmp_path / "operator backup",
                 "external_inventory": {"codex_home": str(source / "codex"),
                     "worker_registration_paths": [str(tmp_path / "workers.local.json")],
                     "external_build_paths": [str(tmp_path / "external game")],
                     "docker_volumes": ["fixture-steam-volume"], "keyring_services": ["ASHAD.Bot.Local", "Codex"]},
                 "confirmed": True, "source_idle_confirmed": True,
                 "external_backup_confirmed": True, "target_drained": True}
    try:
        yield source, target, arguments
    finally:
        target.close()


def source_snapshot(source):
    with closing(sqlite3.connect(source / "ashad.sqlite3")) as db:
        return importer._snapshot(db)


def test_import_preserves_metadata_commands_ledger_evidence_and_replays(workspace):
    source, target, arguments = workspace
    before, evidence = source_snapshot(source), importer._tree(source / "runs")
    # These are inventories only: none of the external paths need exist.
    assert not (source / "codex").exists()
    result = importer.import_workspace(target, **arguments)
    assert importer._snapshot(target.db) == before == source_snapshot(source)
    assert importer._tree(target.root / "runs") == evidence == importer._tree(source / "runs")
    assert result["readiness_verified"] is False and result["reauthentication_required"] is True
    assert target.require("runs", "run-1")["plan"] == {"build_id": "build-1", "primary_goal": "Fixture only"}
    assert not target.list("projects")
    assert importer.import_workspace(target, **arguments) == result
    assert not (target.root / ".workspace-import-in-progress.json").exists()


def test_passive_status_refreshes_allow_replay_without_rewriting_any_rows(workspace):
    source, target, arguments = workspace
    with closing(Store(source)) as old:
        old.put("settings", {"id": "coordinator_heartbeat", "timestamp": 1})
        old.put("settings", {"id": "ai_status", "status": "configuration_required"})
    original = source_snapshot(source)
    result = importer.import_workspace(target, **arguments)
    assert importer._snapshot(target.db) == original  # Volatile rows are still copied exactly.
    backup = arguments["backup_dir"] / "ashad.sqlite3"
    backup_hash = importer._file_hash(backup)
    with closing(sqlite3.connect(backup)) as db:
        assert importer._snapshot(db) == original
    for tick, status in enumerate(("ready", "login_pending", "configuration_required"), 2):
        with closing(Store(source)) as old:
            old.put("settings", {"id": "coordinator_heartbeat", "timestamp": tick})
            old.put("settings", {"id": "ai_status", "status": "not_validated", "timestamp": tick})
        target.put("settings", {"id": "coordinator_heartbeat", "timestamp": tick + 100})
        target.put("settings", {"id": "ai_status", "status": status, "timestamp": tick})
        source_before, target_before = source_snapshot(source), importer._snapshot(target.db)
        marker_before = tuple(target.db.execute("SELECT data,updated FROM records WHERE kind='migrations' AND id=?", (importer.IMPORT_ID,)).fetchone())
        assert importer.import_workspace(target, **arguments) == result
        assert source_snapshot(source) == source_before
        assert importer._snapshot(target.db) == target_before  # Never reset a newer login/status.
        assert tuple(target.db.execute("SELECT data,updated FROM records WHERE kind='migrations' AND id=?", (importer.IMPORT_ID,)).fetchone()) == marker_before
        assert importer._file_hash(backup) == backup_hash


@pytest.mark.parametrize("changed", ["source", "target"])
def test_durable_main_settings_still_invalidate_import_replay(workspace, changed):
    source, target, arguments = workspace
    importer.import_workspace(target, **arguments)
    if changed == "source":
        with closing(Store(source)) as old:
            old.put("settings", {"id": "main", "concurrent_sessions": 2})
    else:
        target.put("settings", {"id": "main", "concurrent_sessions": 2})
    before = importer._snapshot(target.db)
    with pytest.raises(ValueError, match="different source|target changed"):
        importer.import_workspace(target, **arguments)
    assert importer._snapshot(target.db) == before


@pytest.mark.parametrize("flag", ["confirmed", "source_idle_confirmed", "external_backup_confirmed", "target_drained"])
def test_all_explicit_confirmations_are_required(workspace, flag):
    source, target, arguments = workspace
    arguments[flag] = False
    with pytest.raises(ValueError, match="confirmations"):
        importer.import_workspace(target, **arguments)
    assert not arguments["backup_dir"].exists()


@pytest.mark.parametrize("key,record", [
    ("../codex", {"id": "../codex", "state": "Ended"}),
    ("..\\codex", {"id": "..\\codex", "state": "Ended"}),
    ("run_é", {"id": "run_é", "state": "Ended"}),
    ("run_key", {"id": "run_other", "state": "Ended"}),
    ("run_missing", {"state": "Ended"}),
    ("run_list", []), ("run_string", "not an object"), ("run_null", None),
])
def test_malformed_run_identity_refused_before_backup_or_target_changes(workspace, key, record):
    source, target, arguments = workspace
    with closing(Store(source)) as old:
        old.put("runs", record, key=key)  # No ledger/events exist for this malformed row.
    source_before, target_before = source_snapshot(source), importer._snapshot(target.db)
    with pytest.raises(ValueError, match="Source run"):
        importer.import_workspace(target, **arguments)
    assert not arguments["backup_dir"].exists()
    assert importer._snapshot(target.db) == target_before
    assert source_snapshot(source) == source_before
    assert not list((target.root / "runs").iterdir())
    assert not (target.root / ".workspace-import-in-progress.json").exists()


@pytest.mark.parametrize("state,players", [("Queued", {}), ("Running", {}), ("Paused", {}),
    ("Ended", {"p": {"bound": True}}), ("Ended", {"p": {"teardown_confirmed": False}})])
def test_source_owners_block_import(workspace, state, players):
    source, target, arguments = workspace
    with closing(Store(source)) as old:
        old.put("runs", {"id": "run-1", "state": state, "players": players})
    with pytest.raises(ValueError, match="ownership"):
        importer.import_workspace(target, **arguments)
    assert not target.list("runs") and not arguments["backup_dir"].exists()


@pytest.mark.parametrize("blocker", ["lease", "provider_record", "provider_event"])
def test_ambiguous_source_operations_block_import(workspace, blocker):
    source, target, arguments = workspace
    with closing(Store(source)) as old:
        if blocker == "lease":
            old.reserve("missing-run", [("worker", "w")])
        elif blocker == "provider_record":
            old.put("provider_requests", {"id": "request", "status": "requesting"})
        else:
            old.event("run-1", "provider_request_started", {"id": "unmatched-request"})
    with pytest.raises(ValueError, match="leases|provider"):
        importer.import_workspace(target, **arguments)
    assert not target.list("runs")


@pytest.mark.parametrize("name", ["controller.lock", "watchdog.lock"])
def test_live_source_lock_rejected_by_independent_importer(workspace, name):
    source, target, arguments = workspace
    payload = {**arguments, "source_root": str(source), "backup_dir": str(arguments["backup_dir"])}
    code = ("import json,sys; from ashad.store import Store; from ashad.workspace_import import import_workspace; "
            "s=Store(sys.argv[1]); import_workspace(s, **json.loads(sys.argv[2]))")
    with acquire(source / name):
        result = subprocess.run([sys.executable, "-B", "-c", code, str(target.root), json.dumps(payload)],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15)
    assert result.returncode != 0 and "Another ASHAD process owns" in result.stderr
    assert not target.list("runs")


@pytest.mark.parametrize("kind", ["record", "command", "event_file", "backup", "interruption"])
def test_existing_target_or_backup_never_overwritten(workspace, kind):
    source, target, arguments = workspace
    if kind == "record":
        target.put("projects", {"id": "mine", "name": "Keep me"})
    elif kind == "command":
        target.command("project", "mine", {"id": "keep"})
    elif kind == "event_file":
        (target.root / "runs" / "keep.txt").write_text("keep")
    elif kind == "backup":
        arguments["backup_dir"].mkdir()
        (arguments["backup_dir"] / "keep.txt").write_text("keep")
    else:
        (target.root / ".workspace-import-in-progress.json").write_text("interrupted")
    before = importer._snapshot(target.db)
    with pytest.raises(ValueError):
        importer.import_workspace(target, **arguments)
    assert importer._snapshot(target.db) == before


@pytest.mark.parametrize("changed", ["source", "target", "backup", "request"])
def test_replay_refuses_changed_payload_or_corruption(workspace, changed):
    source, target, arguments = workspace
    importer.import_workspace(target, **arguments)
    if changed == "source":
        with closing(Store(source)) as old:
            old.put("agents", {"id": "new", "instructions": "changed"})
    elif changed == "target":
        target.put("projects", {"id": "new", "name": "New user data"})
    elif changed == "backup":
        (arguments["backup_dir"] / "runs" / "run-1" / "evidence.bin").write_bytes(b"damaged")
    else:
        arguments["external_inventory"]["docker_volumes"].append("different")
    before = importer._snapshot(target.db)
    with pytest.raises(ValueError):
        importer.import_workspace(target, **arguments)
    assert importer._snapshot(target.db) == before


def test_keyboard_interrupt_rolls_back_sql_and_published_evidence(workspace, monkeypatch):
    source, target, arguments = workspace
    before = importer._snapshot(target.db)
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt("fixture interruption before commit")
    monkeypatch.setattr(target, "put", interrupt)
    with pytest.raises(KeyboardInterrupt):
        importer.import_workspace(target, **arguments)
    assert importer._snapshot(target.db) == before
    assert not list((target.root / "runs").iterdir())
    assert (arguments["backup_dir"] / "inventory.json").is_file()
    assert not (target.root / ".workspace-import-in-progress.json").exists()
    with pytest.raises(ValueError, match="fresh directory"):
        importer.import_workspace(target, **arguments)


def test_evidence_changed_during_backup_leaves_target_untouched(workspace, monkeypatch):
    source, target, arguments = workspace
    original = importer.shutil.copyfileobj
    def changing(reader, writer):
        original(reader, writer)
        (source / "runs" / "run-1" / "added.bin").write_bytes(b"changed")
    monkeypatch.setattr(importer.shutil, "copyfileobj", changing)
    with pytest.raises(ValueError, match="changed"):
        importer.import_workspace(target, **arguments)
    assert not target.list("runs") and not list((target.root / "runs").iterdir())
    assert not (arguments["backup_dir"] / "inventory.json").exists()


def test_evidence_links_refused_without_copying_external_content(workspace, tmp_path):
    source, target, arguments = workspace
    outside = tmp_path / "not evidence"
    outside.mkdir()
    (outside / "private.txt").write_text("must never copy")
    link = source / "runs" / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            raise
        import _winapi
        _winapi.CreateJunction(str(outside), str(link))
    with pytest.raises(ValueError, match="reparse|Symlink"):
        importer.import_workspace(target, **arguments)
    assert not arguments["backup_dir"].exists()


def test_relative_overlapping_paths_and_corrupt_ledgers_rejected(workspace):
    source, target, arguments = workspace
    for path in (Path("relative"), source, source / "nested", target.root):
        with pytest.raises(ValueError):
            importer.import_workspace(target, **{**arguments, "backup_dir": path})
    (source / "runs" / "run-1" / "ledger.jsonl").write_text("{}\n")
    with pytest.raises(ValueError, match="Malformed"):
        importer.import_workspace(target, **arguments)
    assert not arguments["backup_dir"].exists()


def test_plaintext_auth_and_external_home_are_never_read_or_copied(workspace):
    source, target, arguments = workspace
    (source / "codex").mkdir()
    (source / "codex" / "auth.json").write_text("not valid JSON: fixture credential must not be read")
    (source / "workers.local.json").write_text("not valid JSON: registration is inventory only")
    importer.import_workspace(target, **arguments)
    assert not (target.root / "codex").exists()
    assert not (target.root / "workers.local.json").exists()
    assert {path.name for path in arguments["backup_dir"].iterdir()} == {"ashad.sqlite3", "runs", "inventory.json"}


def test_source_external_path_inventory_must_be_complete(workspace):
    source, target, arguments = workspace
    arguments["external_inventory"]["external_build_paths"] = []
    with pytest.raises(ValueError, match="every recorded build path"):
        importer.import_workspace(target, **arguments)
    assert not arguments["backup_dir"].exists()


def test_abrupt_process_death_rolls_back_sql_and_blocks_partial_replay(workspace):
    source, target, arguments = workspace
    payload = {**arguments, "source_root": str(source), "backup_dir": str(arguments["backup_dir"])}
    before = importer._snapshot(target.db)
    code = ("import json,sys,os; from ashad.store import Store; from ashad.workspace_import import import_workspace; "
            "s=Store(sys.argv[1]); s.put=lambda *a,**k: os._exit(23); import_workspace(s, **json.loads(sys.argv[2]))")
    result = subprocess.run([sys.executable, "-B", "-c", code, str(target.root), json.dumps(payload)],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15)
    assert result.returncode == 23, result.stderr
    assert importer._snapshot(target.db) == before
    assert (target.root / ".workspace-import-in-progress.json").is_file()
    assert (target.root / "runs" / "run-1" / "evidence.bin").is_file()
    assert (arguments["backup_dir"] / "inventory.json").is_file()
    with pytest.raises(ValueError, match="Interrupted import"):
        importer.import_workspace(target, **arguments)
    assert importer._snapshot(target.db) == before
