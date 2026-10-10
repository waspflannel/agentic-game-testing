#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv || { echo 'Python 3.11 or newer is required.' >&2; exit 1; }
fi
.venv/bin/python -m pip install -r requirements.txt
echo 'Development setup complete. Run ./scripts/test.sh, then build the desktop app with npm --prefix desktop run build:linux.'
