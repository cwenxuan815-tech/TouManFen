param(
    [Parameter(Mandatory = $true)]
    [ValidateRange(1, 65535)]
    [int]$Port,
    [Parameter(Mandatory = $true)]
    [string]$LanIPv4,
    [Parameter(Mandatory = $true)]
    [ValidateRange(1, 65535)]
    [int]$GatewayPort
)

# netstat works in ordinary, non-elevated PowerShell sessions where
# Get-NetTCPConnection may be restricted by the host environment.
$listenerRegex = '^\s*TCP\s+(\S+):' + $Port + '\s+\S+\s+LISTENING\s+\d+\s*$'
$listeners = @()
foreach ($line in (& netstat.exe -ano -p tcp)) {
    if ($line -match $listenerRegex) {
        $listeners += $Matches[1]
    }
}
if ($listeners.Count -eq 0) {
    Write-Output 'FREE'
    exit 0
}

$lanBound = @($listeners | Where-Object {
    $_ -eq '0.0.0.0' -or $_ -eq '::' -or $_ -eq $LanIPv4
}).Count -gt 0
if (-not $lanBound) {
    Write-Output 'OTHER'
    exit 0
}

try {
    $page = Invoke-WebRequest -Uri "http://${LanIPv4}:$Port/" -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
    $apiModule = Invoke-WebRequest -Uri "http://${LanIPv4}:$Port/src/api/client.ts" -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
    $expectedApi = "http://${LanIPv4}:$GatewayPort"
    if ($page.StatusCode -eq 200 -and $apiModule.Content.Contains($expectedApi)) {
        Write-Output 'TMF'
    } else {
        Write-Output 'OTHER'
    }
} catch {
    Write-Output 'OTHER'
}
