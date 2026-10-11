"""Project metadata: create, delete and membership checks. No runtime I/O."""
from __future__ import annotations

from pathlib import Path
import re
import stat
import time

from .store import uid


def _identifier(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        raise ValueError(f"{label} must be 1–100 letters, digits, underscores or hyphens")
    return value


def _name(value, label="Project name"):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 200 or not value.isprintable():
        raise ValueError(f"{label} must be 1–200 characters without control characters")
    return value.strip()


def _safe_path(value, error=ValueError, *, regular_only=True):
    """Reject links before resolving: resolve() alone hides unsafe ancestors.

    The resolved result also canonicalizes trailing-dot and 8.3 short-name aliases.
    `error` is the exception type raised; regular_only=False tolerates special files."""
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise error("Unsafe path: it must be absolute without parent traversal")
    for component in reversed([path, *path.parents]):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise error("Unsafe path: symlinks and reparse points are not allowed")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise error("Unsafe path: hardlinked files are not allowed")
        if regular_only and not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise error("Unsafe path: only regular files and directories are allowed")
    return path.resolve()


def _build_refs(run):
    """Only stable recorded IDs; never infer identity from a label or a path."""
    from .game_controls import run_configuration
    return {value for value in [run.get("build_id"), (run.get("plan") or {}).get("build_id"),
        (run.get("approved_plan") or {}).get("build_id"), (run.get("build") or {}).get("id"),
        (run_configuration(run)[1] or {}).get("build_id")] if isinstance(value, str) and value}


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

    def delete(self, project_id):
        """Remove a project and its project configurations and build registrations; the caller holds the transaction and has removed its runs."""
        self.store.require("projects", project_id)
        db = self.store.db
        db.execute("DELETE FROM commands WHERE scope='tuning' AND json_extract(result,'$.configuration_id') IN "
                   "(SELECT id FROM records WHERE kind='configurations' AND json_extract(data,'$.project_id')=?)", (project_id,))
        db.execute("DELETE FROM records WHERE kind='tuning_notes' AND json_extract(data,'$.build_id') IN "
                   "(SELECT id FROM records WHERE kind='builds' AND json_extract(data,'$.project_id')=?)", (project_id,))
        db.execute("DELETE FROM records WHERE kind IN ('configurations','builds') AND json_extract(data,'$.project_id')=?", (project_id,))
        db.execute("DELETE FROM records WHERE kind='environment_preparations' AND json_extract(data,'$.request.project_id')=?", (project_id,))
        db.execute("DELETE FROM commands WHERE scope='project' AND json_extract(result,'$.project_id')=?", (project_id,))
        db.execute("DELETE FROM records WHERE kind='projects' AND id=?", (project_id,))

    def delete_configuration(self, configuration_id):
        """Remove one project configuration and its request rows; the caller holds the transaction. Tuning notes are a per-build cache and stay."""
        self.store.require("configurations", configuration_id)
        db = self.store.db
        db.execute("DELETE FROM commands WHERE scope='tuning' AND json_extract(result,'$.configuration_id')=?", (configuration_id,))
        db.execute("DELETE FROM records WHERE kind='configurations' AND id=?", (configuration_id,))

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

    def project_for_configuration(self, configuration_id, project_id=None, *, allow_unassigned=False):
        profile = self.store.require("configurations", configuration_id)
        assigned = self.project_for_build(profile["build_id"], project_id, allow_unassigned=allow_unassigned)
        if profile.get("project_id") not in (None, assigned):
            raise ValueError("Project configuration and build belong to different projects")
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
            for profile in self.store.list("configurations"):
                if profile.get("build_id") == record["id"] and profile.get("project_id") not in (None, assigned):
                    raise ValueError("Referenced project configuration belongs to another project")
            for run in self.store.list("runs"):
                if record["id"] in _build_refs(run) and run.get("project_id") not in (None, assigned):
                    raise ValueError("Referenced run belongs to another project")
            record["project_id"] = assigned
            return self.store.put("builds", record)
