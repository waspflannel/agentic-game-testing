"""A separate process enforces persisted deadlines even if the HTTP event loop stalls."""
import json
import os
from pathlib import Path
import signal
import subprocess
import time

from .models import timestamp


def revoke_desktop(state: dict) -> None:
    """Only signal viewer groups belonging to the persisted session, never a reused PID."""
    session = state.get("desktop_session")
    if not session or os.name == "nt":
        return
    for value in reversed(state.get("desktop_pids", [])):
        try:
            pid = int(value)
            if pid <= 1:
                continue
            environment = Path(f"/proc/{pid}/environ").read_bytes().split(b"\x00")
            if f"ASHAD_DESKTOP_SESSION={session}".encode() in environment:
                # x11vnc's -clear_all releases human keys on normal shutdown.
                os.killpg(pid, signal.SIGTERM)
        except (OSError, TypeError, ValueError):
            pass


def kill_run_game(pid: int, run_id: str) -> bool:
    """SIGKILL the game group only if the PID still belongs to this run, never a reused PID."""
    environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\x00")
    if f"ASHAD_RUN_ID={run_id}".encode() not in environ:
        return False
    os.killpg(pid, signal.SIGKILL)
    return True


def main():
    root = Path(os.environ.get("ASHAD_WORKER_DATA", "/data"))
    state_path = root / "worker-state.json"
    while True:
        try:
            state = json.loads(state_path.read_text())
            heartbeat_expired = time.time() - state.get("controller_last_seen", time.time()) > state.get("heartbeat_timeout_seconds", 15)
            input_expired = state.get("held_input_expires_at") and time.time() >= state["held_input_expires_at"]
            if (heartbeat_expired or input_expired) and (state.get("held_keys") or state.get("held_buttons")):
                for key in state.get("held_keys", []):
                    subprocess.run(["xdotool", "keyup", key], timeout=2, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                for button in state.get("held_buttons", []):
                    subprocess.run(["xdotool", "mouseup", str(button)], timeout=2, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadlines = [timestamp(value) for value in (state.get("investigation_deadline"), state.get("run_deadline")) if value is not None]
            deadline_expired = deadlines and time.time() >= min(deadlines)
            if state.get("human_control") and (heartbeat_expired or deadline_expired):
                revoke_desktop(state)
            if deadline_expired and state.get("game_pid"):
                pid = int(state["game_pid"])
                if kill_run_game(pid, state["binding"]["run_id"]):
                    for key in state.get("held_keys", []) + ["Control_L", "Shift_L", "Alt_L"]:
                        subprocess.run(["xdotool", "keyup", key], timeout=2, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    for button in set(state.get("held_buttons", []) + [1, 2, 3]):
                        subprocess.run(["xdotool", "mouseup", str(button)], timeout=2, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    event = {"kind": "independent_deadline_enforced", "timestamp": time.time(), "game_pid": pid, "save_status": "unverified"}
                    with (root / "runs" / state["binding"]["run_id"] / "events.jsonl").open("a") as stream:
                        stream.write(json.dumps(event) + "\n")
                        stream.flush()
                        os.fsync(stream.fileno())
        except Exception:  # One bad read must not end deadline enforcement.
            pass
        time.sleep(0.5)


if __name__ == "__main__":
    main()
