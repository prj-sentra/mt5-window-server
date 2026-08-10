Set-StrictMode -Version Latest

function Assert-BridgeArray { param([object]$Value, [string]$Name) if ($Value -isnot [System.Array]) { throw "Bridge v3 '$Name' must be a JSON array" } }
function Assert-DecimalId { param([object]$Value, [string]$Name) if ($Value -isnot [string] -or [string]$Value -notmatch '^(0|[1-9][0-9]*)$') { throw "Bridge v3 '$Name' must be a non-negative decimal string" } }
function Assert-PositiveDecimalId { param([object]$Value, [string]$Name) Assert-DecimalId $Value $Name; if ([string]$Value -eq '0') { throw "Bridge v3 '$Name' must be positive" } }
function Assert-CanonicalDecimal { param([object]$Value, [string]$Name) if ($Value -isnot [string] -or [string]$Value -notmatch '^-?(0|[1-9][0-9]*)(\.[0-9]{1,30})?$') { throw "Bridge v3 '$Name' must be a canonical decimal string" } }
function Assert-FiniteNumber { param([object]$Value, [string]$Name) if ($Value -isnot [ValueType] -or [double]::IsNaN([double]$Value) -or [double]::IsInfinity([double]$Value)) { throw "Bridge v3 '$Name' must be a finite number" } }
function Assert-RequiredProperties { param([object]$Value, [string[]]$Names, [string]$Name) foreach ($property in $Names) { if (@($Value.PSObject.Properties.Name) -notcontains $property) { throw "Bridge v3 '$Name' is missing '$property'" } } }

