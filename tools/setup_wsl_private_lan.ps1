param(
    [string]$Distro = "gatherlink-dev",
    [string]$NodeAAddress = "10.88.0.11",
    [string]$NodeBAddress = "10.88.0.12",
    [string]$LogPath = ""
)

$ErrorActionPreference = "Stop"
if (-not $LogPath) {
    $LogPath = Join-Path $env:USERPROFILE "Documents\setup-gatherlink-wsl-private-lan.log"
}

function Write-Log {
    param([string]$Message)
    $line = "$(Get-Date -Format o) $Message"
    Add-Content -LiteralPath $LogPath -Value $line
    Write-Host $Message
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LogPath) | Out-Null
Set-Content -LiteralPath $LogPath -Value "Gatherlink WSL private LAN setup started $(Get-Date -Format o)"

Write-Log "Configuring WSL private LAN aliases in distro '$Distro'."
Write-Log "WSL2 distros share one Linux network namespace, so this is configured once and visible to gatherlink-dev and gatherlink-peer."

$addresses = @(
    $NodeAAddress,
    $NodeBAddress,
    "10.88.1.11",
    "10.88.1.12",
    "10.88.2.11",
    "10.88.2.12",
    "10.88.3.11",
    "10.88.3.12"
)
$addressCommands = ($addresses | ForEach-Object { "sudo ip addr replace $_/32 dev lo" }) -join "; "
$script = "set -euo pipefail; $addressCommands; ip -br addr show lo"
wsl -d $Distro -- bash -lc $script | Tee-Object -FilePath $LogPath -Append

Write-Log "WSL private LAN setup complete."
Write-Log "Node A service address: $NodeAAddress"
Write-Log "Node B service address: $NodeBAddress"
Write-Log "Path A: 10.88.1.11 <-> 10.88.1.12"
Write-Log "Path B: 10.88.2.11 <-> 10.88.2.12"
Write-Log "Path C: 10.88.3.11 <-> 10.88.3.12"
