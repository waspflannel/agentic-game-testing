$ErrorActionPreference = 'Stop'
$workspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Set-Location -LiteralPath $workspace
& .\.venv\Scripts\python.exe -m pytest tests -q
if ($LASTEXITCODE -ne 0) { throw 'ASHAD automated checks failed.' }
& node --check ashad/static/app.js
if ($LASTEXITCODE -ne 0) { throw 'Dashboard syntax check failed.' }
& node --test tests/test_dashboard_state.js
if ($LASTEXITCODE -ne 0) { throw 'Dashboard state checks failed.' }
