#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
.venv/bin/python -m pytest tests -q
for script in $(find ashad/static -name '*.js'); do node --check "$script"; done
node --check desktop/main.cjs
node --check desktop/prepare.cjs
node --test tests/test_desktop_shell.js tests/test_renderer_core.js tests/test_sidebar.js tests/test_home.js tests/test_instance.js tests/test_timeline.js tests/test_inspector.js tests/test_setup.js tests/test_settings.js tests/test_project.js tests/test_shell.js
