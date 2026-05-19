param(
    [Parameter(Mandatory = $true)]
    [string] $PublicKeyPath,
    [string] $VmRoot = "D:\hyper-v\gatherlink",
    [string] $ImageDirectory = "D:\media\debian",
    [string] $WslDistro = "gatherlink-dev"
)

$ErrorActionPreference = "Stop"

$CloudImageName = "debian-13-genericcloud-amd64.qcow2"
$CloudImageUrl = "https://cloud.debian.org/images/cloud/trixie/latest/$CloudImageName"
$CloudImagePath = Join-Path $ImageDirectory $CloudImageName
$DiskSize = "48G"

$VmSpecs = @(
    @{
        Name = "gatherlink-vm-a"
        InstanceId = "gatherlink-vm-a"
        Hostname = "gatherlink-vm-a"
        MacBase = "00155D9100"
        PathA = "10.91.1.11/24"
        PathB = "10.91.2.11/24"
        PathC = "10.91.3.11/24"
    },
    @{
        Name = "gatherlink-vm-b"
        InstanceId = "gatherlink-vm-b"
        Hostname = "gatherlink-vm-b"
        MacBase = "00155D9200"
        PathA = "10.91.1.12/24"
        PathB = "10.91.2.12/24"
        PathC = "10.91.3.12/24"
    }
)

function Convert-ToWslPath {
    param([string] $Path)
    $escaped = $Path.Replace("'", "'\''")
    return (wsl -d $WslDistro -- bash -lc "wslpath -a '$escaped'").Trim()
}

function Invoke-Wsl {
    param([string] $Command)
    wsl -d $WslDistro -- bash -lc $Command
    if ($LASTEXITCODE -ne 0) {
        throw "WSL command failed with exit code $LASTEXITCODE"
    }
}

