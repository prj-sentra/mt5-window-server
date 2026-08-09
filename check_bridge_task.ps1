param(
    [string]$TaskName = 'MT5 Bridge Server'
)

$ErrorActionPreference = 'Stop'

function Read-DotEnv {
    param([string]$Path)

    $values = @{}
    foreach ($rawLine in Get-Content -LiteralPath $Path) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith('#')) {
            continue
        }
        if ($line.StartsWith('export ')) {
            $line = $line.Substring(7).Trim()
        }
        $parts = $line.Split('=', 2)
        if ($parts.Count -ne 2) {
            throw "Invalid .env line: $rawLine"
        }
        $key = $parts[0].Trim()
        $value = $parts[1].Trim()
        if ($value.Length -ge 2 -and $value[0] -eq $value[$value.Length - 1] -and ($value[0] -eq '"' -or $value[0] -eq "'")) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        $values[$key] = $value
    }
    return $values
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$serverScript = Join-Path $scriptDir 'server.py'
$launcher = Join-Path $scriptDir 'run_bridge.ps1'
$envFile = Join-Path $scriptDir '.env'
$logFile = Join-Path $scriptDir 'logs\bridge.log'
$venvPython = Join-Path $scriptDir '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $serverScript)) {
    throw "Missing server script: $serverScript"
}

if (-not (Test-Path -LiteralPath $launcher)) {
    throw "Missing launcher script: $launcher"
}

if (-not (Test-Path -LiteralPath $envFile)) {
    throw "Missing .env file: $envFile"
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Missing virtualenv python: $venvPython"
}

$config = Read-DotEnv -Path $envFile
$localHost = if ($config['BRIDGE_HOST'] -eq '0.0.0.0') { '127.0.0.1' } else { $config['BRIDGE_HOST'] }
$port = [int]$config['BRIDGE_PORT']
$token = $config['BRIDGE_TOKEN']
$healthUrl = "http://${localHost}:${port}/health"
$headers = @{ Authorization = "Bearer $token" }

$task = Get-ScheduledTask -TaskName $TaskName
$taskInfo = Get-ScheduledTaskInfo -TaskName $TaskName
$listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
$processes = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -match 'server\.py' -or
    $_.CommandLine -match 'run_bridge\.ps1'
}

$health = $null
$healthError = $null
for ($attempt = 1; $attempt -le 10; $attempt++) {
    try {
        $health = Invoke-RestMethod -Uri $healthUrl -Headers $headers -Method Get -TimeoutSec 5
        break
    } catch {
        $healthError = $_.Exception.Message
        Start-Sleep -Seconds 2
        $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    }
}

[pscustomobject]@{
    TaskName = $task.TaskName
    TaskState = $task.State
    LastRunTime = $taskInfo.LastRunTime
    LastTaskResult = $taskInfo.LastTaskResult
    NextRunTime = $taskInfo.NextRunTime
    Listening = ($null -ne $listener)
    ProcessCount = @($processes).Count
    HealthOk = if ($null -ne $health) { $health.ok } else { $false }
    HealthError = $healthError
    InitialFrom = if ($null -ne $health) { $health.initial_from } else { $null }
    AccountLogin = if ($null -ne $health) { $health.account.login } else { $null }
    HealthUrl = $healthUrl
}

Write-Host '--- matching processes ---'
$processes | Select-Object ProcessId, Name, CommandLine

Write-Host '--- listener ---'
if ($null -ne $listener) {
    $listener | Select-Object LocalAddress, LocalPort, State, OwningProcess
} else {
    Write-Host "No listener on port $port"
}

if (Test-Path -LiteralPath $logFile) {
    Write-Host '--- bridge.log (tail 80) ---'
    Get-Content -LiteralPath $logFile -Tail 80
}

Write-Host '--- health response ---'
if ($null -ne $health) {
    $health | ConvertTo-Json -Depth 10
} else {
    Write-Host $healthError
}



