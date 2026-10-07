[CmdletBinding()]
param([int]$Port = 8740)
$ErrorActionPreference = 'Stop'
$workspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Set-Location -LiteralPath $workspace
$env:ASHAD_DATA = Join-Path $workspace '.data\controller'
$env:ASHAD_WORKER_REGISTRATION = Join-Path $workspace '.data\workers.local.json'
$env:ASHAD_BUILD_MANIFEST = Join-Path $workspace 'builds\agronauts-decisions\manifest.json'
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) { throw 'Run .\scripts\setup.ps1 first.' }
& .\.venv\Scripts\python.exe -m ashad --port $Port
