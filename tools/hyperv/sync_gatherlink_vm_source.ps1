param(
    [ValidateSet("gatherlink-vm-a", "gatherlink-vm-b", "gatherlink-vm-c")]
    [string[]] $Name = @("gatherlink-vm-a", "gatherlink-vm-b", "gatherlink-vm-c"),
    [string] $Branch = "main",
    [string] $WslDistro = "gatherlink-dev",
    [string] $WslRepo = "/home/gatherlink-user/src/gatherlink",
    [string] $HostKeyA = "",
    [string] $HostKeyB = "",
    [string] $HostKeyC = "",
    [switch] $Install
)

$ErrorActionPreference = "Stop"

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$hostKeys = @{
    "gatherlink-vm-a" = $HostKeyA
    "gatherlink-vm-b" = $HostKeyB
    "gatherlink-vm-c" = $HostKeyC
}

function Invoke-GatherlinkVm {
    param(
        [string] $VmName,
        [string] $HostKey,
        [string] $Command
    )

    $args = @("-ExecutionPolicy", "Bypass", "-File", (Join-Path $scriptRoot "invoke_gatherlink_vm.ps1"), "-Name", $VmName)
    if ($HostKey) {
        $args += @("-HostKey", $HostKey)
    }
    $args += @("-RemoteCommand", $Command)
    & powershell.exe @args
    if ($LASTEXITCODE -ne 0) {
        throw "VM command failed for ${VmName}: ${Command}"
    }
}

foreach ($vmName in $Name) {
    $hostKey = $hostKeys[$vmName]
    if (-not $hostKey) {
        throw "Provide the matching -HostKeyA, -HostKeyB, or -HostKeyC for unattended sync of ${vmName}."
    }

    $ip = & (Join-Path $scriptRoot "resolve_gatherlink_vm.ps1") -Name $vmName
    Invoke-GatherlinkVm $vmName $hostKey "mkdir -p /home/gatherlink/repos && if [ ! -d /home/gatherlink/repos/gatherlink.git ]; then git init --bare /home/gatherlink/repos/gatherlink.git; fi && git --git-dir=/home/gatherlink/repos/gatherlink.git symbolic-ref HEAD refs/heads/${Branch} || true"

    $bashCommand = @"
set -e
cd '${WslRepo}'
GIT_SSH_COMMAND='/mnt/c/Progra~1/PuTTY/plink.exe -batch -agent -hostkey ${hostKey}' git push --force ssh://gatherlink@${ip}/home/gatherlink/repos/gatherlink.git HEAD:refs/heads/${Branch}
"@
    & wsl -d $WslDistro -- bash -lc $bashCommand
    if ($LASTEXITCODE -ne 0) {
        throw "git push to ${vmName} failed"
    }

    $installCommand = ""
    if ($Install) {
        $installCommand = " && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt >/tmp/gatherlink-pip-install.log && .venv/bin/pip install -e . >>/tmp/gatherlink-pip-install.log && .venv/bin/maturin develop --manifest-path crates/pybindings/Cargo.toml --release >/tmp/gatherlink-maturin.log"
    }
    Invoke-GatherlinkVm $vmName $hostKey "mkdir -p /home/gatherlink/src && if [ ! -d /home/gatherlink/src/gatherlink/.git ]; then rm -rf /home/gatherlink/src/gatherlink && git clone /home/gatherlink/repos/gatherlink.git /home/gatherlink/src/gatherlink; fi && cd /home/gatherlink/src/gatherlink && git fetch origin && git reset --hard origin/${Branch}${installCommand}"
}
