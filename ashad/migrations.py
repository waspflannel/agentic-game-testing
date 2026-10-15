"""One-way, idempotent startup migrations; running one twice changes nothing the second time."""
import logging
import time
from datetime import datetime, timezone

from . import repair_tasks

LOG = logging.getLogger(__name__)


def retire_fixer(store):
    """The fixer is gone: end runs it left in Fixing, and drop its loops, budget, command ids and setting."""
    for run in store.list("runs"):
        if run.get("state") != "Fixing":
            continue
        with store.transaction():
            run.update(state="Ended", ending_reason="fix_loop_removed")
            if not run.get("ended_at"):
                run["ended_at"] = time.time()
            store.put("runs", run)
            store.event(run["id"], "run_ended", {"reason": "fix_loop_removed", "from": "Fixing"})
    with store.transaction() as db:
        db.execute("DELETE FROM records WHERE kind IN ('loops', 'fixer_usage')")
        db.execute("DELETE FROM commands WHERE scope = 'loop'")
        settings = store.get("settings", "main")
        if settings and "fixer_daily_minutes" in settings:
            del settings["fixer_daily_minutes"]
            store.put("settings", settings)


def retire_saves(store):
    """Save/load is gone: drop save rows and manual-save command ids, and strip save links from issue records.
    Run dirs (and their saves/ archives), runs, scenarios and ledger events are kept as inert history."""
    with store.transaction() as db:
        db.execute("DELETE FROM records WHERE kind='saves'")
        db.execute("DELETE FROM commands WHERE scope LIKE 'save:%'")
        for issue in store.list("issues"):
            dropped = False
            for example in issue.get("examples") or []:
                if "saves" in example:
                    del example["saves"]
                    dropped = True
            for seen in (issue.get("first_seen"), issue.get("last_seen")):
                if isinstance(seen, dict) and "save_before" in seen:
                    del seen["save_before"]
                    dropped = True
            if dropped:
                issue["revision"] = issue.get("revision", 0) + 1
                store.put("issues", issue)
    repair_tasks.publish_issues(store)


def _legacy_project(store, issue):
    """The project of the first run, then any run, then any build that still exists and has one."""
    candidates = [("runs", (issue.get("first_seen") or {}).get("run_id")),
                  *(("runs", run_id) for run_id in issue.get("runs", [])),
                  *(("builds", build_id) for build_id in issue.get("builds", []))]
    for kind, key in candidates:
        project_id = key and (store.get(kind, key) or {}).get("project_id")
        if project_id:
            return project_id
    return None


def migrate_issue_projects(store):
    """Issues belong to projects: give ashad.issue/1 records a project and open/closed status, republish, and drop their flat files."""
    with store.transaction():
        for issue in store.list("issues"):
            if issue.get("schema") == repair_tasks.ISSUE_SCHEMA:
                continue
            issue["project_id"] = _legacy_project(store, issue)
            status = "closed" if issue.get("status") == "fixed" else "open"
            if status != issue.get("status"):
                history = issue.setdefault("status_history", [])
                history.append({"at": datetime.now(timezone.utc).isoformat(), "from": issue.get("status"),
                                "to": status, "by": "migration"})
                del history[:-repair_tasks.MAX_STATUS_HISTORY]
                issue["status"] = status
            issue["schema"] = repair_tasks.ISSUE_SCHEMA
            issue["revision"] = issue.get("revision", 0) + 1
            store.put("issues", issue)
    repair_tasks.publish_issues(store)
    names = {repair_tasks.issue_filename(r) for r in store.list("issues")}
    for path in repair_tasks.issue_directory(store).glob("*.json"):
        if path.name in names:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                LOG.warning("Could not remove legacy issue file %s", path)


def drop_run_observations(store):
    """Observations live in their own records now; strip the copy every run save used to rewrite."""
    with store.transaction():
        for run in store.list("runs"):
            stale = [ps for ps in run.get("players", {}).values() if "structured_context" in ps]
            for ps in stale:
                del ps["structured_context"]
            if stale:
                store.put("runs", run)


def drop_recipes(store):
    """Per-configuration recipes were replaced by the project skill library; their configuration-bound steps are not migrated."""
    with store.transaction() as db:
        db.execute("DELETE FROM records WHERE kind='recipes'")


def drop_macro_skills(store):
    """Farming macros became general repeat steps; skills that name a removed macro are dropped and rebuild on the next win."""
    with store.transaction() as db:
        db.execute("DELETE FROM records WHERE kind='skills' AND EXISTS (SELECT 1 FROM json_each(data, '$.steps') "
                   "WHERE json_extract(value, '$.control') IN ('till_area', 'plant_all', 'water_all', 'harvest_all'))")
