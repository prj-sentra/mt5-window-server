param()

$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$serverScript = Join-Path $scriptDir 'server.py'
$envFile = Join-Path $scriptDir '.env'
$logDir = Join-Path $scriptDir 'logs'
$logFile = Join-Path $logDir 'launcher.log'
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
if ((Test-Path -LiteralPath $logFile) -and (Get-Item -LiteralPath $logFile).Length -gt 10MB) {
    $archive = Join-Path $logDir "launcher-$(Get-Date -Format 'yyyyMMdd-HHmmss').log"
    Move-Item -LiteralPath $logFile -Destination $archive -Force
}

Push-Location $scriptDir
try {
    while ($true) {
        Write-BridgeLog "Starting MT5 bridge with $venvPython $serverScript"
        try {
            & $venvPython $serverScript *>> $logFile
            $exitCode = if ($null -ne $LASTEXITCODE) { $LASTEXITCODE } else { 0 }
            Write-BridgeLog "MT5 bridge exited with code $exitCode; restarting in 15 seconds"
        } catch {
            Write-BridgeLog "MT5 bridge process failed: $($_ | Out-String); restarting in 15 seconds"
        }
        Start-Sleep -Seconds 15
    }
} catch {
    Write-BridgeLog "MT5 bridge failed: $($_ | Out-String)"
    throw
} finally {
    Pop-Location
}



