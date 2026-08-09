param(
    [string]$TaskName = 'MT5 Bridge Server',
    [string]$User = "$env:USERDOMAIN\$env:USERNAME",
    [ValidateSet('Startup', 'Logon')]
    [string]$Mode = 'Startup'
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
    -RestartCount 3 `
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
        -Description 'Starts the MT5 bridge server when the user signs in.' `
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



