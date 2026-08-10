. "$PSScriptRoot\validate_bridge_response.ps1"
Set-StrictMode -Version Latest

function New-ValidResponse {
    [pscustomobject]@{
        contractVersion = 4
        server = 'Broker-Server'
        accountLogin = 12345
        cursor = 'opaque'
        historyRange = [pscustomobject]@{ fromMsc=0; toMsc=1760000000000 }
        account = [pscustomobject]@{ currency='USD'; currentBalance='10000.12'; currencyDigits=2 }
        deals = @([pscustomobject]@{ ticket='9001'; order='8001'; positionId='5001'; timeMsc=1760000000000; entry=0; profit=0.0; commission=-1.0; swap=0.0; fee=0.0 })
        orders = @()
    }
}
function Assert-Passes { param([scriptblock]$Action,[string]$Name) try { & $Action; Write-Host "PASS: $Name" } catch { throw "Expected '$Name' to pass: $($_.Exception.Message)" } }
function Assert-Fails { param([scriptblock]$Action,[string]$Name) try { & $Action } catch { Write-Host "PASS: $Name rejected"; return }; throw "Expected '$Name' to be rejected" }
Assert-Passes { Assert-BridgeV4Response (New-ValidResponse) 'Broker-Server' 12345 } 'valid v4 response'
Assert-Fails { $r=New-ValidResponse; $r.contractVersion=3; Assert-BridgeV4Response $r 'Broker-Server' 12345 } 'v3 contract'
Assert-Fails { $r=New-ValidResponse; $r.server='Other'; Assert-BridgeV4Response $r 'Broker-Server' 12345 } 'identity mismatch'
Assert-Fails { $r=New-ValidResponse; $r.historyRange.fromMsc=1760000000001; Assert-BridgeV4Response $r 'Broker-Server' 12345 } 'reversed history range'
Assert-Fails { $r=New-ValidResponse; $r.account.currentBalance='1e4'; Assert-BridgeV4Response $r 'Broker-Server' 12345 } 'noncanonical balance'
Assert-Fails { $r=New-ValidResponse; $r.deals[0].ticket=9001; Assert-BridgeV4Response $r 'Broker-Server' 12345 } 'numeric deal identifier'
Write-Host 'All bridge response validation tests passed.'
