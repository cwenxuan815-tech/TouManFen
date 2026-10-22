$ErrorActionPreference = 'Stop'

# Use .NET network interfaces to avoid requiring administrator access or the
# elevated NetTCPIP cmdlets. Prefer Wi-Fi; Ethernet is a fallback.
$interfaces = [System.Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces() |
    Where-Object { $_.OperationalStatus -eq 'Up' -and $_.NetworkInterfaceType -ne 'Loopback' } |
    Sort-Object @{ Expression = { if ($_.NetworkInterfaceType -eq 'Wireless80211') { 0 } else { 1 } } }, Name

foreach ($interface in $interfaces) {
    $properties = $interface.GetIPProperties()
    $hasGateway = @($properties.GatewayAddresses | Where-Object {
        $_.Address.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork -and
        $_.Address.ToString() -ne '0.0.0.0'
    }).Count -gt 0
    if (-not $hasGateway) { continue }

    $address = $properties.UnicastAddresses |
        Where-Object {
            $_.Address.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork -and
            $_.Address.ToString() -notlike '169.254.*' -and
            $_.Address.ToString() -ne '127.0.0.1'
        } |
        Select-Object -First 1
    if ($address) {
        Write-Output $address.Address.ToString()
        exit 0
    }
}

Write-Error 'No active Wi-Fi/Ethernet IPv4 default gateway was found. Connect to a network or set TMF_LAN_IP manually.'
exit 1
