"""Explicit, idle-only legacy controller import; never call during startup.

Caller MUST hold the target process lock and admission/provider/setup drain for
this entire call. Source controller/watchdog locks are acquired here. No worker,
provider, credential, external build, or Codex-home contents are accessed.
"""
from contextlib import ExitStack, closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time

from .process_lock import acquire
from .projects import (PASSIVE_SETTINGS, _copy_file, _digest, _file_hash, _identifier, _ownership_unresolved,
                       _safe_path, _unredacted)

IMPORT_ID = "workspace-import-v1"
TABLES = {"records": ("kind", "id", "data", "updated"),
          "leases": ("kind", "resource_id", "run_id"),
          "events": ("run_id", "seq", "id", "data"),
          "commands": ("scope", "id", "result")}
LIMITATIONS = [
    "Only controller SQLite and run evidence are copied; this is NOT a complete workspace backup.",
    "Codex home, worker registrations, external builds, OS keyring, Claude token and Docker volumes are inventory only.",
    "The new Codex home requires explicit sign-in; account and desktop readiness are not verified.",
    "Project/build grouping is unchanged; use Projects preview/apply for explicit membership mapping.",
    "Never run old and new controllers against the same worker/account pool concurrently.",
]


def _snapshot(db):
    objects = db.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
    if {tuple(row) for row in objects} != {("table", name) for name in TABLES}:
        raise ValueError("Unsupported source database schema; no tables may be silently dropped")
    for table, columns in TABLES.items():
        if tuple(row[1] for row in db.execute(f"PRAGMA table_info({table})")) != columns:
            raise ValueError("Unsupported source database columns")
    return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1,2")
                   if not (table == "records" and row[0:2] == ("migrations", IMPORT_ID))]
            for table in TABLES}


def _semantic_snapshot(snapshot):
    # Status polling changes neither imported ownership nor durable history.
    # Keep these rows in the raw snapshot for exact first-import/backup copies.
    return {**snapshot, "records": [row for row in snapshot["records"]
            if not (row[0] == "settings" and row[1] in PASSIVE_SETTINGS)]}


def _tree(root, hashed=True):
    """Evidence inventory; hashed=False is a cheap size/mtime change stamp, never stored."""
    _safe_path(root)
    if not root.is_dir():
        raise ValueError("Missing run evidence directory")
    files, directories = {}, []
    def unreadable(error):
        raise error
    for directory, folders, names in os.walk(root, followlinks=False, onerror=unreadable):
        for name in folders + names:
            path = _safe_path(Path(directory) / name)
            relative = path.relative_to(root).as_posix()
            if path.is_dir():
                directories.append(relative)
            elif path.is_file():
                info = path.stat()
                files[relative] = ({"sha256": _file_hash(path), "bytes": info.st_size} if hashed
                                   else {"bytes": info.st_size, "mtime_ns": info.st_mtime_ns})
            else:
                raise ValueError("Only regular evidence files/directories may be imported")
    return {"files": files, "directories": sorted(directories)}


