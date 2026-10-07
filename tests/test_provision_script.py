"""Runs the real provisioner under Windows PowerShell 5.1 against a logging Docker stub; no Docker."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
SCRIPT = Path(__file__).resolve().parents[1] / "scripts/provision-workers.ps1"
STUB = """import json, sys
with open(sys.argv[1], "a", encoding="utf-8") as log:
    log.write(json.dumps(sys.argv[2:]) + "\\n")
if len(sys.argv) > 3 and sys.argv[3] == "inspect":
    sys.stderr.write("Error: No such object\\n")
    sys.exit(1)
"""


@pytest.mark.skipif(os.name != "nt" or not POWERSHELL.is_file(), reason="Windows PowerShell 5.1 only")
def test_first_time_provisioning_survives_missing_resource_stderr(tmp_path):
    log, build = tmp_path / "docker.log", tmp_path / "build"
    build.mkdir()
    (tmp_path / "stub.py").write_text(STUB, encoding="utf-8")
    docker = tmp_path / "docker.cmd"
    docker.write_text(f'@"{sys.executable}" "{tmp_path / "stub.py"}" "{log}" %*\r\n', encoding="ascii")
    registration = tmp_path / "workers.json"
    completed = subprocess.run([str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
        "-Count", "1", "-BuildPath", str(build), "-Image", "ashad-workspace-worker:test", "-SkipBuild",
        "-WorkerPrefix", "workspace-test", "-SecretsRoot", str(tmp_path / "secrets"), "-RegistrationFile", str(registration),
        "-DockerExecutable", str(docker), "-NetworkName", "ashad-test", "-ManagedOwner", "a" * 32],
        capture_output=True, text=True, timeout=120)
    assert completed.returncode == 0, completed.stderr
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    verbs = [call[:2] for call in calls]
    assert ["network", "create"] in verbs and verbs.count(["volume", "create"]) == 2 and ["run", "--pull"] in verbs
    # Go-template label keys must survive Windows PowerShell 5.1 native argument quoting.
    assert calls[0][:4] == ["network", "inspect", "--format", "{{index .Labels `ashad.managed`}}"]
    assert len(json.loads(registration.read_text(encoding="utf-8-sig"))) == 1
