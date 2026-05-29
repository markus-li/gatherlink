param(
    [Parameter(Mandatory = $true)]
    [string] $Out,
    [int] $DurationSeconds = 60,
    [int] $IntervalSeconds = 1,
    [ValidateSet("minimal", "full")]
    [string] $Profile = "minimal"
)

$ErrorActionPreference = "Stop"

function New-Summary {
    return @{
        count = 0
        sum = 0.0
        max = 0.0
    }
}

function Add-Sample {
    param(
        [hashtable] $Summary,
        [double] $Value
    )
    $Summary.count += 1
    $Summary.sum += $Value
    if ($Value -gt $Summary.max) {
        $Summary.max = $Value
    }
}

function Convert-Summary {
    param([hashtable] $Summary)
    $average = 0.0
    if ($Summary.count -gt 0) {
        $average = $Summary.sum / $Summary.count
    }
    return @{
        average = $average
        max = $Summary.max
        samples = $Summary.count
    }
}

$minimalCounterPaths = @(
    "\Hyper-V Virtual Switch(*)\Bytes/sec",
    "\Hyper-V Virtual Switch(*)\Packets/sec",
    "\Hyper-V Virtual Switch(*)\Dropped Packets Incoming/sec",
    "\Hyper-V Virtual Switch(*)\Dropped Packets Outgoing/sec",
    "\Hyper-V Hypervisor Logical Processor(_Total)\% Total Run Time",
    "\Hyper-V Hypervisor Logical Processor(_Total)\% Guest Run Time",
    "\Hyper-V Hypervisor Logical Processor(_Total)\% Hypervisor Run Time"
)

$fullCounterPaths = @(
    "\Hyper-V Virtual Switch(*)\Bytes/sec",
    "\Hyper-V Virtual Switch(*)\Packets/sec",
    "\Hyper-V Virtual Switch(*)\Dropped Packets Incoming/sec",
    "\Hyper-V Virtual Switch(*)\Dropped Packets Outgoing/sec",
    "\Hyper-V Virtual Switch Port(*)\Bytes/sec",
    "\Hyper-V Virtual Switch Port(*)\Packets/sec",
    "\Hyper-V Virtual Switch Port(*)\Dropped Packets Incoming/sec",
    "\Hyper-V Virtual Switch Port(*)\Dropped Packets Outgoing/sec",
    "\Hyper-V Virtual Network Adapter(*)\Bytes/sec",
    "\Hyper-V Virtual Network Adapter(*)\Packets/sec",
    "\Hyper-V Virtual Network Adapter(*)\Dropped Packets Incoming/sec",
    "\Hyper-V Virtual Network Adapter(*)\Dropped Packets Outgoing/sec",
    "\Hyper-V Hypervisor Logical Processor(*)\% Total Run Time",
    "\Hyper-V Hypervisor Logical Processor(*)\% Guest Run Time",
    "\Hyper-V Hypervisor Logical Processor(*)\% Hypervisor Run Time"
)

$counterPaths = $minimalCounterPaths
if ($Profile -eq "full") {
    $counterPaths = $fullCounterPaths
}

$samples = [Math]::Max(1, [Math]::Ceiling($DurationSeconds / $IntervalSeconds))
$started = (Get-Date).ToUniversalTime().ToString("o")
$summaries = @{}

# Let the performance counter provider own the sampling cadence. In long VM
# benchmark runs this avoids a manual PowerShell sleep/read loop that can become
# harder to stop cleanly if a counter read stalls under host pressure.
$counterSamples = Get-Counter -Counter $counterPaths -SampleInterval $IntervalSeconds -MaxSamples $samples -ErrorAction SilentlyContinue
foreach ($counterSample in $counterSamples) {
    foreach ($sample in $counterSample.CounterSamples) {
        $path = $sample.Path
        if (-not $summaries.ContainsKey($path)) {
            $summaries[$path] = New-Summary
        }
        Add-Sample -Summary $summaries[$path] -Value ([double] $sample.CookedValue)
    }
}

$converted = @{}
foreach ($key in $summaries.Keys) {
    $converted[$key] = Convert-Summary -Summary $summaries[$key]
}

$report = [ordered]@{
    schema_version = 1
    started_utc = $started
    finished_utc = (Get-Date).ToUniversalTime().ToString("o")
    duration_seconds = $DurationSeconds
    interval_seconds = $IntervalSeconds
    profile = $Profile
    counters = $converted
}

$parent = Split-Path -Parent $Out
if ($parent) {
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
}
$report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $Out -Encoding UTF8
