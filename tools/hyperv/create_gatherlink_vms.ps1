param(
    [ValidateSet("gatherlink-vm-a", "gatherlink-vm-b", "gatherlink-vm-c")]
    [string[]] $Name = @("gatherlink-vm-a", "gatherlink-vm-b", "gatherlink-vm-c"),
    [string] $VmRoot = "D:\hyper-v\gatherlink",
    [string] $IsoPath = "",
    [string] $InternetSwitchName = ""
)

$ErrorActionPreference = "Stop"

$VmSpecs = @(
    @{
        Name = "gatherlink-vm-a"
        MacBase = "00155D9100"
        PathIpNote = "path-a=10.91.1.11/24 path-b=10.91.2.11/24 path-c=10.91.3.11/24"
    },
    @{
        Name = "gatherlink-vm-b"
        MacBase = "00155D9200"
        PathIpNote = "path-a=10.91.1.12/24 path-b=10.91.2.12/24 path-c=10.91.3.12/24"
    },
    @{
        Name = "gatherlink-vm-c"
        MacBase = "00155D9300"
        PathIpNote = "path-a=10.91.1.13/24 path-b=10.91.2.13/24 path-c=10.91.3.13/24"
    }
)

$PathSwitches = @(
    @{ AdapterName = "path-a"; SwitchName = "gatherlink-path-a"; MacSuffix = "A1" },
    @{ AdapterName = "path-b"; SwitchName = "gatherlink-path-b"; MacSuffix = "B1" },
    @{ AdapterName = "path-c"; SwitchName = "gatherlink-path-c"; MacSuffix = "C1" }
)

$SelectedVmSpecs = $VmSpecs | Where-Object { $Name -contains $_.Name }

if (-not $InternetSwitchName) {
    $existingInternetAdapter = Get-VMNetworkAdapter -VMName "gatherlink-vm-a" -Name "internet" -ErrorAction SilentlyContinue
    if ($existingInternetAdapter -and $existingInternetAdapter.SwitchName) {
        $InternetSwitchName = $existingInternetAdapter.SwitchName
    } elseif (Get-VMSwitch -Name "External Network" -ErrorAction SilentlyContinue) {
        $InternetSwitchName = "External Network"
    } elseif (Get-VMSwitch -Name "Default Switch" -ErrorAction SilentlyContinue) {
        $InternetSwitchName = "Default Switch"
    }
}

if (-not $InternetSwitchName) {
    throw "No internet/management Hyper-V switch was selected. Pass -InternetSwitchName with an existing switch name."
}

if (-not $IsoPath) {
    $iso = Get-ChildItem -LiteralPath "D:\media\debian" -Filter "debian-*-amd64-netinst.iso" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ($iso) {
        $IsoPath = $iso.FullName
    }
}

if (-not $IsoPath -or -not (Test-Path -LiteralPath $IsoPath)) {
    throw "Debian netinst ISO was not found. Pass -IsoPath or run tools\hyperv\download_debian_netinst.ps1 first."
}

foreach ($switchName in @($InternetSwitchName, "gatherlink-path-a", "gatherlink-path-b", "gatherlink-path-c")) {
    if (-not (Get-VMSwitch -Name $switchName -ErrorAction SilentlyContinue)) {
        throw "Required Hyper-V switch '$switchName' does not exist."
    }
}

New-Item -ItemType Directory -Force -Path $VmRoot | Out-Null

foreach ($spec in $SelectedVmSpecs) {
    $vmName = $spec.Name
    if (Get-VM -Name $vmName -ErrorAction SilentlyContinue) {
        Write-Host "reused existing VM $vmName"
        continue
    }

    $vmPath = Join-Path $VmRoot $vmName
    $diskDirectory = Join-Path $vmPath "Virtual Hard Disks"
    $diskPath = Join-Path $diskDirectory "$vmName.vhdx"
    New-Item -ItemType Directory -Force -Path $diskDirectory | Out-Null

    Write-Host "creating VM $vmName under $vmPath"
    New-VM -Name $vmName -Generation 2 -MemoryStartupBytes 4GB -SwitchName $InternetSwitchName -Path $vmPath | Out-Null
    Rename-VMNetworkAdapter -VMName $vmName -Name "Network Adapter" -NewName "internet"
    Set-VMNetworkAdapter -VMName $vmName -Name "internet" -StaticMacAddress "$($spec.MacBase)01"

    Set-VMProcessor -VMName $vmName -Count 2
    Set-VMMemory -VMName $vmName -DynamicMemoryEnabled $false -StartupBytes 4GB

    New-VHD -Path $diskPath -SizeBytes 48GB -Dynamic | Out-Null
    Add-VMHardDiskDrive -VMName $vmName -Path $diskPath | Out-Null
    Add-VMDvdDrive -VMName $vmName -Path $IsoPath | Out-Null

    foreach ($pathSwitch in $PathSwitches) {
        Add-VMNetworkAdapter -VMName $vmName -Name $pathSwitch.AdapterName -SwitchName $pathSwitch.SwitchName
        Set-VMNetworkAdapter -VMName $vmName -Name $pathSwitch.AdapterName -StaticMacAddress "$($spec.MacBase)$($pathSwitch.MacSuffix)"
    }

    # Debian netinst media is easiest to boot consistently in this lab with Secure Boot disabled.
    $dvd = Get-VMDvdDrive -VMName $vmName
    Set-VMFirmware -VMName $vmName -EnableSecureBoot Off -FirstBootDevice $dvd
    Set-VM -Name $vmName -CheckpointType Disabled

    Write-Host "created VM $vmName ($($spec.PathIpNote))"
}

$SelectedVmSpecs.Name |
    ForEach-Object { Get-VM -Name $_ } |
    Select-Object Name, State, Generation, ProcessorCount, MemoryStartup, CheckpointType, Path |
    Format-Table -AutoSize

foreach ($vmName in $SelectedVmSpecs.Name) {
    Write-Host ""
    Write-Host "network adapters for $vmName"
    Get-VMNetworkAdapter -VMName $vmName |
        Select-Object VMName, Name, SwitchName, MacAddress |
        Format-Table -AutoSize
}
