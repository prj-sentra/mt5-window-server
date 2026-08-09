$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

. (Join-Path $PSScriptRoot 'validate_bridge_response.ps1')

function New-ValidResponse {
    return [pscustomobject]@{
        server = 'Broker-Server'
        accountLogin = [int64]12345
        cursor = 'v2.signed-cursor'
        deals = @(
            [pscustomobject]@{
                ticket = '1'; order = '2'; positionId = '3'; magic = '0'
                time = 1; timeMsc = 1000; type = 0; entry = 0; reason = 0
                volume = 1.0; price = 100.0; commission = 0.0; swap = 0.0
                profit = 10.0; fee = 0.0; symbol = 'XAUUSD'; comment = ''; externalId = ''
            }
        )
        orders = @(
            [pscustomobject]@{
                ticket = '2'; positionId = '3'; timeSetup = 1; timeSetupMsc = 1000
                timeDone = 2; timeDoneMsc = 2000; type = 0; state = 4; reason = 0
                volumeInitial = 1.0; volumeCurrent = 0.0; priceOpen = 100.0
                sl = 90.0; tp = 110.0; priceCurrent = 110.0; priceStopLimit = 0.0
                symbol = 'XAUUSD'; comment = ''; externalId = ''
            }
        )
        positionBalances = @(
            [pscustomobject]@{ positionId = '3'; preEntryBalance = 1000.0 }
        )
    }
}

function Assert-Passes {
    param([scriptblock]$Action, [string]$Name)
    try {
        & $Action
        Write-Host "PASS: $Name"
    } catch {
        throw "Expected '$Name' to pass, but it failed: $($_.Exception.Message)"
    }
}

function Assert-Fails {
    param([scriptblock]$Action, [string]$Name)
    try {
        & $Action
    } catch {
        Write-Host "PASS: $Name rejected"
        return
    }
    throw "Expected '$Name' to be rejected"
}

Assert-Passes {
    Assert-BridgeV2Response -Sync (New-ValidResponse) -ExpectedServer 'Broker-Server' -ExpectedLogin 12345
} 'valid response'

Assert-Fails {
    $response = New-ValidResponse
    $response.PSObject.Properties.Remove('cursor')
    Assert-BridgeV2Response -Sync $response -ExpectedServer 'Broker-Server' -ExpectedLogin 12345
} 'missing required property'

Assert-Fails {
    $response = New-ValidResponse
    $response.server = 'broker-server'
    Assert-BridgeV2Response -Sync $response -ExpectedServer 'Broker-Server' -ExpectedLogin 12345
} 'case-changed server identity'

Assert-Fails {
    $response = New-ValidResponse
    $response.cursor = ' '
    Assert-BridgeV2Response -Sync $response -ExpectedServer 'Broker-Server' -ExpectedLogin 12345
} 'empty cursor'

Assert-Fails {
    $response = New-ValidResponse
    $response.deals = [pscustomobject]@{}
    Assert-BridgeV2Response -Sync $response -ExpectedServer 'Broker-Server' -ExpectedLogin 12345
} 'non-array deals'

Assert-Fails {
    $response = New-ValidResponse
    $response.deals[0].ticket = 1
    Assert-BridgeV2Response -Sync $response -ExpectedServer 'Broker-Server' -ExpectedLogin 12345
} 'numeric ticket instead of decimal string'

Assert-Fails {
    $response = New-ValidResponse
    $response.positionBalances += [pscustomobject]@{ positionId = '3'; preEntryBalance = 2000.0 }
    Assert-BridgeV2Response -Sync $response -ExpectedServer 'Broker-Server' -ExpectedLogin 12345
} 'duplicate position balance'

Assert-Fails {
    $response = New-ValidResponse
    $response.positionBalances = @()
    Assert-BridgeV2Response -Sync $response -ExpectedServer 'Broker-Server' -ExpectedLogin 12345
} 'missing required position balance'

Write-Host 'All bridge response validation tests passed.'
