<#
.SYNOPSIS
Download and verify WireGuard Wintun 0.14.1 DLL for InterMux Windows.
#>
[CmdletBinding()]
param(
    [ValidateSet("amd64", "arm64", "x86")]
    [string]$Arch = "",
    [string]$OutDir = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

if (-not $Arch) {
    $rawArch = "$env:PROCESSOR_ARCHITECTURE".ToLowerInvariant()
    switch ($rawArch) {
        "amd64" { $Arch = "amd64" }
        "arm64" { $Arch = "arm64" }
        "x86"   { $Arch = "x86" }
        default { throw "Unsupported Windows architecture: $env:PROCESSOR_ARCHITECTURE" }
    }
}

if (-not $OutDir) {
    $OutDir = Join-Path (Split-Path -Parent $PSScriptRoot) "windows\engine"
}

$WintunUrl = "https://www.wintun.net/builds/wintun-0.14.1.zip"
$ExpectedZipSha256 = "07c256185d6ee3652e09fa55c0b673e2624b565e02c4b9091c79ca7d2f24ef51"
$ExpectedDllSha256 = @{
    "amd64" = "e5da8447dc2c320edc0fc52fa01885c103de8c118481f683643cacc3220dafce"
    "arm64" = "f7ba89005544be9d85231a9e0d5f23b2d15b3311667e2dad0debd344918a3f80"
    "x86"   = "d694fa46ab4cfebcb2632d094c7aa97278eef2f8052438621766d863ae98a931"
}

function Assert-WintunValid([string]$DllPath, [string]$ExpectedHash) {
    $actual = (Get-FileHash -LiteralPath $DllPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $ExpectedHash) {
        throw "wintun.dll SHA-256 mismatch: expected $ExpectedHash, got $actual"
    }
    $sig = Get-AuthenticodeSignature -LiteralPath $DllPath
    if ($sig.Status -ne "Valid") {
        throw "wintun.dll Authenticode signature invalid: $($sig.Status) ($($sig.StatusMessage))"
    }
    if ($sig.SignerCertificate.Subject -notmatch "WireGuard LLC") {
        throw "Unexpected wintun.dll signer: $($sig.SignerCertificate.Subject)"
    }
}

$expectedHash = $ExpectedDllSha256[$Arch]
$destDll = Join-Path $OutDir "wintun.dll"

if ((-not $Force) -and (Test-Path -LiteralPath $destDll)) {
    try {
        Assert-WintunValid -DllPath $destDll -ExpectedHash $expectedHash
        Write-Host "wintun.dll already present and verified ($Arch): $destDll"
        exit 0
    } catch {
        Write-Warning "Existing wintun.dll failed verification ($_); re-downloading."
    }
}

[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$tmpDir = Join-Path ([IO.Path]::GetTempPath()) ("intermux-wintun-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null

try {
    $zipPath = Join-Path $tmpDir "wintun-0.14.1.zip"
    $extractDir = Join-Path $tmpDir "extract"
    Write-Host "Downloading Wintun 0.14.1 from $WintunUrl ..."
    Invoke-WebRequest -Uri $WintunUrl -OutFile $zipPath -UseBasicParsing

    $zipHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($zipHash -ne $ExpectedZipSha256) {
        throw "wintun-0.14.1.zip SHA-256 mismatch: expected $ExpectedZipSha256, got $zipHash"
    }

    Expand-Archive -LiteralPath $zipPath -DestinationPath $extractDir -Force
    $srcDll = [IO.Path]::GetFullPath((Join-Path $extractDir "wintun\bin\$Arch\wintun.dll"))
    $extractRoot = [IO.Path]::GetFullPath($extractDir)
    if (-not $srcDll.StartsWith($extractRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe archive path: $srcDll"
    }
    if (-not (Test-Path -LiteralPath $srcDll)) {
        throw "Missing $Arch DLL in archive: $srcDll"
    }

    Assert-WintunValid -DllPath $srcDll -ExpectedHash $expectedHash
    New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
    Copy-Item -LiteralPath $srcDll -Destination $destDll -Force
    Write-Host "Verified and installed Wintun 0.14.1 ($Arch) to $destDll"
} finally {
    Remove-Item -LiteralPath $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
}
