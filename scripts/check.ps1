$ErrorActionPreference = 'Stop'
$workspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Set-Location -LiteralPath $workspace
& .\.venv\Scripts\python.exe -m compileall -q ashad worker integration scripts
if ($LASTEXITCODE -ne 0) { throw 'Python compileall failed.' }
Get-ChildItem -Recurse -Filter *.js ashad/static | ForEach-Object {
    & node --check $_.FullName
    if ($LASTEXITCODE -ne 0) { throw "node --check failed for $($_.FullName)" }
}
Get-ChildItem desktop -Filter *.cjs | ForEach-Object {
    & node --check $_.FullName
    if ($LASTEXITCODE -ne 0) { throw "node --check failed for $($_.FullName)" }
}
$check = @'
# Controller start check: builds the app without providers or workers and serves health and the UI shell.
import tempfile
from starlette.testclient import TestClient
from ashad.app import create_app

with tempfile.TemporaryDirectory() as data:
    with TestClient(create_app(data, start_services=False), base_url="http://127.0.0.1") as client:
        assert client.get("/api/health").json()["status"] == "ok"
        assert client.get("/").status_code == 200
'@
$check | & .\.venv\Scripts\python.exe -
if ($LASTEXITCODE -ne 0) { throw 'Controller start check failed.' }
Write-Output 'ASHAD pre-merge check passed.'
