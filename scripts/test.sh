#!/usr/bin/env bash
set -eu
cd "$(dirname "$0")/.."
.venv/bin/python -m pytest tests -q
find ashad/static -name '*.js' -print0 | while IFS= read -r -d '' script; do node --check "$script" || exit 1; done
node --check desktop/main.cjs
node --check desktop/prepare.cjs
node --test tests/test_desktop_shell.js tests/test_renderer_core.js tests/test_sidebar.js tests/test_home.js tests/test_instance.js tests/test_timeline.js tests/test_inspector.js tests/test_setup.js tests/test_settings.js tests/test_project.js tests/test_shell.js tests/test_issues.js
