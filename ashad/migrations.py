"""One-way, idempotent startup migrations for retired features; running one twice changes nothing the second time."""
import time


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
