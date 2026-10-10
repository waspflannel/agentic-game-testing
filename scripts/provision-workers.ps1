[CmdletBinding()]
param(
    [ValidateRange(1,16)][int]$Count = 4,
    [string]$BuildPath = (Join-Path $PSScriptRoot '..\builds\agronauts-decisions'),
    [string]$Image = 'ashad-worker:local',
    [int]$ApiBasePort = 8810,
    [int]$DesktopBasePort = 6080,
    [ValidatePattern('^[a-z][a-z0-9-]*$')][string]$WorkerPrefix = 'worker',
    [string]$RegistrationFile = '',
    [string]$SecretsRoot = '',
    [string]$DockerExecutable = 'docker',
    [ValidatePattern('^[a-z][a-z0-9-]*$')][string]$NetworkName = 'ashad-private',
    [ValidatePattern('^$|^[a-f0-9]{32}$')][string]$ManagedOwner = '',
    [switch]$SkipBuild,
    [switch]$Recreate
)
$ErrorActionPreference = 'Stop'
# Windows PowerShell 5.1 turns redirected native stderr into a terminating error under Stop.
function Invoke-DockerProbe { $ErrorActionPreference = 'Continue'; & $DockerExecutable @args 2>$null }
function Get-LocalWorkerStatus($Uri, $Headers) {
    $parameters = @{Uri=$Uri; Headers=$Headers; TimeoutSec=5}
    if ((Get-Command Invoke-RestMethod).Parameters.ContainsKey('NoProxy')) { $parameters.NoProxy = $true }
    else { [Net.WebRequest]::DefaultWebProxy = $null } # Windows PowerShell 5.1: child-process only.
    Invoke-RestMethod @parameters
}
$workspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$buildRoot = [IO.Path]::GetFullPath($BuildPath)
if ($ManagedOwner -and ($Count -ne 1 -or $Recreate -or $WorkerPrefix -notlike 'workspace-*')) { throw 'Request-owned preparation requires one isolated desktop without Recreate.' }
if (-not (Test-Path -LiteralPath $buildRoot)) {
    if ($ManagedOwner) { throw 'Pinned build directory is missing.' }
    New-Item -ItemType Directory -Path $buildRoot -Force | Out-Null
}
$secretsRoot = if ($SecretsRoot) { [IO.Path]::GetFullPath($SecretsRoot) } else { Join-Path $workspace '.data\worker-secrets' }
New-Item -ItemType Directory -Path $secretsRoot -Force | Out-Null
# Only the current Windows account and SYSTEM may read generated service credentials.
$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
& (Join-Path $env:SystemRoot 'System32\icacls.exe') $secretsRoot /inheritance:r /grant:r "*${currentSid}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not restrict worker credential directory permissions.' }
if (-not $SkipBuild) {
    & $DockerExecutable build --tag $Image (Join-Path $workspace 'worker')
    if ($LASTEXITCODE -ne 0) { throw 'Worker image build failed.' }
}
$network = $NetworkName
$networkProbe = Invoke-DockerProbe network inspect --format '{{index .Labels `ashad.managed`}}' $network
if ($LASTEXITCODE -eq 0 -and $ManagedOwner -and $networkProbe -ne 'true') { throw 'Network collision; refusing an unrelated network.' }
if ($LASTEXITCODE -ne 0) {
    & $DockerExecutable network create --label 'ashad.managed=true' $network | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not create ASHAD desktop network.' }
}
function New-SecretFile($Path, $Length) {
    if (Test-Path -LiteralPath $Path) { return }
    $bytes = [byte[]]::new($Length)
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    [IO.File]::WriteAllText($Path, [Convert]::ToBase64String($bytes))
}

