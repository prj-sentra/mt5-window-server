param(
    [string]$TaskName = 'MT5 Bridge Server',
    [int]$Tail = 150
)

$ErrorActionPreference = 'Continue'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$logDir = Join-Path $scriptDir 'logs'
$envFile = Join-Path $scriptDir '.env'

'=== generated ==='
Get-Date -Format o
'=== git ==='
git -C $scriptDir status --short
git -C $scriptDir rev-parse HEAD
'=== task ==='
Get-ScheduledTask -TaskName $TaskName | Format-List TaskName,State,Description,Principal,Settings
Get-ScheduledTaskInfo -TaskName $TaskName | Format-List LastRunTime,LastTaskResult,NextRunTime,NumberOfMissedRuns
'=== processes ==='
Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -match 'server\.py|terminal64\.exe|run_bridge\.ps1' } |
    Select-Object ProcessId,ParentProcessId,Name,CreationDate,CommandLine |
    Format-List
'=== listener ==='
Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalPort -eq 18812 } |
    Format-List LocalAddress,LocalPort,OwningProcess,State
'=== configuration (secrets redacted) ==='
if (Test-Path -LiteralPath $envFile) {
    Get-Content -LiteralPath $envFile | ForEach-Object {
        if ($_ -match '^\s*(BRIDGE_TOKEN|MT5_PASSWORD|.*SECRET.*|.*KEY.*)\s*=') {
            "$($Matches[1])=<redacted>"
        } else { $_ }
    }
}
'=== bridge.log ==='
if (Test-Path -LiteralPath (Join-Path $logDir 'bridge.log')) {
    Get-Content -LiteralPath (Join-Path $logDir 'bridge.log') -Tail $Tail
}
'=== launcher.log ==='
if (Test-Path -LiteralPath (Join-Path $logDir 'launcher.log')) {
    Get-Content -LiteralPath (Join-Path $logDir 'launcher.log') -Tail $Tail
}
'=== recent application errors ==='
Get-WinEvent -FilterHashtable @{ LogName='Application'; StartTime=(Get-Date).AddHours(-6); Level=1,2,3 } -MaxEvents 50 -ErrorAction SilentlyContinue |
    Where-Object { $_.ProviderName -match 'Python|Application Error|Windows Error Reporting|PowerShell' -or $_.Message -match 'server\.py|terminal64\.exe|MetaTrader' } |
    Select-Object TimeCreated,Id,LevelDisplayName,ProviderName,Message |
    Format-List
