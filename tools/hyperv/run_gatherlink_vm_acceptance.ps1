param(
    [string] $HostKeyA = "",
    [string] $HostKeyB = "",
    [string] $Branch = "project-orientation",
    [int] $Count = 5,
    [switch] $SkipSync,
    [switch] $KeepRunning
)

$ErrorActionPreference = "Stop"

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptRoot "..\..")
$configA = "configs/hyperv/two-vm-node-a.json"
$configB = "configs/hyperv/two-vm-node-b.json"

function Invoke-Vm {
    param(
        [string] $Name,
        [string] $HostKey,
        [string] $Command
    )
    $args = @("-ExecutionPolicy", "Bypass", "-File", (Join-Path $scriptRoot "invoke_gatherlink_vm.ps1"), "-Name", $Name)
    if ($HostKey) {
        $args += @("-HostKey", $HostKey)
    }
    $args += @("-RemoteCommand", $Command)
    & powershell.exe @args
    if ($LASTEXITCODE -ne 0) {
        throw "VM command failed for ${Name}: ${Command}"
    }
}

if (-not $HostKeyA -or -not $HostKeyB) {
    throw "Provide -HostKeyA and -HostKeyB for non-interactive Pageant-backed VM acceptance."
}

if (-not $SkipSync) {
    & powershell.exe -ExecutionPolicy Bypass -File (Join-Path $scriptRoot "sync_gatherlink_vm_source.ps1") -Branch $Branch -HostKeyA $HostKeyA -HostKeyB $HostKeyB -Install
    if ($LASTEXITCODE -ne 0) {
        throw "source sync failed"
    }
}

Write-Host "Validating configs on both VMs"
Invoke-Vm "gatherlink-vm-a" $HostKeyA "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink config validate ${configA}"
Invoke-Vm "gatherlink-vm-b" $HostKeyB "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink config validate ${configB}"

Write-Host "Resetting previous services and path shaping"
Invoke-Vm "gatherlink-vm-a" $HostKeyA "cd /home/gatherlink/src/gatherlink && (.venv/bin/gatherlink services close vm.node-a || true); sudo tc qdisc del dev path-a root 2>/dev/null || true; sudo tc qdisc del dev path-b root 2>/dev/null || true; sudo tc qdisc del dev path-c root 2>/dev/null || true; sudo ip link set path-a up; sudo ip link set path-b up; sudo ip link set path-c up"
Invoke-Vm "gatherlink-vm-b" $HostKeyB "cd /home/gatherlink/src/gatherlink && (.venv/bin/gatherlink services close vm.node-b || true); sudo tc qdisc del dev path-a root 2>/dev/null || true; sudo tc qdisc del dev path-b root 2>/dev/null || true; sudo tc qdisc del dev path-c root 2>/dev/null || true; sudo ip link set path-a up; sudo ip link set path-b up; sudo ip link set path-c up"

Write-Host "Starting node services"
Invoke-Vm "gatherlink-vm-b" $HostKeyB "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink run start ${configB} --name vm.node-b --diagnostics-jsonl /tmp/gatherlink-node-b.jsonl"
Start-Sleep -Seconds 1
Invoke-Vm "gatherlink-vm-a" $HostKeyA "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink run start ${configA} --name vm.node-a --diagnostics-jsonl /tmp/gatherlink-node-a.jsonl"
Start-Sleep -Seconds 2

Write-Host "Sending UDP traffic through Gatherlink"
Invoke-Vm "gatherlink-vm-b" $HostKeyB "cd /home/gatherlink/src/gatherlink && rm -f /tmp/gatherlink-vm-received.txt; (timeout 20 .venv/bin/python tools/udp_probe.py receive 127.0.0.1:51820 --count ${Count} > /tmp/gatherlink-vm-received.txt 2>&1 & echo `$! > /tmp/gatherlink-vm-receiver.pid)"
Start-Sleep -Seconds 1
Invoke-Vm "gatherlink-vm-a" $HostKeyA "cd /home/gatherlink/src/gatherlink && .venv/bin/python tools/udp_probe.py send 127.0.0.1:55180 hyperv-vm-acceptance --count ${Count}"
Start-Sleep -Seconds 2
Invoke-Vm "gatherlink-vm-b" $HostKeyB "test `$(grep -c '^hyperv-vm-acceptance' /tmp/gatherlink-vm-received.txt) -eq ${Count} && cat /tmp/gatherlink-vm-received.txt"

Write-Host "Checking status and monitor output"
Invoke-Vm "gatherlink-vm-a" $HostKeyA "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services status vm.node-a"
Invoke-Vm "gatherlink-vm-b" $HostKeyB "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services status vm.node-b"
Invoke-Vm "gatherlink-vm-a" $HostKeyA "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services monitor vm.node-a --once"
Invoke-Vm "gatherlink-vm-b" $HostKeyB "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services monitor vm.node-b --once"

Write-Host "Applying a simple path degradation and recovery check"
Invoke-Vm "gatherlink-vm-a" $HostKeyA "sudo tc qdisc replace dev path-c root netem rate 500kbit delay 20ms loss 1%"
Invoke-Vm "gatherlink-vm-a" $HostKeyA "tc qdisc show dev path-c"
Invoke-Vm "gatherlink-vm-a" $HostKeyA "sudo tc qdisc del dev path-c root"

if (-not $KeepRunning) {
    Write-Host "Stopping services"
    Invoke-Vm "gatherlink-vm-a" $HostKeyA "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services close vm.node-a"
    Invoke-Vm "gatherlink-vm-b" $HostKeyB "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services close vm.node-b"
}

Write-Host "Hyper-V VM acceptance completed"
