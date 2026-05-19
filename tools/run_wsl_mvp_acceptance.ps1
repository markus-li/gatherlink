param(
    [string]$PrimaryDistro = "gatherlink-dev",
    [string]$PeerDistro = "gatherlink-peer",
    [string]$User = "markus",
    [string]$Repo = "/home/markus/src/gatherlink",
    [string]$Branch = "project-orientation",
    [string]$BundlePath = "",
    [int]$PacketCount = 12,
    [switch]$ClearShaping
)

$ErrorActionPreference = "Stop"
if (-not $BundlePath) {
    $BundlePath = "/mnt/c/Users/$env:USERNAME/Documents/gatherlink-project-orientation.bundle"
}
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$SetupScript = Join-Path $ScriptRoot "setup_wsl_private_lan.ps1"
$NodeAService = "core.windows-node-a"
$NodeBService = "core.windows-node-b"
$Paths = @("wsl-path-a", "wsl-path-b", "wsl-path-c")

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message"
}

function Quote-Bash {
    param([string]$Value)
    return "'" + ($Value -replace "'", "'\''") + "'"
}

function Invoke-WslText {
    param(
        [string]$Distro,
        [string]$Command,
        [string]$AsUser = "",
        [switch]$AllowFailure
    )

    $arguments = @("-d", $Distro)
    if ($AsUser) {
        $arguments += @("-u", $AsUser)
    }
    $arguments += @("--", "bash", "-lc", $Command)
    $oldErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & wsl @arguments 2>&1
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $oldErrorActionPreference
    }
    $text = ($output | ForEach-Object { "$_" }) -join "`n"
    if ($exitCode -ne 0 -and -not $AllowFailure) {
        throw "WSL command failed in ${Distro}: exit=${exitCode}`n${Command}`n${text}"
    }
    return $text
}

function Invoke-Gatherlink {
    param(
        [string]$Distro,
        [string]$Command,
        [string]$AsUser = $User,
        [switch]$AllowFailure
    )

    $quotedRepo = Quote-Bash $Repo
    return Invoke-WslText `
        -Distro $Distro `
        -AsUser $AsUser `
        -AllowFailure:$AllowFailure `
        -Command "cd $quotedRepo && . .venv/bin/activate && $Command"
}

function Invoke-GatherlinkJson {
    param(
        [string]$Distro,
        [string]$Command,
        [string]$AsUser = $User
    )

    $text = Invoke-Gatherlink -Distro $Distro -AsUser $AsUser -Command $Command
    return $text | ConvertFrom-Json
}

function Wait-ServiceStatus {
    param(
        [string]$Distro,
        [string]$Name,
        [string]$AsUser = $User
    )

    for ($attempt = 1; $attempt -le 20; $attempt++) {
        try {
            return Invoke-GatherlinkJson -Distro $Distro -AsUser $AsUser -Command "gatherlink services status $Name"
        }
        catch {
            Start-Sleep -Milliseconds 250
        }
    }
    throw "Service did not become IPC-readable: ${Distro}/${Name}"
}

function Assert-PathCounter {
    param(
        [object]$Status,
        [string]$Path,
        [string]$Counter,
        [int64]$Minimum
    )

    $pathProperty = $Status.path_stats.PSObject.Properties[$Path]
    if ($null -eq $pathProperty) {
        throw "Status did not include path stats for $Path"
    }
    $value = [int64]$pathProperty.Value.$Counter
    if ($value -lt $Minimum) {
        throw "Expected $Path $Counter >= $Minimum, got $value"
    }
}

function Assert-DiagnosticsJsonl {
    param(
        [string]$Distro,
        [string]$Name
    )

    $path = ".gatherlink/services/$Name/diagnostics.jsonl"
    $line = Invoke-Gatherlink -Distro $Distro -Command "test -s $path && tail -n 1 $path"
    $null = $line | ConvertFrom-Json
    Write-Host "diagnostics OK: ${Distro}/${Name} $path"
}

