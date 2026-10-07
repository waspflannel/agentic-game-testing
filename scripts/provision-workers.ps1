[CmdletBinding()]
param(
    [ValidateRange(1,16)][int]$Count = 4,
    [string]$BuildPath = (Join-Path $PSScriptRoot '..\builds\agronauts-decisions'),
    [string]$Image = 'ashad-worker:local',
    [int]$ApiBasePort = 8810,
    [int]$DesktopBasePort = 6080,
    [ValidatePattern('^[a-z][a-z0-9-]*$')][string]$WorkerPrefix = 'worker',
    [string]$RegistrationFile = '',
    [switch]$SkipBuild,
    [switch]$Recreate
)
$ErrorActionPreference = 'Stop'
$workspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$buildRoot = [IO.Path]::GetFullPath($BuildPath)
if (-not (Test-Path -LiteralPath $buildRoot)) { New-Item -ItemType Directory -Path $buildRoot -Force | Out-Null }
$secretsRoot = Join-Path $workspace '.data\worker-secrets'
New-Item -ItemType Directory -Path $secretsRoot -Force | Out-Null
# Only the current Windows account and SYSTEM may read generated service credentials.
$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
& icacls $secretsRoot /inheritance:r /grant:r "*${currentSid}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not restrict worker credential directory permissions.' }
if (-not $SkipBuild) {
    & docker build --tag $Image (Join-Path $workspace 'worker')
    if ($LASTEXITCODE -ne 0) { throw 'Worker image build failed.' }
}
$network = 'ashad-private'
& docker network inspect $network *> $null
if ($LASTEXITCODE -ne 0) {
    & docker network create --label 'ashad.managed=true' $network | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not create ASHAD desktop network.' }
}
$records = @()
for ($i = 1; $i -le $Count; $i++) {
    $workerId = "$WorkerPrefix-$i"
    $name = "ashad-$workerId"
    $secretDirectory = Join-Path $secretsRoot $workerId
    New-Item -ItemType Directory -Path $secretDirectory -Force | Out-Null
    $tokenFile = Join-Path $secretDirectory 'worker_token'
    $vncFile = Join-Path $secretDirectory 'vnc_password'
    if (-not (Test-Path -LiteralPath $tokenFile)) {
        $tokenBytes = [byte[]]::new(48)
        [Security.Cryptography.RandomNumberGenerator]::Fill($tokenBytes)
        [IO.File]::WriteAllText($tokenFile, [Convert]::ToBase64String($tokenBytes))
    }
    if (-not (Test-Path -LiteralPath $vncFile)) {
        $vncBytes = [byte[]]::new(6)
        [Security.Cryptography.RandomNumberGenerator]::Fill($vncBytes)
        [IO.File]::WriteAllText($vncFile, [Convert]::ToBase64String($vncBytes))
    }
    $apiPort = $ApiBasePort + $i
    $desktopPort = $DesktopBasePort + $i
    $existing = & docker container inspect --format '{{index .Config.Labels "ashad.managed"}}' $name 2>$null
    if ($LASTEXITCODE -eq 0) {
        if ($existing -ne 'true') { throw "Container $name is not ASHAD-managed; refusing to change it." }
        if ($Recreate) {
            $headers = @{Authorization=('Bearer ' + [IO.File]::ReadAllText($tokenFile).Trim())}
            $status = Invoke-RestMethod -Uri "http://127.0.0.1:$apiPort/status" -Headers $headers -TimeoutSec 5 -NoProxy
            if ($status.game_running -or $status.lifecycle -notin @('unbound','ended') -or $status.steam.setup_active) {
                throw "$workerId is leased or in human setup; finish the session before replacing its container."
            }
            & docker stop --time 10 $name | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "Could not stop idle $workerId." }
            & docker container rm $name | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "Could not replace idle $workerId." }
            $existing = $null
        } else {
            # Reuse existing desktops and account profiles; container replacement requires -Recreate.
            & docker start $name | Out-Null
        }
    } else { $existing = $null }
    if (-not $existing) {
        $dockerArgs = @('run','--detach','--name',$name,'--label','ashad.managed=true',
            '--network',$network,'--network-alias',$name,'--restart','unless-stopped','--init',
            '--shm-size','1g','--cpus','3','--memory','4g',
            '--publish',"127.0.0.1:${apiPort}:8080",'--publish',"127.0.0.1:${desktopPort}:6080",
            '--env',"ASHAD_WORKER_ID=$workerId",'--env','ASHAD_EXECUTABLE_ROOTS=/game:/opt/runtime',
            '--mount',"type=bind,source=$buildRoot,target=/game,readonly",
            '--mount',"type=bind,source=$secretDirectory,target=/run/secrets,readonly",
            '--mount',"type=volume,source=ashad-$workerId-data,target=/data",
            '--mount',"type=volume,source=ashad-$workerId-home,target=/home/player",$Image)
        & docker @dockerArgs | Out-Null
    }
    if ($LASTEXITCODE -ne 0) { throw "Could not start $workerId." }
    $records += [ordered]@{id=$workerId; name="Player desktop $i"; url="http://127.0.0.1:$apiPort";
        token_file=$tokenFile; desktop_url="http://127.0.0.1:$desktopPort/vnc.html";
        vnc_password_file=$vncFile; host_address=$name; enabled=$true; container=$name}
}
$config = if ($RegistrationFile) { [IO.Path]::GetFullPath($RegistrationFile) } else { Join-Path $workspace '.data\workers.local.json' }
if (Test-Path -LiteralPath $config) {
    $previousRecords = @(Get-Content -LiteralPath $config -Raw | ConvertFrom-Json)
    $records = @($previousRecords | Where-Object { $_.id -notin $records.id }) + $records
}
ConvertTo-Json -InputObject @($records) -Depth 4 | Set-Content -LiteralPath $config -Encoding utf8
Write-Host "Started $Count independent ASHAD desktops. Local registration file: $config"
Write-Host 'No Steam accounts are signed in automatically. Use the dashboard account setup flow.'
Write-Host 'Worker ports and human setup desktops are bound to localhost. Keep service tokens backend-only.'
