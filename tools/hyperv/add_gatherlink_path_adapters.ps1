param(
    [ValidateSet("gatherlink-vm-a", "gatherlink-vm-b", "gatherlink-vm-c")]
    [string[]] $Name = @("gatherlink-vm-a", "gatherlink-vm-b", "gatherlink-vm-c"),
    [switch] $AllowStopRunningVms
)

$ErrorActionPreference = "Stop"

$VmSpecs = @(
    @{ Name = "gatherlink-vm-a"; MacBase = "00155D9100" },
    @{ Name = "gatherlink-vm-b"; MacBase = "00155D9200" },
    @{ Name = "gatherlink-vm-c"; MacBase = "00155D9300" }
)

$PathSwitches = @(
    @{ AdapterName = "path-a"; SwitchName = "gatherlink-path-a"; MacSuffix = "A1" },
    @{ AdapterName = "path-b"; SwitchName = "gatherlink-path-b"; MacSuffix = "B1" },
    @{ AdapterName = "path-c"; SwitchName = "gatherlink-path-c"; MacSuffix = "C1" },
    @{ AdapterName = "path-d"; SwitchName = "gatherlink-path-d"; MacSuffix = "D1" },
    @{ AdapterName = "path-e"; SwitchName = "gatherlink-path-e"; MacSuffix = "E1" }
)

foreach ($pathSwitch in $PathSwitches) {
    $existing = Get-VMSwitch -Name $pathSwitch.SwitchName -ErrorAction SilentlyContinue
    if ($null -eq $existing) {
        New-VMSwitch -Name $pathSwitch.SwitchName -SwitchType Private | Out-Null
        Write-Host "created switch $($pathSwitch.SwitchName)"
        continue
    }
    if ($existing.SwitchType.ToString() -ne "Private") {
        throw "switch $($pathSwitch.SwitchName) exists but has type $($existing.SwitchType), expected Private"
    }
}

foreach ($spec in ($VmSpecs | Where-Object { $Name -contains $_.Name })) {
    $vm = Get-VM -Name $spec.Name -ErrorAction SilentlyContinue
    if (-not $vm) {
        throw "VM $($spec.Name) does not exist."
    }

    $needsOfflineChange = $false
    foreach ($pathSwitch in $PathSwitches) {
        $expectedMac = "$($spec.MacBase)$($pathSwitch.MacSuffix)"
        $adapter = Get-VMNetworkAdapter -VMName $spec.Name -Name $pathSwitch.AdapterName -ErrorAction SilentlyContinue
        if ($null -eq $adapter) {
            $needsOfflineChange = $true
            continue
        }
        if ($adapter.MacAddress -ne $expectedMac -or $adapter.DynamicMacAddressEnabled) {
            $needsOfflineChange = $true
        }
    }

    $wasRunning = $vm.State -eq "Running"
    if ($wasRunning -and $needsOfflineChange) {
        if (-not $AllowStopRunningVms) {
            throw "VM $($spec.Name) is running and needs adapter MAC changes. Re-run with -AllowStopRunningVms."
        }
        Write-Host "stopping $($spec.Name) so Hyper-V can apply deterministic lab MAC addresses"
        Stop-VM -Name $spec.Name -Force
    }

    foreach ($pathSwitch in $PathSwitches) {
        $expectedMac = "$($spec.MacBase)$($pathSwitch.MacSuffix)"
        $adapter = Get-VMNetworkAdapter -VMName $spec.Name -Name $pathSwitch.AdapterName -ErrorAction SilentlyContinue
        if ($null -eq $adapter) {
            Add-VMNetworkAdapter `
                -VMName $spec.Name `
                -Name $pathSwitch.AdapterName `
                -SwitchName $pathSwitch.SwitchName `
                -StaticMacAddress $expectedMac
            Write-Host "added $($pathSwitch.AdapterName) to $($spec.Name)"
        } elseif ($adapter.SwitchName -ne $pathSwitch.SwitchName) {
            Connect-VMNetworkAdapter -VMName $spec.Name -Name $pathSwitch.AdapterName -SwitchName $pathSwitch.SwitchName
            Write-Host "connected $($spec.Name)/$($pathSwitch.AdapterName) to $($pathSwitch.SwitchName)"
        }

        $adapter = Get-VMNetworkAdapter -VMName $spec.Name -Name $pathSwitch.AdapterName
        if ($adapter.MacAddress -ne $expectedMac -or $adapter.DynamicMacAddressEnabled) {
            Set-VMNetworkAdapter -VMName $spec.Name -Name $pathSwitch.AdapterName -StaticMacAddress $expectedMac
        }
    }

    if ($wasRunning -and $needsOfflineChange) {
        Start-VM -Name $spec.Name
        Write-Host "restarted $($spec.Name)"
    }
}

foreach ($vmName in $Name) {
    Write-Host ""
    Write-Host "network adapters for $vmName"
    Get-VMNetworkAdapter -VMName $vmName |
        Select-Object VMName, Name, SwitchName, MacAddress |
        Sort-Object Name |
        Format-Table -AutoSize
}
