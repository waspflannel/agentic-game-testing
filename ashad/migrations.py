"""One-way, idempotent startup migrations for retired features; running one twice changes nothing the second time."""
import time

from . import repair_tasks


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
