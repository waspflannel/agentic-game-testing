[CmdletBinding()]
param([switch]$Workers, [int]$PlayerCount = 4)
$ErrorActionPreference = 'Stop'
$workspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Set-Location -LiteralPath $workspace
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    & python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 or newer is required.' }
}
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Application dependency installation failed.' }
if ($Workers) {
    & (Join-Path $PSScriptRoot 'provision-workers.ps1') -Count $PlayerCount
}
Write-Output 'Setup complete. Launch with .\scripts\start.ps1 and open http://127.0.0.1:8740.'
