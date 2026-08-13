param([int]$Tail = 120)

$log = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'logs\bridge.log'
if (-not (Test-Path -LiteralPath $log)) {
    throw "Bridge log not found: $log"
}

$patterns = @(
    'logging initialized',
    'initial MT5 connection',
    'sync login ',
    'authorization',
    'initialize',
    'identity mismatch',
    'request account authorization',
    'request runtime failure',
    'request failed'
)

Get-Content -LiteralPath $log -Tail 5000 |
    Select-String -SimpleMatch -Pattern $patterns |
    Select-Object -Last $Tail |
    ForEach-Object { $_.Line }
