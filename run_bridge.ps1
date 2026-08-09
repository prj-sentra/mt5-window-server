param()

$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$serverScript = Join-Path $scriptDir 'server.py'
$envFile = Join-Path $scriptDir '.env'
$logDir = Join-Path $scriptDir 'logs'
$logFile = Join-Path $logDir 'bridge.log'
$venvPython = Join-Path $scriptDir '.venv\Scripts\python.exe'

function Write-BridgeLog {
    param([string]$Message)

    $timestamp = Get-Date -Format o
    Add-Content -Path $logFile -Value "$timestamp $Message"
}

if (-not (Test-Path -LiteralPath $serverScript)) {
    throw "Missing server script: $serverScript"
}

if (-not (Test-Path -LiteralPath $envFile)) {
    throw "Missing .env file: $envFile"
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Missing virtualenv python: $venvPython"
}

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

Push-Location $scriptDir
try {
    Write-BridgeLog "Starting MT5 bridge with $venvPython $serverScript"
    & $venvPython $serverScript *>> $logFile
    $exitCode = if ($null -ne $LASTEXITCODE) { $LASTEXITCODE } else { 0 }
    Write-BridgeLog "MT5 bridge exited with code $exitCode"
    exit $exitCode
} catch {
    Write-BridgeLog "MT5 bridge failed: $($_ | Out-String)"
    throw
} finally {
    Pop-Location
}



