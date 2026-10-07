$ErrorActionPreference = 'Stop'
$workspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Set-Location -LiteralPath $workspace
& .\.venv\Scripts\python.exe -m pytest tests -q
if ($LASTEXITCODE -ne 0) { throw 'ASHAD automated checks failed.' }
& node --check ashad/static/app.js
if ($LASTEXITCODE -ne 0) { throw 'Dashboard syntax check failed.' }
& node --check desktop/main.cjs
if ($LASTEXITCODE -ne 0) { throw 'Desktop shell syntax check failed.' }
& node --check ashad/static/workspace.js
if ($LASTEXITCODE -ne 0) { throw 'Workspace syntax check failed.' }
& node --check desktop/prepare.cjs
if ($LASTEXITCODE -ne 0) { throw 'Desktop resource preparation syntax check failed.' }
& node --check ashad/static/instance-workspace.js
if ($LASTEXITCODE -ne 0) { throw 'Instance workspace syntax check failed.' }
& node --test tests/test_dashboard_state.js tests/test_desktop_dashboard.js tests/test_desktop_shell.js tests/test_workspace_ui.js tests/test_workspace_integration.js tests/test_instance_workspace.js tests/test_instance_integration.js
if ($LASTEXITCODE -ne 0) { throw 'Dashboard and desktop boundary checks failed.' }