function Apply-StandardShaping {
    Invoke-WslText `
        -Distro $PrimaryDistro `
        -AsUser $User `
        -Command "cd $(Quote-Bash $Repo) && sudo tools/wsl_shape_private_lan.sh clear >/dev/null 2>&1 || true; sudo tools/wsl_shape_private_lan.sh apply path-a.ab=3mbit path-a.ba=2mbit path-b.ab=1500kbit path-b.ba=1200kbit path-c.ab=750kbit path-c.ba=900kbit"
}

function Drop-PathATowardPeer {
    Invoke-WslText `
        -Distro $PrimaryDistro `
        -AsUser $User `
        -Command "sudo tc filter replace dev lo protocol ip parent 88: prio 1 flower ip_proto udp dst_ip 10.88.1.12 dst_port 57001 action drop"
}

function Send-And-Receive {
    param(
        [string]$Payload,
        [int]$Count,
        [int]$MinCount = $Count,
        [int]$TimeoutSeconds = 15
    )

    $quotedRepo = Quote-Bash $Repo
    $receiveCommand = "cd $quotedRepo && python3 tools/udp_probe.py receive 10.88.0.12:51820 --count $Count --min-count $MinCount --timeout $TimeoutSeconds"
    $receiver = Start-Job -ScriptBlock {
        param($Distro, $UserName, $CommandText)
        & wsl -d $Distro -u $UserName -- bash -lc $CommandText 2>&1
        exit $LASTEXITCODE
    } -ArgumentList $PeerDistro, $User, $receiveCommand
    Start-Sleep -Milliseconds 750
    Invoke-WslText `
        -Distro $PrimaryDistro `
        -AsUser $User `
        -Command "cd $quotedRepo && python3 tools/udp_probe.py send 10.88.0.11:55180 $Payload --count $Count"
    if (-not (Wait-Job $receiver -Timeout ($TimeoutSeconds + 5))) {
        Stop-Job $receiver
        throw "UDP receiver timed out"
    }
    $receiveOutput = (Receive-Job $receiver) -join "`n"
    if ($receiver.ChildJobs[0].JobStateInfo.State -eq "Failed") {
        throw "UDP receiver failed:`n$receiveOutput"
    }
    if ($receiveOutput -notmatch [regex]::Escape($Payload)) {
        throw "UDP receiver did not observe payload '$Payload':`n$receiveOutput"
    }
    if ($MinCount -eq $Count -and $receiveOutput -notmatch [regex]::Escape("$Payload-$Count")) {
        throw "UDP receiver did not observe final counted payload '$Payload-$Count':`n$receiveOutput"
    }
    Write-Host $receiveOutput
}

function Close-ServiceQuietly {
    param(
        [string]$Distro,
        [string]$Name
    )

    $null = Invoke-Gatherlink `
        -Distro $Distro `
        -Command "gatherlink services close $Name >/dev/null 2>&1 || true" `
        -AllowFailure
}

function Assert-ServiceStopped {
    param(
        [string]$Distro,
        [string]$Name
    )

    $list = Invoke-Gatherlink -Distro $Distro -Command "gatherlink services list"
    if ($list -notmatch [regex]::Escape($Name) -or $list -notmatch "state=stopped" -or $list -notmatch "pid=") {
        throw "Service list did not show stopped service ${Distro}/${Name}:`n$list"
    }
}

Write-Step "Configure WSL private LAN aliases"
& powershell.exe -ExecutionPolicy Bypass -File $SetupScript -Distro $PrimaryDistro
if ($LASTEXITCODE -ne 0) {
    throw "private LAN setup failed"
}