function Write-Utf8NoBom {
    param(
        [string] $Path,
        [string] $Content
    )

    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Format-MacForCloudInit {
    param([string] $MacAddress)

    $clean = $MacAddress.ToLower() -replace "[^0-9a-f]", ""
    if ($clean.Length -ne 12) {
        throw "Invalid MAC address for cloud-init network config: $MacAddress"
    }

    return (($clean -split "(.{2})" | Where-Object { $_ }) -join ":")
}

if (-not (Test-Path -LiteralPath $PublicKeyPath)) {
    throw "Public key file was not found: $PublicKeyPath"
}

$publicKeys = Get-Content -LiteralPath $PublicKeyPath |
    ForEach-Object { $_.Trim() } |
    Where-Object { $_ -and -not $_.StartsWith("#") }

if (-not $publicKeys) {
    throw "Public key file does not contain any OpenSSH public keys."
}

foreach ($publicKey in $publicKeys) {
    if (-not $publicKey.StartsWith("ssh-")) {
        throw "Public key file contains a line that does not look like an OpenSSH public key."
    }
}

Invoke-Wsl "command -v qemu-img >/dev/null && command -v cloud-localds >/dev/null"

New-Item -ItemType Directory -Force -Path $ImageDirectory | Out-Null
New-Item -ItemType Directory -Force -Path $VmRoot | Out-Null

if (Test-Path -LiteralPath $CloudImagePath) {
    Write-Host "reused existing Debian cloud image $CloudImagePath"
} else {
    Write-Host "Downloading $CloudImageUrl"
    & curl.exe -L --fail --retry 5 --retry-delay 5 --show-error --output $CloudImagePath $CloudImageUrl
    if ($LASTEXITCODE -ne 0) {
        throw "curl.exe failed with exit code $LASTEXITCODE"
    }
}

foreach ($spec in $VmSpecs) {
    $vmName = $spec.Name
    $vm = Get-VM -Name $vmName -ErrorAction SilentlyContinue
    if (-not $vm) {
        throw "VM $vmName does not exist. Run create_gatherlink_vms.ps1 first."
    }

    if ($vm.State -ne "Off") {
        Write-Host "stopping VM $vmName"
        Stop-VM -Name $vmName -Force
    }

    $vmPath = Join-Path $VmRoot $vmName
    $diskDirectory = Join-Path $vmPath "Virtual Hard Disks"
    $seedDirectory = Join-Path $vmPath "seed"
    $seedSourceDirectory = Join-Path $vmPath "seed-src"
    $diskPath = Join-Path $diskDirectory "$vmName-cloud.vhdx"
    $workingQcow2Path = Join-Path $diskDirectory "$vmName-working.qcow2"
    $seedIsoPath = Join-Path $seedDirectory "$vmName-seed.iso"

    New-Item -ItemType Directory -Force -Path $diskDirectory, $seedDirectory, $seedSourceDirectory | Out-Null

    $userDataPath = Join-Path $seedSourceDirectory "user-data"
    $metaDataPath = Join-Path $seedSourceDirectory "meta-data"
    $networkConfigPath = Join-Path $seedSourceDirectory "network-config"

    $authorizedKeysYaml = ($publicKeys | ForEach-Object { "      - $_" }) -join "`n"

    Write-Utf8NoBom -Path $userDataPath -Content @"
#cloud-config
preserve_hostname: false
hostname: $($spec.Hostname)
manage_etc_hosts: true
users:
  - default
  - name: gatherlink
    gecos: Gatherlink Lab
    groups: [sudo]
    shell: /bin/bash
    sudo: ["ALL=(ALL) NOPASSWD:ALL"]
    lock_passwd: true
    ssh_authorized_keys:
$authorizedKeysYaml
ssh_pwauth: false
package_update: true
packages:
  - curl
  - git
  - iproute2
  - iperf3
  - nftables
  - openssh-server
  - python3
  - python3-venv
  - rsync
  - sudo
  - tcpdump
  - vim
runcmd:
  - [ systemctl, enable, --now, ssh ]
  - [ sh, -lc, "mkdir -p /home/gatherlink/codex /home/gatherlink/src && chown -R gatherlink:gatherlink /home/gatherlink" ]
final_message: "Gatherlink Hyper-V lab VM is ready after `$UPTIME seconds"
"@

    Write-Utf8NoBom -Path $metaDataPath -Content @"
instance-id: $($spec.InstanceId)
local-hostname: $($spec.Hostname)
"@

    $internetMac = Format-MacForCloudInit "$($spec.MacBase)01"
    $pathAMac = Format-MacForCloudInit "$($spec.MacBase)A1"
    $pathBMac = Format-MacForCloudInit "$($spec.MacBase)B1"
    $pathCMac = Format-MacForCloudInit "$($spec.MacBase)C1"

    Write-Utf8NoBom -Path $networkConfigPath -Content @"
version: 2
ethernets:
  internet:
    match:
      macaddress: "$internetMac"
    set-name: internet
    dhcp4: true
    dhcp6: true
  path-a:
    match:
      macaddress: "$pathAMac"
    set-name: path-a
    addresses: [$($spec.PathA)]
    dhcp4: false
    dhcp6: false
  path-b:
    match:
      macaddress: "$pathBMac"
    set-name: path-b
    addresses: [$($spec.PathB)]
    dhcp4: false
    dhcp6: false
  path-c:
    match:
      macaddress: "$pathCMac"
    set-name: path-c
    addresses: [$($spec.PathC)]
    dhcp4: false
    dhcp6: false
"@

    $cloudImageWsl = Convert-ToWslPath $CloudImagePath
    $diskPathWsl = Convert-ToWslPath $diskPath
    $workingQcow2PathWsl = Convert-ToWslPath $workingQcow2Path
    $seedIsoPathWsl = Convert-ToWslPath $seedIsoPath
    $userDataWsl = Convert-ToWslPath $userDataPath
    $metaDataWsl = Convert-ToWslPath $metaDataPath
    $networkConfigWsl = Convert-ToWslPath $networkConfigPath

    Write-Host "creating cloud disk $diskPath"
    Remove-Item -LiteralPath $diskPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $workingQcow2Path -Force -ErrorAction SilentlyContinue
    Invoke-Wsl "cp '$cloudImageWsl' '$workingQcow2PathWsl' && qemu-img resize '$workingQcow2PathWsl' $DiskSize && qemu-img convert -f qcow2 -O vhdx -o subformat=dynamic '$workingQcow2PathWsl' '$diskPathWsl' && rm -f '$workingQcow2PathWsl'"

    Write-Host "creating NoCloud seed ISO $seedIsoPath"
    Remove-Item -LiteralPath $seedIsoPath -Force -ErrorAction SilentlyContinue
    Invoke-Wsl "cloud-localds --network-config='$networkConfigWsl' '$seedIsoPathWsl' '$userDataWsl' '$metaDataWsl'"

    Get-VMHardDiskDrive -VMName $vmName | Remove-VMHardDiskDrive
    Add-VMHardDiskDrive -VMName $vmName -Path $diskPath | Out-Null

    Get-VMDvdDrive -VMName $vmName | Remove-VMDvdDrive
    Add-VMDvdDrive -VMName $vmName -Path $seedIsoPath | Out-Null

    $hardDisk = Get-VMHardDiskDrive -VMName $vmName
    Set-VMFirmware -VMName $vmName -EnableSecureBoot Off -FirstBootDevice $hardDisk

    Write-Host "prepared $vmName with Debian cloud image and NoCloud seed"
}

Get-VM -Name gatherlink-vm-a,gatherlink-vm-b |
    Select-Object Name, State, Generation, ProcessorCount, MemoryStartup, Path |
    Format-Table -AutoSize
