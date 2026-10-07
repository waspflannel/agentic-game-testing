"""Project metadata and explicit, idle-only workspace migration. No runtime I/O.

Call migration while holding the controller's admission/provider/setup drain lock.
The Store lock excludes in-process writers, not a second unowned controller. The
caller must own the normal data/process lock. Never invoke migration at startup.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import time

from .store import uid

MIGRATION_ID = "projects-v1"
ACTIVE = {"queued", "starting", "running", "paused", "investigating", "stopping"}


def _identifier(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        raise ValueError(f"{label} must be 1–100 letters, digits, underscores or hyphens")
    return value


def _name(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 200 or not value.isprintable():
        raise ValueError("Project name must be 1–200 characters without control characters")
    return value.strip()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _file_hash(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _safe_path(value):
    """Reject links before resolving: resolve() alone hides unsafe ancestors."""
    path = Path(value).absolute()
    if ".." in path.parts:
        raise ValueError("Unsafe backup path: parent traversal is not allowed")
    for component in reversed([path, *path.parents]):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("Unsafe backup path: links and reparse points are not allowed")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise ValueError("Unsafe backup path: hardlinked files are not allowed")
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise ValueError("Unsafe backup path: only regular files and directories are allowed")
    return path


def _tree_files(root):
    root = _safe_path(root)
    if not root.is_dir():
        raise ValueError("Backup evidence directory is unavailable")
    def unreadable(error):
        raise error
    files = {}
    for directory, directories, names in os.walk(root, followlinks=False, onerror=unreadable):
        for name in directories:
            _safe_path(Path(directory) / name)
        for name in names:
            path = _safe_path(Path(directory) / name)
            files[path.relative_to(root).as_posix()] = path
    return files


def _file_record(path, relative):
    _safe_path(path)
    return {"path": relative, "sha256": _file_hash(path), "bytes": path.stat().st_size}


def _build_refs(run):
    """Only stable recorded IDs; never infer identity from a label or a path."""
    return {value for value in [run.get("build_id"), (run.get("plan") or {}).get("build_id"),
        (run.get("approved_plan") or {}).get("build_id"), (run.get("build") or {}).get("id"),
        (run.get("agent_profile") or {}).get("build_id")] if isinstance(value, str) and value}


class Projects:
    def __init__(self, store):
        self.store = store

    def create(self, name, request_id):
        name = _name(name)
        request_id = _identifier(request_id, "Request ID")
        with self.store.transaction():
            previous = self.store.command("project", request_id)
            if previous:
                if previous["request"] != {"name": name}:
                    raise ValueError("Project request ID already belongs to a different payload")
                return self.store.require("projects", previous["project_id"])
            now = time.time()
            project = {"id": uid("project_"), "name": name, "created_at": now, "updated_at": now}
            self.store.put("projects", project)
            self.store.command("project", request_id, {"request": {"name": name}, "project_id": project["id"]})
            return project

    def project_for_build(self, build_id, project_id=None, *, allow_unassigned=False):
        build = self.store.require("builds", build_id)
        assigned = build.get("project_id")
        if project_id is not None:
            self.store.require("projects", _identifier(project_id, "Project ID"))
            if assigned != project_id:
                raise ValueError("Build does not belong to the selected project")
        if assigned:
            self.store.require("projects", assigned)
        elif not allow_unassigned:
            raise ValueError("Build has no project; explicitly migrate or register it first")
        return assigned

    def project_for_agent(self, agent_id, project_id=None, *, allow_unassigned=False):
        profile = self.store.require("agents", agent_id)
        assigned = self.project_for_build(profile["build_id"], project_id, allow_unassigned=allow_unassigned)
        if profile.get("project_id") not in (None, assigned):
            raise ValueError("Agent and build belong to different projects")
        return assigned

    def register_build(self, record, project_id=None):
        """Persist the already validated build_record result; never read a manifest."""
        record = dict(record)
        _identifier(record.get("id"), "Build ID")
        with self.store.transaction():
            previous = self.store.get("builds", record["id"])
            supplied = record.get("project_id")
            if supplied and project_id and supplied != project_id:
                raise ValueError("Build and selected project disagree")
            assigned = project_id or supplied or (previous or {}).get("project_id")
            if not assigned:
                raise ValueError("Select a project before registering a build")
            self.store.require("projects", _identifier(assigned, "Project ID"))
            if previous:
                if previous.get("project_id") not in (None, assigned):
                    raise ValueError("A registered build cannot move to another project")
                presentation = {"name", "project_id", "created_at", "updated_at"}
                identity = lambda item: {key: value for key, value in item.items() if key not in presentation}
                if identity(previous) != identity(record):
                    raise ValueError("Pinned build identity changed; register the revision under a new build ID")
            for profile in self.store.list("agents"):
                if profile.get("build_id") == record["id"] and profile.get("project_id") not in (None, assigned):
                    raise ValueError("Referenced agent belongs to another project")
            for run in self.store.list("runs"):
                if record["id"] in _build_refs(run) and run.get("project_id") not in (None, assigned):
                    raise ValueError("Referenced run belongs to another project")
            record["project_id"] = assigned
            return self.store.put("builds", record)

    def _groups(self, groups):
        if not isinstance(groups, list):
            raise ValueError("Migration groups must be an explicit list")
        projects, mapping = {}, {}
        for group in groups:
            if not isinstance(group, dict) or set(group) != {"project_id", "name", "build_ids"}:
                raise ValueError("Each group requires project_id, name and build_ids")
            project_id, name = _identifier(group["project_id"], "Project ID"), _name(group["name"])
            if project_id in projects:
                raise ValueError("Duplicate project ID in migration groups")
            existing = self.store.get("projects", project_id)
            if existing and existing["name"] != name:
                raise ValueError("Migration cannot rename an existing project")
            if not isinstance(group["build_ids"], list) or not group["build_ids"]:
                raise ValueError("Each migration group must name at least one stable build ID")
            projects[project_id] = name
            for build_id in group["build_ids"]:
                _identifier(build_id, "Build ID")
                if build_id in mapping:
                    raise ValueError("Ambiguous or duplicate build mapping")
                mapping[build_id] = project_id
        return projects, mapping

    def _blockers(self, operations_active):
        blockers = ["Provider or desktop setup operations are active"] if operations_active else []
        if self.store.leases():
            blockers.append("Resource leases remain; reconcile their owners first")
        for run in self.store.list("runs"):
            state = str(run.get("state", "unknown")).lower()
            players = (run.get("players") or {}).values()
            unresolved = any(player.get("teardown_confirmed") is False or
                ((player.get("bound") or player.get("launched") or player.get("phase") == "teardown_unknown")
                 and player.get("teardown_confirmed") is not True) for player in players)
            if state in ACTIVE or state not in {"ended", "completed", "failed", "terminated", "cancelled"} or unresolved or run.get("human_control") or run.get("external_control"):
                blockers.append(f"Run {run['id']} is active or has unresolved ownership")
        return blockers

    def _token(self, connection=None):
        connection = self.store.db if connection is None else connection
        # Passive heartbeat/auth-status polling cannot invalidate an operator's preview.
        # Ownership, durable commands, settings-main and the ledger remain authoritative.
        snapshot = {}
        for table in ("records", "leases", "commands", "events"):
            query = f"SELECT * FROM {table}"
            if table == "records":
                query += " WHERE NOT (kind='settings' AND id IN ('coordinator_heartbeat','ai_status'))"
            snapshot[table] = [tuple(row) for row in connection.execute(query + " ORDER BY 1,2")]
        return _digest(snapshot)

    def preview(self, groups, *, operations_active=False):
        with self.store.lock:
            projects, mapping = self._groups(groups)
            blockers = self._blockers(operations_active)
            assignments = {kind: {} for kind in ("builds", "agents", "runs")}
            unassigned = {kind: [] for kind in assignments}
            existing_projects = {record["id"] for record in self.store.list("projects")} | set(projects)
            builds = {record["id"]: record for record in self.store.list("builds")}
            agents = {record["id"]: record for record in self.store.list("agents")}
            known_builds = set(builds) | {profile.get("build_id") for profile in agents.values()}
            for run in self.store.list("runs"):
                known_builds.update(_build_refs(run))
            for build_id in mapping.keys() - known_builds:
                blockers.append(f"Build {build_id} has no stable catalog or historical reference")
            for build_id, build in builds.items():
                current = build.get("project_id")
                if current and current in existing_projects:
                    if build_id in mapping and mapping[build_id] != current:
                        blockers.append(f"Build {build_id} already belongs to another project")
                    mapping.setdefault(build_id, current)

            def assign(kind, record, candidates):
                target = next(iter(candidates)) if len(candidates) == 1 and None not in candidates else None
                current = record.get("project_id")
                if current:
                    if current not in existing_projects or (target and current != target):
                        blockers.append(f"{kind}/{record['id']} has conflicting project membership")
                        return
                    target = current
                if target:
                    assignments[kind][record["id"]] = target
                else:
                    unassigned[kind].append({"id": record["id"], "reason": "Missing or ambiguous stable build/project references"})

            for build_id, build in builds.items():
                assign("builds", build, {mapping.get(build_id)})
            for profile in agents.values():
                assign("agents", profile, {mapping.get(profile.get("build_id"))})
            for run in self.store.list("runs"):
                references = _build_refs(run)
                if not references:
                    profile = agents.get(run.get("agent_id") or (run.get("plan") or {}).get("agent_id"), {})
                    if profile.get("build_id"):
                        references.add(profile["build_id"])
                assign("runs", run, {mapping.get(build_id) for build_id in references})
            normalized = {"projects": projects, "build_projects": {build_id: group["project_id"]
                for group in groups for build_id in group["build_ids"]}}
            marker = self.store.get("migrations", MIGRATION_ID)
            if marker and marker["grouping_hash"] != _digest(normalized):
                blockers.append("Workspace membership migration was already applied with a different grouping")
            source_token, grouping_hash = self._token(), _digest(normalized)
            return {"version": 1, "projects": projects, "assignments": assignments, "unassigned": unassigned,
                    "blockers": blockers, "source_token": source_token, "grouping_hash": grouping_hash,
                    "preview_token": _digest({"source": source_token, "grouping": grouping_hash}),
                    "already_applied": bool(marker),
                    "backup_scope": "SQLite metadata and run evidence only; Codex home, registrations, external builds, keyring and Docker volumes are not copied. This is not a complete workspace backup."}

    def _verify_database(self, database, source_token):
        _safe_path(database)
        if any(Path(str(database) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            raise ValueError("Backup verification failed: SQLite snapshot has journal sidecars")
        with database.open("rb") as source:
            header = source.read(20)
        # immutable readers report DELETE even for WAL headers; inspect the on-disk versions.
        if header[:16] != b"SQLite format 3\x00" or header[18:20] != b"\x01\x01":
            raise ValueError("Backup verification failed: SQLite snapshot has a non-standalone header")
        try:
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)) as saved:
                if saved.execute("PRAGMA journal_mode").fetchone()[0] != "delete" or saved.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("Backup verification failed: SQLite snapshot is not standalone and healthy")
                if self._token(saved) != source_token:
                    raise ValueError("Backup verification failed: SQLite snapshot belongs to different source data")
        except sqlite3.DatabaseError as error:
            raise ValueError("Backup verification failed: invalid SQLite snapshot") from error

    def _backup(self, destination, preview, codex_home, registration_paths):
        destination = _safe_path(destination)
        if destination.is_relative_to(self.store.root) or self.store.root.is_relative_to(destination):
            raise ValueError("Backup must be outside the source data directory")
        for suffix in ("", "-wal", "-shm"):
            _safe_path(self.store.root / ("ashad.sqlite3" + suffix))
        evidence = {"runs/" + relative: source for relative, source in _tree_files(self.store.root / "runs").items()}
        expected = {relative: _file_record(source, relative) for relative, source in evidence.items()}
        manifest, database = destination / "inventory.json", destination / "ashad.sqlite3"
        if destination.exists():
            actual = _tree_files(destination)
            if "inventory.json" not in actual:
                raise ValueError("Backup is incomplete or destination is occupied; choose a new backup directory")
            try:
                inventory = json.loads(manifest.read_text(encoding="utf-8"))
                if not isinstance(inventory, dict) or inventory.get("version") != 1 or inventory.get("source_root") != str(self.store.root) or inventory.get("preview_token") != preview["preview_token"] or inventory.get("grouping_hash") != preview["grouping_hash"]:
                    raise ValueError("Backup verification failed: different source root or preview")
                entries = inventory.get("copied_files")
                if not isinstance(entries, list) or any(not isinstance(item, dict) or not isinstance(item.get("path"), str)
                    or type(item.get("bytes")) is not int or item["bytes"] < 0 for item in entries):
                    raise ValueError("Backup verification failed: invalid file inventory")
                recorded = {item["path"]: item for item in entries}
                required = set(expected) | {"ashad.sqlite3"}
                if len(recorded) != len(entries) or set(recorded) != required or set(actual) != required | {"inventory.json"}:
                    raise ValueError("Backup verification failed: incomplete or unexpected file inventory")
                for relative, record in recorded.items():
                    if record != _file_record(actual[relative], relative) or (relative in expected and record != expected[relative]):
                        raise ValueError("Existing backup verification failed: hash, size or source evidence mismatch")
                self._verify_database(database, preview["source_token"])
            except (OSError, TypeError, KeyError, json.JSONDecodeError) as error:
                raise ValueError("Backup verification failed: unreadable or invalid inventory") from error
            return destination
        destination.mkdir(parents=True)
        _safe_path(destination)
        with closing(sqlite3.connect(database, isolation_level=None)) as saved:
            self.store.db.backup(saved)
            saved.execute("PRAGMA journal_mode=DELETE")
        self._verify_database(database, preview["source_token"])
        copied = [_file_record(database, database.name)]
        for relative, source in evidence.items():
            target = _safe_path(destination / relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            _safe_path(target)
            shutil.copy2(_safe_path(source), target)
            if expected[relative] != _file_record(target, relative) or expected[relative] != _file_record(source, relative):
                raise ValueError("Run evidence changed during backup; leave the source idle and retry")
            copied.append(expected[relative])
        if set(_tree_files(self.store.root / "runs")) != {relative[5:] for relative in evidence}:
            raise ValueError("Run evidence changed during backup; review the evidence inventory")
        path_info = lambda value: {"path": str(Path(value).resolve()), "exists": Path(value).exists(), "copied": False}
        inventory = {"version": 1, "source_root": str(self.store.root), "preview_token": preview["preview_token"],
            "grouping_hash": preview["grouping_hash"], "copied_files": copied,
            "codex_home": path_info(codex_home) if codex_home is not None else None,
            "registration_paths": [path_info(value) for value in registration_paths],
            "build_paths": [{"id": build["id"], **{key: build[key] for key in ("manifest_path", "source_root", "catalog_path", "brief_path") if key in build}} for build in self.store.list("builds")],
            "worker_ids": [record["id"] for record in self.store.list("workers")],
            "account_ids": [record["id"] for record in self.store.list("accounts")],
            "limitations": ["Codex home, registrations, external builds, keyring and Docker volumes are not copied.",
                            "Reauthentication and separately authorized external backups may be required."]}
        _safe_path(manifest)
        with manifest.open("x", encoding="utf-8") as saved:
            saved.write(json.dumps(inventory, indent=2, ensure_ascii=False) + "\n")
        return destination

    def apply(self, groups, *, preview_token, backup_dir, confirmed=False, operations_active=False,
              codex_home=None, registration_paths=()):
        if confirmed is not True:
            raise ValueError("Explicit user confirmation is required before membership migration")
        with self.store.lock:
            preview = self.preview(groups, operations_active=operations_active)
            previous = self.store.get("migrations", MIGRATION_ID)
            if previous and previous["grouping_hash"] == preview["grouping_hash"]:
                return previous
            if preview["blockers"]:
                raise ValueError("Migration blocked: " + "; ".join(preview["blockers"]))
            if preview_token != preview["preview_token"]:
                raise ValueError("Migration preview is stale; review a fresh preview")
            backup = self._backup(backup_dir, preview, codex_home, registration_paths)
            with self.store.transaction() as db:
                if preview["source_token"] != self._token():
                    raise ValueError("Workspace changed during backup; review a fresh preview")
                now = time.time()
                for project_id, name in preview["projects"].items():
                    if not self.store.get("projects", project_id):
                        self.store.put("projects", {"id": project_id, "name": name, "created_at": now, "updated_at": now})
                for kind, records in preview["assignments"].items():
                    for record_id, project_id in records.items():
                        record = self.store.require(kind, record_id)
                        if record.get("project_id") == project_id:
                            continue
                        record["project_id"] = project_id
                        # Do not run legacy snapshots through redaction or any schema conversion.
                        db.execute("UPDATE records SET data=?,updated=? WHERE kind=? AND id=?",
                                   (json.dumps(record, ensure_ascii=False), now, kind, record_id))
                result = {"id": MIGRATION_ID, "version": 1, "grouping_hash": preview["grouping_hash"],
                          "applied_at": now, "backup_dir": str(backup), "assignments": preview["assignments"],
                          "unassigned": preview["unassigned"]}
                self.store.put("migrations", result)
            return result
