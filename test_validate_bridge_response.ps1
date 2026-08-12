. "$PSScriptRoot\validate_bridge_response.ps1"
Set-StrictMode -Version Latest
function New-ValidResponse {
    param([bool]$HasMore = $false)
    $page = [ordered]@{ hasMore=$HasMore; bytes=512 }; if ($HasMore) { $page.nextCursor = 'opaque-page-cursor' }
    [pscustomobject]@{ contractVersion=5; server='Broker-Server'; accountLogin=12345; mode='bootstrap'; snapshotToMsc=1760000000000; account=[pscustomobject]@{ currency='USD'; currentBalance='10000.12'; currencyDigits=2 }; page=[pscustomobject]$page; deals=@([pscustomobject]@{ ticket='9001'; order='8001'; positionId='5001'; timeMsc=1760000000000; entry=0; profit=0.0; commission=-1.0; swap=0.0; fee=0.0 }); orders=@() }
}
function Assert-Passes { param([scriptblock]$Action,[string]$Name) try { & $Action; Write-Host "PASS: $Name" } catch { throw "Expected '$Name' to pass: $($_.Exception.Message)" } }
function Assert-Fails { param([scriptblock]$Action,[string]$Name) try { & $Action } catch { Write-Host "PASS: $Name rejected"; return }; throw "Expected '$Name' to be rejected" }
function Assert-V5 { param([object]$Response) Assert-BridgeV5Response $Response 'Broker-Server' 12345 1760000000000 }
Assert-Passes { Assert-V5 (New-ValidResponse) } 'valid final v5 page'
Assert-Passes { Assert-V5 (New-ValidResponse $true) } 'valid non-final v5 page'
Assert-Fails { $r=New-ValidResponse; $r.contractVersion=4; Assert-V5 $r } 'old v4 contract'
Assert-Fails { $r=New-ValidResponse; $r.mode='incremental'; Assert-V5 $r } 'wrong mode'
Assert-Fails { $r=New-ValidResponse; $r.snapshotToMsc=1760000000001; Assert-V5 $r } 'wrong snapshot'
Assert-Fails { $r=New-ValidResponse; $r.page.hasMore=$true; Assert-V5 $r } 'non-final page without cursor'
Assert-Fails { $r=New-ValidResponse; $r.page | Add-Member -NotePropertyName nextCursor -NotePropertyValue 'unexpected'; Assert-V5 $r } 'final page with cursor'
Assert-Fails { $r=New-ValidResponse; $r.page.bytes=1MB; Assert-V5 $r } 'one MiB page'
Assert-Fails { $r=New-ValidResponse; $r.page.bytes=12.5; Assert-V5 $r } 'fractional page bytes'
Assert-Fails { $r=New-ValidResponse; $r.deals='not-an-array'; Assert-V5 $r } 'malformed arrays'
Write-Host 'All bridge response validation tests passed.'
