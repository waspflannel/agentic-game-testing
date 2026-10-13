#!/usr/bin/env bash
set -eu
cd "$(dirname "$0")/.."
.venv/bin/python -m compileall -q ashad worker integration scripts
find ashad/static -name '*.js' -print0 | while IFS= read -r -d '' script; do node --check "$script" || exit 1; done
for script in desktop/*.cjs; do node --check "$script"; done
.venv/bin/python - <<'PY'
# Controller start check: builds the app without providers or workers and serves health and the UI shell.
import tempfile
from starlette.testclient import TestClient
from ashad.app import create_app

with tempfile.TemporaryDirectory() as data:
    with TestClient(create_app(data, start_services=False), base_url="http://127.0.0.1") as client:
        assert client.get("/api/health").json()["status"] == "ok"
        assert client.get("/").status_code == 200
PY
echo 'ASHAD pre-merge check passed.'
