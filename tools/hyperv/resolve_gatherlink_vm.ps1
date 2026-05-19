param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("gatherlink-vm-a", "gatherlink-vm-b", "gatherlink-vm-c")]
    [string] $Name,
    [int] $PingTimeoutSeconds = 1
)

$ErrorActionPreference = "Stop"

$VmMacs = @{
    "gatherlink-vm-a" = "00-15-5d-91-00-01"
    "gatherlink-vm-b" = "00-15-5d-92-00-01"
    "gatherlink-vm-c" = "00-15-5d-93-00-01"
}

function Get-DefaultSwitchPrefix {
    $ip = Get-NetIPAddress -InterfaceAlias "vEthernet (Default Switch)" -AddressFamily IPv4 -ErrorAction Stop |
        Select-Object -First 1
    $parts = $ip.IPAddress.Split(".")
    return "$($parts[0]).$($parts[1]).$($parts[2])"
}

function Find-IpByMac {
    param([string] $MacAddress)

    $escaped = [regex]::Escape($MacAddress)
    $line = arp -a | Select-String -Pattern $escaped -CaseSensitive:$false | Select-Object -First 1
    if (-not $line) {
        return $null
    }

    if ($line.Line -match "^\s*(\d+\.\d+\.\d+\.\d+)\s+") {
        return $Matches[1]
    }

    return $null
}

$mac = $VmMacs[$Name]
$existing = Find-IpByMac $mac
if ($existing) {
    Write-Output $existing
    exit 0
}

$prefix = Get-DefaultSwitchPrefix
$jobs = foreach ($hostId in 2..254) {
    Start-Job -ScriptBlock {
        param($Address, $TimeoutSeconds)
        Test-Connection -ComputerName $Address -Count 1 -Quiet -TimeoutSeconds $TimeoutSeconds | Out-Null
    } -ArgumentList "$prefix.$hostId", $PingTimeoutSeconds
}

Wait-Job $jobs -Timeout 30 | Out-Null
Remove-Job $jobs -Force -ErrorAction SilentlyContinue

$found = Find-IpByMac $mac
if (-not $found) {
    throw "Could not resolve $Name from ARP using MAC $mac on Default Switch prefix $prefix. Confirm the VM is running."
}

Write-Output $found
