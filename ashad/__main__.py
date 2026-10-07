import argparse
from contextlib import closing
import os
from pathlib import Path
import subprocess
import sys
import uvicorn


def main():
    parser = argparse.ArgumentParser(description="ASHAD local dashboard/controller")
    parser.add_argument("--port", type=int, default=8740)
    parser.add_argument("--no-watchdog", action="store_true", help="Tests only: do not start independent deadline watchdog")
    parser.add_argument("--desktop", action="store_true", help="Owned Electron controller; bounded configuration arrives on stdin")
    args = parser.parse_args()
    if args.desktop:
        from .desktop import main as desktop_main
        try:
            desktop_main()
        except (ValueError, RuntimeError) as exc:
            raise SystemExit(str(exc)) from None
        return
    from .process_lock import acquire
    root = Path(os.environ.get("ASHAD_DATA", Path(__file__).resolve().parents[1] / ".data" / "controller"))
    try:
        controller_lock = acquire(root / "controller.lock")
    except RuntimeError as exc:
        raise SystemExit(str(exc))
    with controller_lock:
        watchdog = None
        if not args.no_watchdog:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            watchdog = subprocess.Popen(
                [sys.executable, "-m", "ashad.watchdog"],
                cwd=Path(__file__).resolve().parents[1], creationflags=flags,
            )
        try:
            uvicorn.run("ashad.app:create_app", factory=True, host="127.0.0.1", port=args.port, access_log=False)
        finally:
            # Watchdog deliberately remains if runs are active; it owns their deadlines.
            if watchdog:
                from .store import Store
                with closing(Store(root)) as store:
                    active = any(run["state"] not in {"Queued", "Ended"} for run in store.list("runs"))
                if not active:
                    watchdog.terminate()


if __name__ == "__main__":
    main()