$records = @()
for ($i = 1; $i -le $Count; $i++) {
    $workerId = "$WorkerPrefix-$i"
    $name = "ashad-$workerId"
    $secretDirectory = Join-Path $secretsRoot $workerId
    New-Item -ItemType Directory -Path $secretDirectory -Force | Out-Null
    $tokenFile = Join-Path $secretDirectory 'worker_token'
    $vncFile = Join-Path $secretDirectory 'vnc_password'
    New-SecretFile $tokenFile 48
    New-SecretFile $vncFile 6
    $apiPort = $ApiBasePort + $i
    $desktopPort = $DesktopBasePort + $i
    if ($ManagedOwner) {
        foreach ($volume in @("ashad-$workerId-data", "ashad-$workerId-home")) {
            $volumeOwner = Invoke-DockerProbe volume inspect --format '{{index .Labels `ashad.environment-owner`}}' $volume
            if ($LASTEXITCODE -eq 0) {
                if ($volumeOwner -ne $ManagedOwner) { throw 'Persistent volume collision; refusing unrelated data.' }
            } else {
                & $DockerExecutable volume create --label 'ashad.managed=true' --label "ashad.environment-owner=$ManagedOwner" $volume | Out-Null
                if ($LASTEXITCODE -ne 0) { throw 'Could not create owned persistent profile volume.' }
            }
        }
    }
    $existing = Invoke-DockerProbe container inspect --format '{{index .Config.Labels `ashad.managed`}}' $name
    if ($LASTEXITCODE -eq 0) {
        if ($existing -ne 'true') { throw "Container $name is not ASHAD-managed; refusing to change it." }
        if ($ManagedOwner) {
            $details = @(& $DockerExecutable container inspect $name | ConvertFrom-Json)[0]
            if ($LASTEXITCODE -ne 0 -or $details.Config.Labels.'ashad.environment-owner' -ne $ManagedOwner -or $details.Config.Image -ne $Image) { throw 'Container ownership/image mismatch; refusing adoption.' }
            foreach ($mount in @(@{target='/game';source=$buildRoot}, @{target='/run/secrets';source=$secretDirectory})) {
                $actual = @($details.Mounts | Where-Object { $_.Destination -eq $mount.target })
                if ($actual.Count -ne 1 -or $actual[0].RW -or [IO.Path]::GetFullPath($actual[0].Source) -ne [IO.Path]::GetFullPath($mount.source)) { throw 'Container read-only mounts do not match this request.' }
            }
            if ($details.State.Running) {
                $headers = @{Authorization=('Bearer ' + [IO.File]::ReadAllText($tokenFile).Trim())}
                $status = Get-LocalWorkerStatus "http://127.0.0.1:$apiPort/status" $headers
                $idle = ($status.lifecycle -eq 'unbound' -and -not $status.binding) -or ($status.lifecycle -eq 'ended' -and $status.teardown_confirmed -eq $true)
                if (-not $idle -or $status.worker_id -ne $workerId -or $status.game_running -ne $false -or $status.human_control -ne $false -or $status.steam.setup_active -ne $false -or $status.held_keys.Count -ne 0 -or $status.held_buttons.Count -ne 0) { throw 'Owned worker is busy or its idle state is unknown.' }
            } else {
                $recovery = Join-Path $secretsRoot "$workerId-recovery.json"
                & $DockerExecutable cp "${name}:/data/worker-state.json" $recovery | Out-Null
                if ($LASTEXITCODE -ne 0) { throw 'Stopped worker has no verifiable persisted idle state.' }
                $state = Get-Content -LiteralPath $recovery -Raw | ConvertFrom-Json
                if ($state.worker_id -ne $workerId -or $state.contract_version -ne 'ashad-worker/1' -or $state.lifecycle -ne 'unbound' -or $state.binding -or $state.game_pid -or $state.human_control -ne $false -or $state.desktop_session -or $state.desktop_pids.Count -ne 0 -or $state.held_keys.Count -ne 0 -or $state.held_buttons.Count -ne 0) { throw 'Stopped worker is not proven unbound; manual reconciliation is required.' }
            }
        }
        if ($Recreate) {
            $headers = @{Authorization=('Bearer ' + [IO.File]::ReadAllText($tokenFile).Trim())}
            $status = Get-LocalWorkerStatus "http://127.0.0.1:$apiPort/status" $headers
            if ($status.game_running -or $status.lifecycle -notin @('unbound','ended') -or $status.steam.setup_active) {
                throw "$workerId is leased or in human setup; finish the session before replacing its container."
            }
            & $DockerExecutable stop --time 10 $name | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "Could not stop idle $workerId." }
            & $DockerExecutable container rm $name | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "Could not replace idle $workerId." }
            $existing = $null
        } else {
            # Reuse existing desktops and account profiles; container replacement requires -Recreate.
            & $DockerExecutable start $name | Out-Null
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
        if ($ManagedOwner) { $dockerArgs = @('run','--pull','never','--label',"ashad.environment-owner=$ManagedOwner") + $dockerArgs[1..($dockerArgs.Count-1)] }
        & $DockerExecutable @dockerArgs | Out-Null
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
