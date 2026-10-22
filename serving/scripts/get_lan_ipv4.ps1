param(
    [ValidateRange(1, 65535)]
    [int]$FrontendPort = 5173,
    [string]$AddressList
)
$ErrorActionPreference = 'Stop'

# Return every usable private IPv4 on active, routed Wi-Fi/Ethernet adapters.
# Requiring an IPv4 gateway excludes common WSL/VMware host-only adapters.
$interfaceRows = @(
    foreach ($interface in [System.Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces()) {
        if ($interface.OperationalStatus -ne 'Up' -or $interface.NetworkInterfaceType -notin @(
            [System.Net.NetworkInformation.NetworkInterfaceType]::Wireless80211,
            [System.Net.NetworkInformation.NetworkInterfaceType]::Ethernet
        )) { continue }

        $hasGateway = @($interface.GetIPProperties().GatewayAddresses | Where-Object {
            $_.Address.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork -and
            $_.Address.ToString() -ne '0.0.0.0'
        }).Count -gt 0
        if (-not $hasGateway) { continue }
        $priority = if ($hasGateway -and $interface.NetworkInterfaceType -eq [System.Net.NetworkInformation.NetworkInterfaceType]::Wireless80211) { 0 }
            elseif ($hasGateway) { 1 }
            elseif ($interface.NetworkInterfaceType -eq [System.Net.NetworkInformation.NetworkInterfaceType]::Wireless80211) { 2 }
            else { 3 }
        [pscustomobject]@{ Priority = $priority; Name = $interface.Name; Interface = $interface }
    }
)
$interfaces = @($interfaceRows | Sort-Object Priority, Name | ForEach-Object { $_.Interface })

if (-not [string]::IsNullOrWhiteSpace($AddressList)) {
    $addresses = @($AddressList -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ } | Select-Object -Unique)
} else {
    $addresses = @()
    foreach ($interface in $interfaces) {
        foreach ($entry in $interface.GetIPProperties().UnicastAddresses) {
            if ($entry.Address.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) { continue }
            $ip = $entry.Address.ToString()
            if ($ip -eq '127.0.0.1' -or $ip -like '169.254.*' -or $ip -eq '0.0.0.0') { continue }
            if (-not ($ip -match '^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)')) { continue }
            if ($addresses -notcontains $ip) { $addresses += $ip }
        }
    }
}

if ($addresses.Count -eq 0) {
    Write-Error 'No active private Wi-Fi/Ethernet IPv4 address was found. Connect to a network or set TMF_LAN_IP/TMF_LAN_IPS manually.'
    exit 1
}

$primary = [string]$addresses[0]
$origins = @($addresses | ForEach-Object { "http://${_}:$FrontendPort" })
$origins += "http://localhost:$FrontendPort"
$origins += "http://127.0.0.1:$FrontendPort"
$ipList = [string]::Join(',', [string[]]$addresses)
$originList = [string]::Join(',', [string[]]$origins)
Write-Output ($primary + '|' + $ipList + '|' + $originList)
