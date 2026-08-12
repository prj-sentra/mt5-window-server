Set-StrictMode -Version Latest

function Assert-BridgeArray { param([object]$Value, [string]$Name) if ($Value -isnot [System.Array]) { throw "Bridge v5 '$Name' must be a JSON array" } }
function Assert-DecimalId { param([object]$Value, [string]$Name) if ($Value -isnot [string] -or [string]$Value -notmatch '^(0|[1-9][0-9]*)$') { throw "Bridge v5 '$Name' must be a non-negative decimal string" } }
function Assert-CanonicalDecimal { param([object]$Value, [string]$Name) if ($Value -isnot [string] -or [string]$Value -notmatch '^-?(0|[1-9][0-9]*)(\.[0-9]{1,30})?$') { throw "Bridge v5 '$Name' must be a canonical decimal string" } }
function Assert-FiniteNumber { param([object]$Value, [string]$Name) if ($Value -isnot [ValueType] -or [double]::IsNaN([double]$Value) -or [double]::IsInfinity([double]$Value)) { throw "Bridge v5 '$Name' must be a finite number" } }
function Assert-RequiredProperties { param([object]$Value, [string[]]$Names, [string]$Name) foreach ($property in $Names) { if (@($Value.PSObject.Properties.Name) -notcontains $property) { throw "Bridge v5 '$Name' is missing '$property'" } } }

function Assert-BridgeV5Response {
    param([object]$Sync, [string]$ExpectedServer, [int64]$ExpectedLogin, [int64]$ExpectedSnapshotToMsc)
    Assert-RequiredProperties $Sync @('contractVersion','server','accountLogin','mode','snapshotToMsc','account','page','deals','orders') 'response'
    if ($Sync.contractVersion -ne 5) { throw 'Bridge response must use contractVersion 5' }
    if ($Sync.server -cne $ExpectedServer -or [int64]$Sync.accountLogin -ne $ExpectedLogin) { throw 'Bridge v5 response identity mismatch' }
    if ($Sync.mode -cne 'bootstrap') { throw 'Bridge v5 response mode must be bootstrap' }
    Assert-FiniteNumber $Sync.snapshotToMsc 'snapshotToMsc'
    if ([int64]$Sync.snapshotToMsc -ne $ExpectedSnapshotToMsc -or [int64]$Sync.snapshotToMsc -lt 0) { throw 'Bridge v5 response snapshotToMsc mismatch' }
    Assert-RequiredProperties $Sync.account @('currency','currentBalance','currencyDigits') 'account'
    if ($Sync.account.currency -isnot [string] -or [string]::IsNullOrWhiteSpace($Sync.account.currency)) { throw 'Bridge v5 account currency is invalid' }
    Assert-CanonicalDecimal $Sync.account.currentBalance 'account.currentBalance'
    Assert-FiniteNumber $Sync.account.currencyDigits 'account.currencyDigits'
    $currencyDigits = [double]$Sync.account.currencyDigits
    if ($currencyDigits -ne [Math]::Truncate($currencyDigits) -or $currencyDigits -lt 0 -or $currencyDigits -gt 8) { throw 'Bridge v5 account currency digits are invalid' }
    Assert-BridgeArray $Sync.deals 'deals'; Assert-BridgeArray $Sync.orders 'orders'
    foreach ($deal in $Sync.deals) {
        Assert-RequiredProperties $deal @('ticket','order','positionId','timeMsc','entry','profit','commission','swap','fee') 'deal'
        foreach ($field in @('ticket','order','positionId')) { Assert-DecimalId $deal.$field "deals.$field" }
        foreach ($field in @('timeMsc','profit','commission','swap','fee')) { Assert-FiniteNumber $deal.$field "deals.$field" }
    }
    Assert-RequiredProperties $Sync.page @('hasMore','bytes') 'page'
    if ($Sync.page.hasMore -isnot [bool]) { throw "Bridge v5 'page.hasMore' must be a boolean" }
    Assert-FiniteNumber $Sync.page.bytes 'page.bytes'
    $bytes = [double]$Sync.page.bytes
    if ($bytes -ne [Math]::Truncate($bytes) -or $bytes -lt 1 -or $bytes -ge 1MB) { throw 'Bridge v5 page.bytes must be a positive integer below 1 MiB' }
    $hasNextCursor = @($Sync.page.PSObject.Properties.Name) -contains 'nextCursor'
    if ($Sync.page.hasMore) {
        if (-not $hasNextCursor -or $Sync.page.nextCursor -isnot [string] -or [string]::IsNullOrWhiteSpace($Sync.page.nextCursor)) { throw 'Bridge v5 non-final page must include a non-empty nextCursor' }
    } elseif ($hasNextCursor) {
        throw 'Bridge v5 final page must not include nextCursor'
    }
}