def _idle(snapshot, root):
    if snapshot["leases"]:
        raise ValueError("Source has unreconciled leases")
    outstanding = set()
    for kind, key, raw, _ in snapshot["records"]:
        record = json.loads(raw)
        if kind == "runs":
            if not isinstance(record, dict) or record.get("id") != key:
                raise ValueError("Source run must be an object with an ID matching its record key")
            _identifier(record["id"], "Source run ID")  # Same folder-safe contract as Store.run_dir.
            if _ownership_unresolved(record):
                raise ValueError("Source has active or unresolved run ownership")
        if kind == "provider_requests" and record.get("status") not in {
            "completed", "request_cancelled", "provider_failure", "rate_limited", "invalid_authentication",
            "model_unavailable", "connection_failure", "configuration_required", "incomplete_response",
            "invalid_plan", "invalid_decision", "invalid_tuning", "context_limit"}:
            raise ValueError("Source has ambiguous provider requests")
        if kind == "settings" and key == "ai_status" and record.get("status") == "login_pending":
            raise ValueError("Source has an unreconciled provider sign-in")
    indexed = {}
    for run_id, seq, event_id, raw in snapshot["events"]:
        event = json.loads(raw)
        indexed[(run_id, seq)] = event
        request = event.get("payload", {}).get("id")
        if event.get("kind") in {"provider_request_started", "provider_request_finished"} and not isinstance(request, str):
            raise ValueError("Source has ambiguous provider event identity")
        if event.get("kind") == "provider_request_started":
            outstanding.add((run_id, request))
        elif event.get("kind") == "provider_request_finished":
            outstanding.discard((run_id, request))
    if outstanding:
        raise ValueError("Source has unmatched provider requests; reconcile before importing")
    # Validate retained ledgers without repairing/reformatting the source or its index.
    seen = {}
    for path in (root / "runs").glob("*/ledger.jsonl"):
        _safe_path(path)
        previous = None
        with path.open(encoding="utf-8") as ledger:
            for seq, line in enumerate(ledger, 1):
                event = json.loads(line)
                given = event.pop("hash")
                expected = hashlib.sha256(json.dumps(event, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                event["hash"] = given
                if given != expected or event.get("previous_hash") != previous or event.get("seq") != seq or event.get("run_id") != path.parent.name:
                    raise ValueError("Source ledger integrity check failed")
                seen[(path.parent.name, seq)] = event
                previous = given
    if seen != indexed:
        raise ValueError("Source ledger and SQLite event index disagree; reconcile first")


def _external_inventory(value):
    keys = {"codex_home", "worker_registration_paths", "external_build_paths", "docker_volumes", "keyring_services"}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("Explicit Codex/registration/build/volume/keyring inventory is required")
    if not isinstance(value["codex_home"], str) or not value["codex_home"].isprintable() or len(value["codex_home"]) > 4096 or not Path(value["codex_home"]).is_absolute():
        raise ValueError("Inventory must identify the absolute Codex home path")
    for key in keys - {"codex_home"}:
        if not isinstance(value[key], list) or any(not isinstance(item, str) or not item or len(item) > 4096 or not item.isprintable() for item in value[key]):
            raise ValueError("Inventory entries must be paths or resource identifiers, never credentials")
        if key.endswith("paths") and any(not Path(item).is_absolute() or ".." in Path(item).parts for item in value[key]):
            raise ValueError("External inventory paths must be absolute without traversal")
    # Do not stat, resolve, open, export or copy any of these external locations.
    return json.loads(json.dumps(value))


def _write_json(path, value):
    with path.open("x", encoding="utf-8") as output:
        json.dump(value, output, indent=2, ensure_ascii=False)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())


def _copy_runs(source, destination, evidence):
    """One read per file, verified against the evidence digest; callers stamp-check the source."""
    destination.mkdir()
    for directory in evidence["directories"]:
        (destination / directory).mkdir(parents=True, exist_ok=True)
    for relative, expected in evidence["files"].items():
        copied = destination / relative
        copied.parent.mkdir(parents=True, exist_ok=True)
        if _copy_file(source / relative, copied) != expected:
            raise ValueError("Source evidence changed during copying")
    written = _tree(destination, hashed=False)
    if written["directories"] != evidence["directories"] or written["files"].keys() != evidence["files"].keys():
        raise ValueError("Copied evidence does not match the source inventory")


def _verify_backup(backup, manifest, manifest_hash):
    _safe_path(backup)
    if _file_hash(_safe_path(backup / "inventory.json")) != manifest_hash:
        raise ValueError("Backup inventory verification failed")
    if {path.name for path in backup.iterdir()} != {"ashad.sqlite3", "runs", "inventory.json"}:
        raise ValueError("Backup contains unexpected or incomplete files")
    if _file_hash(_safe_path(backup / "ashad.sqlite3")) != manifest["database_sha256"] or _tree(backup / "runs") != manifest["evidence"]:
        raise ValueError("Backup data verification failed")


def import_workspace(store, *, source_root, backup_dir, external_inventory,
                     confirmed=False, source_idle_confirmed=False,
                     external_backup_confirmed=False, target_drained=False):
    """Import once or return the identical completed result; never resume a partial import.

    Every True flag is an explicit caller/operator attestation, not an ownership
    check. The caller must hold the target process lock and runtime drain. A crash
    leaves an interruption marker: refuse further import pending operator recovery.
    """
    if any(flag is not True for flag in (confirmed, source_idle_confirmed, external_backup_confirmed, target_drained)):
        raise ValueError("Explicit import, idle-source, external-backup and target-drain confirmations are required")
    source, target, backup = map(_safe_path, (source_root, store.root, backup_dir))
    for left, right in ((source, target), (source, backup), (target, backup)):
        if left.is_relative_to(right) or right.is_relative_to(left):
            raise ValueError("Source, target and backup must be distinct non-nested absolute roots")
    external = _external_inventory(external_inventory)
    for value in (str(source), str(target), str(backup), external):
        _unredacted(value)  # The completed marker must replay these values exactly.
    source_file = _safe_path(source / "ashad.sqlite3")
    _safe_path(target / "ashad.sqlite3")
    for root in (source, target):
        for suffix in ("-wal", "-shm", "-journal"):
            _safe_path(root / ("ashad.sqlite3" + suffix))
    _safe_path(target / "runs")
    if not source_file.is_file():
        raise ValueError("Source controller database does not exist")
    journal = target / ".workspace-import-in-progress.json"
    stage = target / ".workspace-import-runs"
    _safe_path(journal)
    _safe_path(stage)
    if journal.exists() and not stage.exists():
        # COMMIT succeeded but journal cleanup was lost (crash, AV lock): clear only a
        # journal that names exactly this target's committed marker, then replay below.
        marker = store.get("migrations", IMPORT_ID)
        try:
            recorded = json.loads(journal.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            recorded = None
        if marker and marker.get("target_root") == str(target) and recorded == {
                "source_identity": marker.get("source_identity"), "backup_dir": marker.get("backup_dir")}:
            journal.unlink()
    if journal.exists() or stage.exists():
        raise ValueError("Interrupted import detected; operator reconciliation is required")
    for name in ("controller.lock", "watchdog.lock"):
        _safe_path(source / name)
    # The caller's drain blocks admission. The target Store lock is held only to read the
    # target snapshot and for the publish transaction, never across copying or hashing.
    with ExitStack() as locks:
        locks.enter_context(acquire(source / "controller.lock"))
        locks.enter_context(acquire(source / "watchdog.lock"))
        db = locks.enter_context(closing(sqlite3.connect(source_file.as_uri() + "?mode=ro", uri=True)))
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Source SQLite integrity check failed")
        if db.execute("SELECT 1 FROM records WHERE kind='migrations' AND id=?", (IMPORT_ID,)).fetchone():
            raise ValueError("Source already has an import lineage; use the original legacy source")
        # ponytail: snapshot rows in memory; stream only if real history sizes require it.
        stamps = _tree(source / "runs", hashed=False)  # Taken first: any later write changes it.
        snapshot, evidence = _snapshot(db), _tree(source / "runs")
        if Path(external["codex_home"]) != source / "codex":
            raise ValueError("External backup confirmation must cover this source's Codex home")
        for kind, _, raw, _ in snapshot["records"]:
            if kind == "builds":
                build = json.loads(raw)
                for key in ("manifest_path", "source_root", "catalog_path", "brief_path"):
                    if build.get(key):
                        path = Path(build[key])
                        if ".." in path.parts or not any(path.is_relative_to(Path(root)) for root in external["external_build_paths"]):
                            raise ValueError("External backup inventory does not cover every recorded build path")
        try:
            _idle(snapshot, source)
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError("Malformed source history; reconcile before importing") from exc
        identity = _digest({"source_root": str(source), "database": _semantic_snapshot(snapshot), "evidence": evidence})
        with store.lock:
            previous = store.get("migrations", IMPORT_ID)
            existing = _snapshot(store.db)
        if previous:
            if previous["source_identity"] != identity or previous["external_inventory"] != external or previous["backup_dir"] != str(backup):
                raise ValueError("Import was already completed with a different source or request")
            _verify_backup(backup, previous["backup_manifest"], previous["backup_manifest_sha256"])
            if _digest(_semantic_snapshot(existing)) != previous["target_database_identity"] or _tree(target / "runs") != evidence:
                raise ValueError("Imported target changed; replay cannot overwrite new data")
            return previous
        if any(row[0] != "settings" for row in existing["records"]) or any(existing[name] for name in ("leases", "events", "commands")) or any((target / "runs").iterdir()):
            raise ValueError("Import requires an empty target workspace; existing settings alone are allowed")
        if backup.exists():
            raise ValueError("Backup destination is occupied or incomplete; choose a fresh directory")
        backup.mkdir(parents=True)
        with closing(sqlite3.connect(backup / "ashad.sqlite3")) as copied:
            db.backup(copied)
            copied.execute("PRAGMA journal_mode=DELETE")
            if copied.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or _snapshot(copied) != snapshot:
                raise ValueError("Source changed during database backup")
        _copy_runs(source / "runs", backup / "runs", evidence)
        if _tree(source / "runs", hashed=False) != stamps:
            raise ValueError("Source evidence changed during copying")
        if _snapshot(db) != snapshot:
            raise ValueError("Source database changed during backup")
        manifest = {"version": 1, "source_root": str(source), "source_identity": identity,
                    "database_sha256": _file_hash(backup / "ashad.sqlite3"), "evidence": evidence,
                    "external_inventory": external, "limitations": LIMITATIONS}
        _write_json(backup / "inventory.json", manifest)
        manifest_hash = _file_hash(backup / "inventory.json")
        result = {"id": IMPORT_ID, "version": 1, "source_identity": identity,
                  "source_root": str(source), "target_root": str(target), "backup_dir": str(backup),
                  "backup_manifest": manifest, "backup_manifest_sha256": manifest_hash,
                  "external_inventory": external, "imported_at": time.time(),
                  "target_database_identity": _digest(_semantic_snapshot(snapshot)), "readiness_verified": False,
                  "reauthentication_required": True, "limitations": LIMITATIONS}
        _write_json(journal, {"source_identity": identity, "backup_dir": str(backup)})
        published = committed = False
        try:
            _copy_runs(backup / "runs", stage, evidence)
            with store.transaction() as destination:
                # Each stage file was verified against its evidence digest while copying and
                # the source locks are held, so only cheap change checks repeat here.
                # Passive status rows may refresh meanwhile; they are replaced like before.
                if (_semantic_snapshot(_snapshot(store.db)) != _semantic_snapshot(existing) or _snapshot(db) != snapshot
                        or _tree(source / "runs", hashed=False) != stamps):
                    raise ValueError("Workspace changed before import; no data was imported")
                (target / "runs").rmdir()  # Proven empty; never replace an occupied evidence directory.
                stage.rename(target / "runs")
                published = True
                destination.execute("DELETE FROM records")  # Empty workspace settings only.
                for table, columns in TABLES.items():
                    placeholders = ",".join("?" for _ in columns)
                    destination.executemany(f"INSERT INTO {table} VALUES ({placeholders})", snapshot[table])
                if _digest(_snapshot(destination)) != _digest(snapshot):
                    raise ValueError("Imported SQLite rows did not verify exactly")
                store.put("migrations", result)
            committed = True
            journal.unlink()
        except BaseException:
            if not committed:
                # Delete only this operation's verified evidence. Unexpected edits stay
                # intact, with the durable interruption marker, for operator recovery.
                owned = target / "runs" if published else stage
                if owned.exists() and _tree(owned) == evidence:
                    shutil.rmtree(owned)
                    if published:
                        owned.mkdir()
                if not (target / "runs").exists():
                    (target / "runs").mkdir()
                if not stage.exists() and not any((target / "runs").iterdir()):
                    journal.unlink()
            raise
        return result
