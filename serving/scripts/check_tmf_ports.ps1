$ErrorActionPreference = 'Stop'
$managedPorts = @(5173, 8000, 8101, 8103, 8104, 8105, 8106)
$active = @()

foreach ($line in (& netstat.exe -ano -p tcp)) {
    if ($line -match '^\s*TCP\s+\S+:(\d+)\s+\S+\s+LISTENING\s+(\d+)\s*$') {
        $port = [int]$Matches[1]
        if ($managedPorts -contains $port) {
            $active += [pscustomobject]@{ Port = $port; PID = [int]$Matches[2] }
        }
    }
}

if ($active.Count -gt 0) {
    $active | Sort-Object Port, PID -Unique | Format-Table -AutoSize
    exit 1
}

Write-Output 'All TMF frontend, gateway, and worker ports are released.'
exit 0
