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

$lanAddresses = @($LanIPv4 -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
$lanBound = @($listeners | Where-Object {
    $_ -eq '0.0.0.0' -or $_ -eq '::' -or $lanAddresses -contains $_
}).Count -gt 0
if (-not $lanBound) {
    Write-Output 'OTHER'
    exit 0
}

try {
    foreach ($address in $lanAddresses) {
        $page = Invoke-WebRequest -Uri "http://${address}:$Port/" -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
        $apiModule = Invoke-WebRequest -Uri "http://${address}:$Port/src/api/client.ts" -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
        $health = Invoke-WebRequest -Uri "http://${address}:$GatewayPort/api/v1/healthz" -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
        if ($page.StatusCode -ne 200 -or
            $health.StatusCode -ne 200 -or
            $apiModule.Content -notmatch 'window\.location\.hostname') {
            Write-Output 'OTHER'
            exit 0
        }
    }
    Write-Output 'TMF'
} catch {
    Write-Output 'OTHER'
}
