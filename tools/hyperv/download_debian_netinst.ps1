param(
    [string] $DestinationDirectory = "D:\media\debian"
)

$ErrorActionPreference = "Stop"

$indexUrl = "https://cdimage.debian.org/debian-cd/current/amd64/iso-cd/"

New-Item -ItemType Directory -Force -Path $DestinationDirectory | Out-Null

Write-Host "Reading Debian netinst index from $indexUrl"
$index = Invoke-WebRequest -UseBasicParsing -Uri $indexUrl
$isoName = ([regex]::Matches($index.Content, 'debian-[0-9.]+-amd64-netinst\.iso') |
    ForEach-Object { $_.Value } |
    Sort-Object -Unique |
    Select-Object -First 1)

if (-not $isoName) {
    throw "Could not find a Debian amd64 netinst ISO link at $indexUrl"
}

$isoUrl = "$indexUrl$isoName"
$isoPath = Join-Path $DestinationDirectory $isoName

$expectedBytes = 0
try {
    $head = Invoke-WebRequest -UseBasicParsing -Method Head -Uri $isoUrl
    $expectedBytes = [int64]$head.Headers["Content-Length"]
} catch {
    Write-Warning "Could not read expected ISO size before download: $($_.Exception.Message)"
}

if ((Test-Path -LiteralPath $isoPath) -and ($expectedBytes -gt 0)) {
    $existing = Get-Item -LiteralPath $isoPath
    if ($existing.Length -eq $expectedBytes) {
        Write-Host "reused existing complete ISO $isoPath"
        Get-Item -LiteralPath $isoPath | Select-Object FullName, Length, LastWriteTime | Format-List
        exit 0
    }

    Write-Warning "removing incomplete ISO $isoPath ($($existing.Length) bytes, expected $expectedBytes)"
    Remove-Item -LiteralPath $isoPath -Force
}

Write-Host "Downloading $isoUrl"
& curl.exe -L --fail --retry 5 --retry-delay 5 --show-error --output $isoPath $isoUrl
if ($LASTEXITCODE -ne 0) {
    throw "curl.exe failed with exit code $LASTEXITCODE"
}

if ($expectedBytes -gt 0) {
    $downloaded = Get-Item -LiteralPath $isoPath
    if ($downloaded.Length -ne $expectedBytes) {
        throw "Downloaded ISO has $($downloaded.Length) bytes, expected $expectedBytes"
    }
}

Write-Host "downloaded $isoPath"

Get-Item -LiteralPath $isoPath | Select-Object FullName, Length, LastWriteTime | Format-List
