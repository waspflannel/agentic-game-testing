"""Independent OS process: enforced deadlines survive controller/model failure."""
import asyncio
import os
from pathlib import Path
import time
from .store import Store
from .credentials import Credentials
from .ai import OpenAI
from .coordinator import Coordinator


async def once(store, coordinator, now=None):
    now = now or time.time()
    for run in store.list("runs"):
        if run["state"] not in {"Starting", "Running", "Paused", "Investigating", "Stopping"}:
            continue
        deadline = run.get("investigation_deadline")
        duration = run["plan"]["limits"]["duration_seconds"]
        run_deadline = run.get("started_at", now) + duration if duration else None
        if deadline and now >= deadline:
            await coordinator.finish(run["id"], "investigation_expired", terminate=True)
        elif run_deadline and now >= run_deadline:
            await coordinator.finish(run["id"], "limit_reached", terminate=True, limitation="Independent watchdog enforced the run duration limit.")


async def main():
    root = Path(os.environ.get("ASHAD_DATA", Path(__file__).resolve().parents[1] / ".data" / "controller"))
    from .process_lock import acquire
    try:
        process_lock = acquire(root / "watchdog.lock")
    except RuntimeError:
        return
    store = Store(root)
    credentials = Credentials()
    coordinator = Coordinator(store, credentials, OpenAI(store, credentials))
    while True:
        try:
            await once(store, coordinator)
        except Exception as exc:
            print(f"ASHAD watchdog: {type(exc).__name__}", flush=True)
        await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