function Assert-BridgeV3Response {
    param([object]$Sync, [string]$ExpectedServer, [int64]$ExpectedLogin)
    Assert-RequiredProperties $Sync @('contractVersion','server','accountLogin','cursor','ledgerSemanticsVersion','deals','orders','positionEntryBalances','unsupportedPositionEntryBalances') 'response'
    if ($Sync.contractVersion -ne 3 -or $Sync.ledgerSemanticsVersion -ne 1) { throw 'Bridge response must use contractVersion 3 and ledgerSemanticsVersion 1' }
    if ($Sync.server -cne $ExpectedServer -or [int64]$Sync.accountLogin -ne $ExpectedLogin) { throw 'Bridge v3 response identity mismatch' }
    if ([string]::IsNullOrWhiteSpace([string]$Sync.cursor)) { throw 'Bridge v3 response cursor is empty' }
    Assert-BridgeArray $Sync.deals 'deals'; Assert-BridgeArray $Sync.orders 'orders'; Assert-BridgeArray $Sync.positionEntryBalances 'positionEntryBalances'; Assert-BridgeArray $Sync.unsupportedPositionEntryBalances 'unsupportedPositionEntryBalances'
    $dealsByTicket = @{}; $positions = @{}
    foreach ($deal in $Sync.deals) {
        Assert-RequiredProperties $deal @('ticket','order','positionId','timeMsc','entry') 'deal'
        foreach ($field in @('ticket','order','positionId')) { Assert-DecimalId $deal.$field "deals.$field" }
        Assert-FiniteNumber $deal.timeMsc 'deals.timeMsc'; $dealsByTicket[[string]$deal.ticket] = $deal
        if ([string]$deal.positionId -ne '0') { $positions[[string]$deal.positionId] = $true }
    }
    $seenPositions = @{}; $seenAnchors = @{}
    foreach ($row in $Sync.positionEntryBalances) {
        Assert-RequiredProperties $row @('positionId','entryDealTicket','entryOrderTicket','entryTimeMsc','preEntryBalance','ledgerSemanticsVersion') 'positionEntryBalances'
        foreach ($field in @('positionId','entryDealTicket','entryOrderTicket')) { Assert-PositiveDecimalId $row.$field "positionEntryBalances.$field" }
        Assert-FiniteNumber $row.entryTimeMsc 'positionEntryBalances.entryTimeMsc'; Assert-CanonicalDecimal $row.preEntryBalance 'positionEntryBalances.preEntryBalance'
        if ($row.ledgerSemanticsVersion -ne 1) { throw 'Bridge v3 proven row has an unsupported semantic version' }
        if ($seenPositions.ContainsKey([string]$row.positionId) -or $seenAnchors.ContainsKey([string]$row.entryDealTicket)) { throw 'Bridge v3 assertion identity is duplicated' }
        $seenPositions[[string]$row.positionId] = $true; $seenAnchors[[string]$row.entryDealTicket] = $true
        if ($dealsByTicket.ContainsKey([string]$row.entryDealTicket)) { $deal = $dealsByTicket[[string]$row.entryDealTicket]; if ($deal.positionId -cne $row.positionId -or $deal.order -cne $row.entryOrderTicket -or [int64]$deal.timeMsc -ne [int64]$row.entryTimeMsc) { throw 'Bridge v3 proven anchor does not equal its exact deal ticket' } }
    }
    foreach ($row in $Sync.unsupportedPositionEntryBalances) {
        Assert-RequiredProperties $row @('kind','positionId','reason','ledgerSemanticsVersion') 'unsupportedPositionEntryBalances'
        Assert-PositiveDecimalId $row.positionId 'unsupportedPositionEntryBalances.positionId'
        if ($row.ledgerSemanticsVersion -ne 1 -or $seenPositions.ContainsKey([string]$row.positionId)) { throw 'Bridge v3 unsupported row is duplicated or has an unsupported semantic version' }
        if ($row.kind -ceq 'ANCHORED') {
            Assert-RequiredProperties $row @('entryDealTicket','entryOrderTicket','entryTimeMsc') 'anchored unsupported row'
            foreach ($field in @('entryDealTicket','entryOrderTicket')) { Assert-PositiveDecimalId $row.$field "unsupportedPositionEntryBalances.$field" }
            Assert-FiniteNumber $row.entryTimeMsc 'unsupportedPositionEntryBalances.entryTimeMsc'
            if ($row.reason -cnotin @('UNSUPPORTED_INOUT','UNSUPPORTED_ACCOUNT_NOT_APPROVED','UNSUPPORTED_CHECKPOINT')) { throw 'Bridge v3 anchored unsupported row has an invalid reason' }
            if (@($row.PSObject.Properties.Name) -contains 'preEntryBalance' -or $seenAnchors.ContainsKey([string]$row.entryDealTicket)) { throw 'Bridge v3 anchored unsupported row has a balance or duplicate anchor' }
            $seenAnchors[[string]$row.entryDealTicket] = $true
            if ($dealsByTicket.ContainsKey([string]$row.entryDealTicket)) { $deal = $dealsByTicket[[string]$row.entryDealTicket]; if ($deal.positionId -cne $row.positionId -or $deal.order -cne $row.entryOrderTicket -or [int64]$deal.timeMsc -ne [int64]$row.entryTimeMsc) { throw 'Bridge v3 unsupported anchor does not equal its exact deal ticket' } }
        } elseif ($row.kind -ceq 'UNANCHORED') {
            if ($row.reason -cne 'OPENING_DEAL_OUTSIDE_HISTORY') { throw 'Bridge v3 unanchored row has an invalid reason' }
            foreach ($field in @('entryDealTicket','entryOrderTicket','entryTimeMsc','preEntryBalance')) { if (@($row.PSObject.Properties.Name) -contains $field) { throw "Bridge v3 unanchored row forbids '$field'" } }
        } else { throw 'Bridge v3 unsupported row has an invalid kind' }
        $seenPositions[[string]$row.positionId] = $true
    }
    foreach ($positionId in $positions.Keys) { if (-not $seenPositions.ContainsKey($positionId)) { throw "Bridge v3 response lacks an assertion for position $positionId" } }
}
