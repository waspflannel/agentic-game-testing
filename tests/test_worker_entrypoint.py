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


def test_root_stage_copies_secrets_to_private_tmpfs_before_dropping_privileges():
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    shell = str(git_bash) if git_bash.is_file() else shutil.which("sh")
    if not shell:
        pytest.skip("POSIX shell is unavailable")
    script = (Path(__file__).parents[1] / "worker/entrypoint.sh").read_text()
    root_block = script[script.index('if [ "$(id -u)" = "0" ]; then'):script.index("\nfi\n") + 4]
    root_block = root_block.replace("exec gosu", "gosu")
    stubs = ("id() { echo 0; }; mkdir() { :; }; chown() { :; }; install() { printf 'install %s\\n' \"$*\"; }; "
             "gosu() { env | grep '^ASHAD_.*_FILE=' | sort; }\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("ASHAD_")}
    result = subprocess.run([shell, "-c", stubs + root_block], env=env, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "install -d -o player -g player -m 0700 /dev/shm/ashad-secrets",
        "install -o player -g player -m 0400 /run/secrets/worker_token /dev/shm/ashad-secrets/worker_token",
        "install -o player -g player -m 0400 /run/secrets/vnc_password /dev/shm/ashad-secrets/vnc_password",
        "ASHAD_VNC_PASSWORD_FILE=/dev/shm/ashad-secrets/vnc_password",
        "ASHAD_WORKER_TOKEN_FILE=/dev/shm/ashad-secrets/worker_token",
    ]
