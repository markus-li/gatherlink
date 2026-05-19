$ErrorActionPreference = "Stop"

$RequiredSwitches = @(
    @{ Name = "gatherlink-path-a"; Type = "Private" },
    @{ Name = "gatherlink-path-b"; Type = "Private" },
    @{ Name = "gatherlink-path-c"; Type = "Private" }
)

foreach ($item in $RequiredSwitches) {
    $existing = Get-VMSwitch -Name $item.Name -ErrorAction SilentlyContinue
    if ($null -eq $existing) {
        New-VMSwitch -Name $item.Name -SwitchType $item.Type | Out-Null
        Write-Host "created switch $($item.Name) type=$($item.Type)"
        continue
    }

    if ($existing.SwitchType.ToString() -ne $item.Type) {
        throw "switch $($item.Name) exists but has type $($existing.SwitchType), expected $($item.Type)"
    }

    Write-Host "reused switch $($item.Name) type=$($existing.SwitchType)"
}

$internet = Get-VMSwitch -Name "External Network" -ErrorAction SilentlyContinue
if ($null -eq $internet) {
    Write-Warning "internet switch 'External Network' was not found; create or choose one before attaching VM internet NICs"
} else {
    Write-Host "internet switch ready External Network type=$($internet.SwitchType)"
}

Get-VMSwitch |
    Where-Object { $_.Name -in @("External Network", "gatherlink-path-a", "gatherlink-path-b", "gatherlink-path-c") } |
    Select-Object Name, SwitchType, NetAdapterInterfaceDescription, AllowManagementOS |
    Format-Table -AutoSize
