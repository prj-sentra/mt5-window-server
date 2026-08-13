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

$lines = @(Get-Content -LiteralPath $log -Tail 5000)
$latestStart = -1
for ($index = $lines.Count - 1; $index -ge 0; $index--) {
    if ($lines[$index] -match 'logging initialized') {
        $latestStart = $index
        break
    }
}
if ($latestStart -ge 0) {
    $lines = @($lines[$latestStart..($lines.Count - 1)])
}

$matched = @($lines |
    Select-String -SimpleMatch -Pattern $patterns |
    Select-Object -Last $Tail |
    ForEach-Object { $_.Line })

if ($matched.Count -eq 0) {
    'No authentication or connection events in the current bridge process.'
} else {
    $matched
}
