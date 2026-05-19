param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("gatherlink-vm-a", "gatherlink-vm-b", "gatherlink-vm-c")]
    [string] $Name,
    [string] $HostKey = "",
    [Parameter(Mandatory = $true, ValueFromRemainingArguments = $true)]
    [string[]] $RemoteCommand
)

$ErrorActionPreference = "Stop"

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ip = & (Join-Path $scriptRoot "resolve_gatherlink_vm.ps1") -Name $Name
$remoteCommandText = $RemoteCommand -join " "

Write-Host "$Name -> $ip"
$plinkArgs = @("-batch", "-agent")
if ($HostKey) {
    $plinkArgs += @("-hostkey", $HostKey)
}
$plinkArgs += @("-l", "gatherlink", $ip, $remoteCommandText)

& plink.exe @plinkArgs
exit $LASTEXITCODE