try {
    Write-Step "Synchronize peer distro from $PrimaryDistro"
    $quotedRepo = Quote-Bash $Repo
    $quotedBundle = Quote-Bash $BundlePath
    Invoke-WslText `
        -Distro $PrimaryDistro `
        -AsUser $User `
        -Command "cd $quotedRepo && git bundle create $quotedBundle HEAD"
    Invoke-WslText `
        -Distro $PeerDistro `
        -AsUser $User `
        -Command "cd $quotedRepo && git fetch $quotedBundle HEAD:refs/remotes/bundle/$Branch && git checkout $Branch && git reset --hard refs/remotes/bundle/$Branch"

    Write-Step "Validate WSL node configs"
    Invoke-Gatherlink -Distro $PrimaryDistro -Command "gatherlink config validate configs/examples/windows-two-node-a.json"
    Invoke-Gatherlink -Distro $PeerDistro -Command "gatherlink config validate configs/examples/windows-two-node-b.json"

    Write-Step "Apply three-path shaping"
    Apply-StandardShaping

    Write-Step "Start managed encrypted services"
    Close-ServiceQuietly -Distro $PrimaryDistro -Name $NodeAService
    Close-ServiceQuietly -Distro $PeerDistro -Name $NodeBService
    Invoke-Gatherlink `
        -Distro $PeerDistro `
        -Command "gatherlink run start configs/examples/windows-two-node-b.json --name $NodeBService --scheduler-reapply-interval 5"
    Invoke-Gatherlink `
        -Distro $PrimaryDistro `
        -Command "gatherlink run start configs/examples/windows-two-node-a.json --name $NodeAService --scheduler-reapply-interval 5"
    $null = Wait-ServiceStatus -Distro $PeerDistro -Name $NodeBService
    $null = Wait-ServiceStatus -Distro $PrimaryDistro -Name $NodeAService

    Write-Step "Send counted UDP payloads across Gatherlink"
    Send-And-Receive -Payload "mvp-acceptance" -Count $PacketCount

    Write-Step "Drop one carrier path, prove remaining paths still carry traffic, then recover"
    Drop-PathATowardPeer
    Send-And-Receive -Payload "mvp-path-a-down" -Count 9 -MinCount 1 -TimeoutSeconds 8
    Apply-StandardShaping
    Send-And-Receive -Payload "mvp-path-a-recovered" -Count 6

    Write-Step "Validate service status, per-path counters, and monitor output"
    $nodeAStatus = Wait-ServiceStatus -Distro $PrimaryDistro -Name $NodeAService
    $nodeBStatus = Wait-ServiceStatus -Distro $PeerDistro -Name $NodeBService
    foreach ($path in $Paths) {
        Assert-PathCounter -Status $nodeAStatus -Path $path -Counter "tx_packets" -Minimum 1
        Assert-PathCounter -Status $nodeBStatus -Path $path -Counter "rx_packets" -Minimum 1
    }
    $monitorA = Invoke-Gatherlink -Distro $PrimaryDistro -Command "gatherlink services monitor $NodeAService --once"
    $monitorB = Invoke-Gatherlink -Distro $PeerDistro -Command "gatherlink services monitor $NodeBService --once"
    foreach ($path in $Paths) {
        if ($monitorA -notmatch [regex]::Escape($path) -or $monitorB -notmatch [regex]::Escape($path)) {
            throw "Monitor output did not include $path"
        }
    }
    Write-Host $monitorA
    Write-Host $monitorB

    Write-Step "Validate JSONL diagnostics are present and parseable"
    Assert-DiagnosticsJsonl -Distro $PrimaryDistro -Name $NodeAService
    Assert-DiagnosticsJsonl -Distro $PeerDistro -Name $NodeBService
}
finally {
    Write-Step "Close managed services"
    Close-ServiceQuietly -Distro $PrimaryDistro -Name $NodeAService
    Close-ServiceQuietly -Distro $PeerDistro -Name $NodeBService
}

Write-Step "Verify clean service teardown"
Assert-ServiceStopped -Distro $PrimaryDistro -Name $NodeAService
Assert-ServiceStopped -Distro $PeerDistro -Name $NodeBService

if ($ClearShaping) {
    Write-Step "Clear WSL private LAN shaping"
    Invoke-WslText -Distro $PrimaryDistro -AsUser $User -Command "cd $(Quote-Bash $Repo) && sudo tools/wsl_shape_private_lan.sh clear"
}

Write-Host ""
Write-Host "PASS: WSL MVP acceptance carried $PacketCount encrypted UDP payloads across three shaped paths, exposed monitor counters, wrote diagnostics JSONL, and closed cleanly."
