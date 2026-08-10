Set-StrictMode -Version Latest

function Assert-BridgeArray { param([object]$Value, [string]$Name) if ($Value -isnot [System.Array]) { throw "Bridge v4 '$Name' must be a JSON array" } }
function Assert-DecimalId { param([object]$Value, [string]$Name) if ($Value -isnot [string] -or [string]$Value -notmatch '^(0|[1-9][0-9]*)$') { throw "Bridge v4 '$Name' must be a non-negative decimal string" } }
function Assert-CanonicalDecimal { param([object]$Value, [string]$Name) if ($Value -isnot [string] -or [string]$Value -notmatch '^-?(0|[1-9][0-9]*)(\.[0-9]{1,30})?$') { throw "Bridge v4 '$Name' must be a canonical decimal string" } }
function Assert-FiniteNumber { param([object]$Value, [string]$Name) if ($Value -isnot [ValueType] -or [double]::IsNaN([double]$Value) -or [double]::IsInfinity([double]$Value)) { throw "Bridge v4 '$Name' must be a finite number" } }
function Assert-RequiredProperties { param([object]$Value, [string[]]$Names, [string]$Name) foreach ($property in $Names) { if (@($Value.PSObject.Properties.Name) -notcontains $property) { throw "Bridge v4 '$Name' is missing '$property'" } } }

function Assert-BridgeV4Response {
    param([object]$Sync, [string]$ExpectedServer, [int64]$ExpectedLogin)
    Assert-RequiredProperties $Sync @('contractVersion','server','accountLogin','cursor','historyRange','account','deals','orders') 'response'
    if ($Sync.contractVersion -ne 4) { throw 'Bridge response must use contractVersion 4' }
    if ($Sync.server -cne $ExpectedServer -or [int64]$Sync.accountLogin -ne $ExpectedLogin) { throw 'Bridge v4 response identity mismatch' }
    if ([string]::IsNullOrWhiteSpace([string]$Sync.cursor)) { throw 'Bridge v4 response cursor is empty' }
    Assert-RequiredProperties $Sync.historyRange @('fromMsc','toMsc') 'historyRange'
    Assert-FiniteNumber $Sync.historyRange.fromMsc 'historyRange.fromMsc'
    Assert-FiniteNumber $Sync.historyRange.toMsc 'historyRange.toMsc'
    if ([int64]$Sync.historyRange.fromMsc -lt 0 -or [int64]$Sync.historyRange.toMsc -lt [int64]$Sync.historyRange.fromMsc) { throw 'Bridge v4 history range is invalid' }
    Assert-RequiredProperties $Sync.account @('currency','currentBalance','currencyDigits') 'account'
    if ($Sync.account.currency -isnot [string] -or [string]::IsNullOrWhiteSpace($Sync.account.currency)) { throw 'Bridge v4 account currency is invalid' }
    Assert-CanonicalDecimal $Sync.account.currentBalance 'account.currentBalance'
    if ($Sync.account.currencyDigits -isnot [int] -or $Sync.account.currencyDigits -lt 0 -or $Sync.account.currencyDigits -gt 8) { throw 'Bridge v4 account currency digits are invalid' }
    Assert-BridgeArray $Sync.deals 'deals'; Assert-BridgeArray $Sync.orders 'orders'
    foreach ($deal in $Sync.deals) {
        Assert-RequiredProperties $deal @('ticket','order','positionId','timeMsc','entry','profit','commission','swap','fee') 'deal'
        foreach ($field in @('ticket','order','positionId')) { Assert-DecimalId $deal.$field "deals.$field" }
        foreach ($field in @('timeMsc','profit','commission','swap','fee')) { Assert-FiniteNumber $deal.$field "deals.$field" }
    }
}
