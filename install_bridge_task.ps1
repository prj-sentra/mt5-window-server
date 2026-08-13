param(
    [string]$TaskName = 'MT5 Bridge Server',
    [string]$User = "$env:USERDOMAIN\$env:USERNAME",
    [ValidateSet('Startup', 'Logon')]
    [string]$Mode = 'Logon'
)

$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$launcher = Join-Path $scriptDir 'run_bridge.ps1'
$envFile = Join-Path $scriptDir '.env'

if (-not (Test-Path -LiteralPath $launcher)) {
    throw "Missing launcher script: $launcher"
}

if (-not (Test-Path -LiteralPath $envFile)) {
    throw "Missing .env file: $envFile"
}

# Reinstalling the task must not leave an orphan bridge competing for the port.
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -like "*$serverScript*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoLogo -NoProfile -ExecutionPolicy Bypass -File `"$launcher`"" `
    -WorkingDirectory $scriptDir

if ($Mode -eq 'Startup') {
    $trigger = New-ScheduledTaskTrigger -AtStartup
} else {
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
}

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable

if ($Mode -eq 'Startup') {
    $passwordSecure = Read-Host -AsSecureString "Windows account password for scheduled task user $User"
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($passwordSecure)
    try {
        $password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Description 'Starts the MT5 bridge server when the Windows VM boots.' `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -User $User `
        -Password $password `
        -RunLevel Highest `
        -Force | Out-Null
} else {
    $principal = New-ScheduledTaskPrincipal `
        -UserId $User `
        -LogonType Interactive `
        -RunLevel Highest

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Description 'Starts and supervises the MT5 bridge in the interactive MT5 terminal session.' `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Force | Out-Null
}

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 2
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State, Author
Get-ScheduledTaskInfo -TaskName $TaskName | Select-Object LastRunTime, LastTaskResult, NextRunTime



