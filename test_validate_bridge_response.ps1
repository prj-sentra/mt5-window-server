$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'validate_bridge_response.ps1')

function New-ValidResponse {
    [pscustomobject]@{
        contractVersion = 3; server = 'Broker-Server'; accountLogin = [int64]12345; cursor = 'signed-cursor'; ledgerSemanticsVersion = 1
        deals = @([pscustomobject]@{ ticket='9001'; order='8001'; positionId='5001'; timeMsc=1760000000000; entry=0 })
        orders = @()
        positionEntryBalances = @([pscustomobject]@{ positionId='5001'; entryDealTicket='9001'; entryOrderTicket='8001'; entryTimeMsc=1760000000000; preEntryBalance='10000.12'; ledgerSemanticsVersion=1 })
        unsupportedPositionEntryBalances = @([pscustomobject]@{ kind='UNANCHORED'; positionId='5002'; reason='OPENING_DEAL_OUTSIDE_HISTORY'; ledgerSemanticsVersion=1 })
    }
}
function Assert-Passes { param([scriptblock]$Action,[string]$Name) try { & $Action; Write-Host "PASS: $Name" } catch { throw "Expected '$Name' to pass: $($_.Exception.Message)" } }
function Assert-Fails { param([scriptblock]$Action,[string]$Name) try { & $Action } catch { Write-Host "PASS: $Name rejected"; return }; throw "Expected '$Name' to be rejected" }
Assert-Passes { Assert-BridgeV3Response (New-ValidResponse) 'Broker-Server' 12345 } 'valid v3 response'
Assert-Fails { $r=New-ValidResponse; $r.contractVersion=2; Assert-BridgeV3Response $r 'Broker-Server' 12345 } 'v2 contract'
Assert-Fails { $r=New-ValidResponse; $r.positionEntryBalances[0].entryOrderTicket='other'; Assert-BridgeV3Response $r 'Broker-Server' 12345 } 'exact deal anchor mismatch'
Assert-Fails { $r=New-ValidResponse; $r.unsupportedPositionEntryBalances[0] | Add-Member entryDealTicket '1'; Assert-BridgeV3Response $r 'Broker-Server' 12345 } 'unanchored anchor'
Assert-Fails { $r=New-ValidResponse; $r.unsupportedPositionEntryBalances[0].reason='UNSUPPORTED_INOUT'; Assert-BridgeV3Response $r 'Broker-Server' 12345 } 'unanchored wrong reason'
Assert-Passes { $r=New-ValidResponse; $r.unsupportedPositionEntryBalances += [pscustomobject]@{ kind='ANCHORED'; positionId='5003'; entryDealTicket='9002'; entryOrderTicket='8002'; entryTimeMsc=1760000000001; reason='UNSUPPORTED_INOUT'; ledgerSemanticsVersion=1 }; Assert-BridgeV3Response $r 'Broker-Server' 12345 } 'supported anchored reason'
Assert-Fails { $r=New-ValidResponse; $r.unsupportedPositionEntryBalances += [pscustomobject]@{ kind='ANCHORED'; positionId='5003'; entryDealTicket='9002'; entryOrderTicket='8002'; entryTimeMsc=1760000000001; reason='UNKNOWN'; ledgerSemanticsVersion=1 }; Assert-BridgeV3Response $r 'Broker-Server' 12345 } 'unknown anchored reason'
Assert-Fails { $r=New-ValidResponse; $r.unsupportedPositionEntryBalances += [pscustomobject]@{ kind='ANCHORED'; positionId='5001'; entryDealTicket='2'; entryOrderTicket='2'; entryTimeMsc=2; reason='UNSUPPORTED_INOUT'; ledgerSemanticsVersion=1 }; Assert-BridgeV3Response $r 'Broker-Server' 12345 } 'cross-set position duplicate'
Write-Host 'All bridge response validation tests passed.'
