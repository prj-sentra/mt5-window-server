Set-StrictMode -Version Latest

function Assert-BridgeArray {
    param([object]$Value, [string]$Name)
    if ($Value -isnot [System.Array]) {
        throw "Bridge v2 '$Name' must be a JSON array"
    }
}

function Assert-DecimalId {
    param([object]$Value, [string]$Name)
    if ($Value -isnot [string] -or [string]$Value -notmatch '^(0|[1-9][0-9]*)$') {
        throw "Bridge v2 '$Name' must be a non-negative decimal string"
    }
}

function Assert-FiniteNumber {
    param([object]$Value, [string]$Name)
    if ($Value -isnot [ValueType]) {
        throw "Bridge v2 '$Name' must be a finite number"
    }
    $number = [double]$Value
    if ([double]::IsNaN($number) -or [double]::IsInfinity($number)) {
        throw "Bridge v2 '$Name' must be a finite number"
    }
}

function Assert-BridgeV2Response {
    param(
        [object]$Sync,
        [string]$ExpectedServer,
        [int64]$ExpectedLogin
    )

    $requiredProperties = @('server', 'accountLogin', 'cursor', 'deals', 'orders', 'positionBalances')
    $presentProperties = @($Sync.PSObject.Properties.Name)
    foreach ($property in $requiredProperties) {
        if ($presentProperties -notcontains $property) {
            throw "Bridge v2 response is missing '$property'"
        }
    }
    if ($Sync.server -cne $ExpectedServer) {
        throw 'Bridge v2 response server does not exactly match MT5_SERVER'
    }
    if ([int64]$Sync.accountLogin -ne $ExpectedLogin) {
        throw 'Bridge v2 response accountLogin does not match MT5_LOGIN'
    }
    if ([string]::IsNullOrWhiteSpace([string]$Sync.cursor)) {
        throw 'Bridge v2 response cursor is empty'
    }

    Assert-BridgeArray -Value $Sync.deals -Name 'deals'
    Assert-BridgeArray -Value $Sync.orders -Name 'orders'
    Assert-BridgeArray -Value $Sync.positionBalances -Name 'positionBalances'

    $dealNumberFields = @('time', 'timeMsc', 'type', 'entry', 'reason', 'volume', 'price', 'commission', 'swap', 'profit', 'fee')
    foreach ($deal in $Sync.deals) {
        foreach ($field in @('ticket', 'order', 'positionId', 'magic')) {
            Assert-DecimalId -Value $deal.$field -Name "deals.$field"
        }
        foreach ($field in $dealNumberFields) {
            Assert-FiniteNumber -Value $deal.$field -Name "deals.$field"
        }
        foreach ($field in @('symbol', 'comment', 'externalId')) {
            if ($deal.$field -isnot [string]) {
                throw "Bridge v2 'deals.$field' must be a string"
            }
        }
    }

    $orderNumberFields = @('timeSetup', 'timeSetupMsc', 'timeDone', 'timeDoneMsc', 'type', 'state', 'reason', 'volumeInitial', 'volumeCurrent', 'priceOpen', 'sl', 'tp', 'priceCurrent', 'priceStopLimit')
    foreach ($order in $Sync.orders) {
        foreach ($field in @('ticket', 'positionId')) {
            Assert-DecimalId -Value $order.$field -Name "orders.$field"
        }
        foreach ($field in $orderNumberFields) {
            Assert-FiniteNumber -Value $order.$field -Name "orders.$field"
        }
        foreach ($field in @('symbol', 'comment', 'externalId')) {
            if ($order.$field -isnot [string]) {
                throw "Bridge v2 'orders.$field' must be a string"
            }
        }
    }

    $seenBalanceIds = @{}
    foreach ($balance in $Sync.positionBalances) {
        Assert-DecimalId -Value $balance.positionId -Name 'positionBalances.positionId'
        Assert-FiniteNumber -Value $balance.preEntryBalance -Name 'positionBalances.preEntryBalance'
        if ($seenBalanceIds.ContainsKey([string]$balance.positionId)) {
            throw "Bridge v2 duplicate position balance '$($balance.positionId)'"
        }
        $seenBalanceIds[[string]$balance.positionId] = $true
    }

    $requiredBalanceIds = @(
        $Sync.deals |
            Where-Object { [int64]$_.positionId -gt 0 } |
            ForEach-Object { [string]$_.positionId } |
            Sort-Object -Unique
    )
    foreach ($positionId in $requiredBalanceIds) {
        if (-not $seenBalanceIds.ContainsKey($positionId)) {
            throw "Bridge v2 response lacks pre-entry balance for position $positionId"
        }
    }
}
