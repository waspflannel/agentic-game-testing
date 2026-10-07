"""Execute the actual display cleanup guard with mocked desktop/process probes."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("display,listener,server,allowed", [(":99", 1, 1, True), (":99", 0, 1, False),
    (":99", 1, 0, False), ("remote:99", 1, 1, False), (":../99", 1, 1, False)])
def test_private_display_cleanup_never_unlinks_a_live_or_unvalidated_display(display, listener, server, allowed):
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    shell = str(git_bash) if git_bash.is_file() else shutil.which("sh")
    if not shell:
        pytest.skip("POSIX shell is unavailable")
    script = (Path(__file__).parents[1] / "worker/entrypoint.sh").read_text()
    guard = script[script.index("display_number="):script.index("export XDG_RUNTIME_DIR=")]
    probes = f"xdpyinfo() {{ return {listener}; }}; pgrep() {{ return {server}; }}; rm() {{ printf '%s\\n' \"$@\"; }};\n"
    result = subprocess.run([shell, "-c", probes + guard], env={**os.environ, "DISPLAY": display},
                            text=True, capture_output=True, timeout=10)
    assert (result.returncode == 0) is allowed, result.stderr
    if allowed:
        assert result.stdout.splitlines() == ["-f", "--", "/tmp/.X99-lock", "/tmp/.X11-unix/X99"]
    else:
        assert not result.stdout
