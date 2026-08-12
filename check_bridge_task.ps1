param(
    [string]$TaskName = 'MT5 Bridge Server',
    [string]$TickSymbol = ''
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

$validator = Join-Path $scriptDir 'validate_bridge_response.ps1'
if (-not (Test-Path -LiteralPath $validator)) {
    throw "Missing response validator: $validator"
}
. $validator
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
$baseUrl = "http://${localHost}:${port}"
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

$sync = $null
$syncValid = $false
$capabilities = $null
$tickValid = [string]::IsNullOrWhiteSpace($TickSymbol)
$tickError = ''
$syncError = $null
$syncDealCount = 0
$syncOrderCount = 0
$syncPageCount = 0
if ($null -ne $health) {
    try {
        $snapshotToMsc = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
        $syncRequest = @{
            contractVersion = 5
            server = $config['MT5_SERVER']
            accountLogin = [int64]$config['MT5_LOGIN']
            password = $config['MT5_PASSWORD']
            mode = 'bootstrap'
            snapshotToMsc = $snapshotToMsc
        }
        do {
            $sync = Invoke-RestMethod -Uri "http://${localHost}:${port}/sync" -Headers $headers -Method Post -ContentType 'application/json' -Body ($syncRequest | ConvertTo-Json -Compress) -TimeoutSec 30
            Assert-BridgeV5Response -Sync $sync -ExpectedServer $config['MT5_SERVER'] -ExpectedLogin ([int64]$config['MT5_LOGIN']) -ExpectedSnapshotToMsc $snapshotToMsc
            $syncDealCount += @($sync.deals).Count; $syncOrderCount += @($sync.orders).Count; $syncPageCount++
            if ($sync.page.hasMore) { $syncRequest.pageCursor = $sync.page.nextCursor }
        } while ($sync.page.hasMore)
        $syncValid = $true
    } catch {
        $errorDetailsMessage = if (
            $null -ne $_.ErrorDetails -and
            $null -ne $_.ErrorDetails.PSObject.Properties['Message']
        ) {
            [string]$_.ErrorDetails.Message
        } else {
            ''
        }
        $syncError = if (-not [string]::IsNullOrWhiteSpace($errorDetailsMessage)) {
            $errorDetailsMessage
        } else {
            $_.Exception.Message
        }
        $sync = $null
        $syncValid = $false
    }
}

try {
    $capabilities = Invoke-RestMethod -Uri "$baseUrl/capabilities" -Headers $headers -Method Get
    if ($capabilities.contractVersion -ne 5 -or $capabilities.ticks.cursorNamespace -ne 'ticks-v1') {
        throw 'Bridge capabilities do not advertise the required ticks-v1 contract'
    }
    if (-not [string]::IsNullOrWhiteSpace($TickSymbol)) {
        $snapshotToMsc = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
        $tickPayload = @{
            contractVersion = 5
            server = $config['MT5_SERVER']
            accountLogin = [int64]$config['MT5_LOGIN']
            password = $config['MT5_PASSWORD']
            symbol = $TickSymbol
            rawRange = @{ fromMsc = $snapshotToMsc - 300000; toMsc = $snapshotToMsc }
            snapshotToMsc = $snapshotToMsc
            pageSize = 1000
        }
        $tickCount = 0
        do {
            $tickPage = Invoke-RestMethod -Uri "$baseUrl/ticks" -Headers $headers -Method Post -ContentType 'application/json' -Body ($tickPayload | ConvertTo-Json -Depth 5)
            if ($tickPage.cursorNamespace -ne 'ticks-v1' -or $tickPage.symbol -ne $TickSymbol) { throw 'Invalid tick page identity' }
            $tickCount += @($tickPage.ticks).Count
            if ($tickPage.complete) { break }
            $tickPayload = @{ contractVersion = 5; pageCursor = $tickPage.nextCursor }
        } while ($true)
        $tickValid = $true
    }
} catch {
    $tickError = $_.Exception.Message
    $tickValid = $false
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
    SyncOk = $syncValid
    SyncError = $syncError
    SyncDealCount = if ($syncValid) { $syncDealCount } else { $null }
    SyncOrderCount = if ($syncValid) { $syncOrderCount } else { $null }
    SyncPageCount = if ($syncValid) { $syncPageCount } else { $null }
    SyncCurrency = if ($null -ne $sync) { $sync.account.currency } else { $null }
    SyncCurrentBalance = if ($null -ne $sync) { $sync.account.currentBalance } else { $null }
    TickCapabilityOk = ($null -ne $capabilities)
    TickProbeOk = $tickValid
    TickProbeError = $tickError
}

Write-Host '--- matching processes ---'
$processes | Select-Object ProcessId, Name, CommandLine

Write-Host '--- listener ---'
if ($null -ne $listener) {
    $listener | Select-Object LocalAddress, LocalPort, State, OwningProcess
} else {
    Write-Host "No listener on port $port"
}

Write-Host '--- health summary ---'
if ($null -ne $health) {
    [pscustomobject]@{
        Ok = $health.ok
        InitialFrom = $health.initial_from
        AccountLogin = $health.account.login
    }
} else {
    Write-Host $healthError
}

Write-Host '--- sync v5 bootstrap summary ---'
if ($syncValid) {
    [pscustomobject]@{
        Server = $sync.server
        AccountLogin = $sync.accountLogin
        SnapshotToMsc = $sync.snapshotToMsc
        PageCount = $syncPageCount
        DealCount = $syncDealCount
        OrderCount = $syncOrderCount
        Currency = $sync.account.currency
        CurrentBalance = $sync.account.currentBalance
    }
} else {
    Write-Host $syncError
}

if ($null -eq $health -or -not $syncValid) {
    exit 1
}
if (-not $tickValid) { exit 1 }



