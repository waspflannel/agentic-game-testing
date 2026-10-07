"""Real process competition: a lock collision must be actionable, not a traceback."""
from pathlib import Path
import subprocess
import sys

from ashad.process_lock import acquire


def test_competing_process_reports_ownership_and_can_retry_after_release(tmp_path):
    root = Path(__file__).resolve().parents[1]
    path = tmp_path / "controller.lock"
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); from ashad.process_lock import acquire\n"
        "try:\n acquire(sys.argv[2]).close()\n"
        "except RuntimeError as exc:\n print(str(exc)); sys.exit(17)\n"
    )
    command = [sys.executable, "-I", "-B", "-c", code, str(root), str(path)]
    with acquire(path):
        blocked = subprocess.run(command, capture_output=True, text=True, timeout=10)
        assert blocked.returncode == 17
        assert blocked.stdout.strip() == "Another ASHAD process owns controller.lock"
        assert blocked.stderr == ""
    released = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert released.returncode == 0 and released.stderr == ""
