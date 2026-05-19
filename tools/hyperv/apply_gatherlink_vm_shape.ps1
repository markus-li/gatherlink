param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("gatherlink-vm-a", "gatherlink-vm-b", "gatherlink-vm-c")]
    [string] $Name,
    [Parameter(Mandatory = $true)]
    [ValidateSet("path-a", "path-b", "path-c")]
    [string] $Interface,
    [string] $HostKey = "",
    [string] $Rate = "",
    [string] $Delay = "",
    [string] $Jitter = "",
    [string] $Loss = "",
    [int] $Mtu = 0,
    [ValidateSet("up", "down", "unchanged")]
    [string] $State = "unchanged",
    [switch] $Clear
)

$ErrorActionPreference = "Stop"
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

if ($Clear) {
    $remote = "sudo tc qdisc del dev ${Interface} root 2>/dev/null || true; sudo ip link set dev ${Interface} up"
} else {
    $parts = @("sudo tc qdisc replace dev ${Interface} root netem")
    if ($Rate) {
        $parts += "rate ${Rate}"
    }
    if ($Delay) {
        $parts += "delay ${Delay}"
        if ($Jitter) {
            $parts += $Jitter
        }
    }
    if ($Loss) {
        $parts += "loss ${Loss}"
    }
    $commands = @($parts -join " ")
    if ($Mtu -gt 0) {
        $commands += "sudo ip link set dev ${Interface} mtu ${Mtu}"
    }
    if ($State -ne "unchanged") {
        $commands += "sudo ip link set dev ${Interface} ${State}"
    }
    $remote = $commands -join "; "
}

$args = @("-ExecutionPolicy", "Bypass", "-File", (Join-Path $scriptRoot "invoke_gatherlink_vm.ps1"), "-Name", $Name)
if ($HostKey) {
    $args += @("-HostKey", $HostKey)
}
$args += @("-RemoteCommand", $remote)
& powershell.exe @args
exit $LASTEXITCODE
