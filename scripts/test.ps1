$ErrorActionPreference = 'Stop'
$workspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Set-Location -LiteralPath $workspace
& .\.venv\Scripts\python.exe -m pytest tests -q
if ($LASTEXITCODE -ne 0) { throw 'ASHAD automated checks failed.' }
foreach ($script in Get-ChildItem -Recurse -Filter *.js ashad/static) {
  & node --check $script.FullName
  if ($LASTEXITCODE -ne 0) { throw "Dashboard syntax check failed: $($script.Name)" }
}
& node --check desktop/main.cjs
if ($LASTEXITCODE -ne 0) { throw 'Desktop shell syntax check failed.' }
& node --check desktop/prepare.cjs
if ($LASTEXITCODE -ne 0) { throw 'Desktop resource preparation syntax check failed.' }
& node --test tests/test_desktop_shell.js tests/test_renderer_core.js tests/test_sidebar.js tests/test_home.js tests/test_instance.js tests/test_timeline.js tests/test_inspector.js tests/test_setup.js tests/test_settings.js tests/test_project.js tests/test_shell.js
if ($LASTEXITCODE -ne 0) { throw 'Dashboard and desktop boundary checks failed.' }
