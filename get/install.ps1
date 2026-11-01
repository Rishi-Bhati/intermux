# InterMux installer for Windows (per user, no admin needed to install).
#
#   irm https://raw.githubusercontent.com/Rishi-Bhati/intermux/main/get/install.ps1 | iex
#
# A specific release, or uninstall:
#
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/Rishi-Bhati/intermux/main/get/install.ps1))) -Version 2.0
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/Rishi-Bhati/intermux/main/get/install.ps1))) -Uninstall
#
# It downloads InterMux.exe from the newest GitHub release that has one, checks
# its SHA-256 when GitHub publishes a digest for it, puts it in
# %LOCALAPPDATA%\Programs\InterMux and adds a Start menu shortcut and an
# "Installed apps" entry. InterMux itself asks for administrator rights (UAC)
# when it starts.

param(
    [string]$Version = $env:INTERMUX_VERSION,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is very slow with the progress bar on

$Repo        = 'Rishi-Bhati/intermux'
$AssetName   = 'InterMux.exe'
$InstallDir  = Join-Path $env:LOCALAPPDATA 'Programs\InterMux'
$ExePath     = Join-Path $InstallDir $AssetName
$Shortcut    = Join-Path ([Environment]::GetFolderPath('Programs')) 'InterMux.lnk'
$UninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\InterMux'

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host " +  $msg" -ForegroundColor Green }

function Stop-InterMux {
    Get-Process -Name 'InterMux' -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -eq $ExePath } |
        Stop-Process -Force -ErrorAction SilentlyContinue
}

function Remove-InterMux {
    Write-Step 'Removing InterMux'
    Stop-InterMux
    Remove-Item -LiteralPath $Shortcut -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $UninstallKey -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Ok 'InterMux has been removed.'
}

if (-not [Environment]::Is64BitOperatingSystem) {
    throw 'InterMux for Windows needs 64-bit Windows 10 or 11.'
}

if ($Uninstall) {
    Remove-InterMux
    return
}

# Windows PowerShell 5.1 does not enable TLS 1.2 by default.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$headers = @{ 'User-Agent' = 'intermux-installer'; 'Accept' = 'application/vnd.github+json' }

Write-Step 'Finding the InterMux release'
$releases = Invoke-RestMethod -Headers $headers -Uri "https://api.github.com/repos/$Repo/releases?per_page=50"
$want = if ($Version) { $Version.TrimStart('v') } else { '' }
$release = $releases |
    Where-Object { -not $_.draft } |
    Where-Object { $_.assets.name -contains $AssetName } |
    Where-Object { -not $want -or $_.tag_name.TrimStart('v') -eq $want } |
    Select-Object -First 1
if (-not $release) {
    if ($want) { throw "No release $Version with $AssetName was found. See https://github.com/$Repo/releases" }
    throw "No release with $AssetName was found. See https://github.com/$Repo/releases"
}
$asset = $release.assets | Where-Object { $_.name -eq $AssetName } | Select-Object -First 1
$tag = $release.tag_name
$displayVersion = $tag.TrimStart('v')

Write-Step "Downloading $AssetName ($tag)"
$tmp = Join-Path ([IO.Path]::GetTempPath()) ("intermux-" + [guid]::NewGuid().ToString('N') + '.exe')
try {
    Invoke-WebRequest -UseBasicParsing -Headers @{ 'User-Agent' = 'intermux-installer' } -Uri $asset.browser_download_url -OutFile $tmp

    $digest = "$($asset.digest)"
    if ($digest -like 'sha256:*') {
        $expected = $digest.Substring(7).ToLowerInvariant()
        $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $tmp).Hash.ToLowerInvariant()
        if ($expected -ne $actual) { throw "Checksum mismatch for $AssetName (expected $expected, got $actual). Not installing." }
        Write-Ok "Checksum verified ($actual)"
    } else {
        Write-Host ' !  GitHub has no checksum for this file; skipping verification.' -ForegroundColor Yellow
    }

    Write-Step "Installing to $InstallDir"
    Stop-InterMux
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    Move-Item -LiteralPath $tmp -Destination $ExePath -Force
} finally {
    Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
}
Unblock-File -LiteralPath $ExePath -ErrorAction SilentlyContinue

# Start menu shortcut
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($Shortcut)
$lnk.TargetPath = $ExePath
$lnk.WorkingDirectory = $InstallDir
$lnk.IconLocation = "$ExePath,0"
$lnk.Description = 'Bind applications to specific network interfaces'
$lnk.Save()

# "Installed apps" entry with a local uninstaller (no download needed to remove it).
# Paths go into single-quoted strings, so double any single quote (e.g. a user named O'Neil).
function Format-Quoted($s) { "'" + ($s -replace "'", "''") + "'" }
$uninstallScript = Join-Path $InstallDir 'uninstall.ps1'
@"
`$ErrorActionPreference = 'SilentlyContinue'
Get-Process -Name 'InterMux' | Where-Object { `$_.Path -eq $(Format-Quoted $ExePath) } | Stop-Process -Force
Remove-Item -LiteralPath $(Format-Quoted $Shortcut) -Force
Remove-Item -LiteralPath $(Format-Quoted $UninstallKey) -Recurse -Force
Set-Location `$env:TEMP
Remove-Item -LiteralPath $(Format-Quoted $InstallDir) -Recurse -Force
Write-Host 'InterMux has been removed.'
"@ | Set-Content -LiteralPath $uninstallScript -Encoding UTF8

New-Item -Path $UninstallKey -Force | Out-Null
$props = @{
    DisplayName     = 'InterMux'
    DisplayVersion  = $displayVersion
    Publisher       = 'Rishi-Bhati'
    DisplayIcon     = $ExePath
    InstallLocation = $InstallDir
    URLInfoAbout    = "https://github.com/$Repo"
    UninstallString = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$uninstallScript`""
}
foreach ($k in $props.Keys) { New-ItemProperty -Path $UninstallKey -Name $k -Value $props[$k] -PropertyType String -Force | Out-Null }
New-ItemProperty -Path $UninstallKey -Name 'NoModify' -Value 1 -PropertyType DWord -Force | Out-Null
New-ItemProperty -Path $UninstallKey -Name 'NoRepair' -Value 1 -PropertyType DWord -Force | Out-Null

Write-Host ''
Write-Ok "InterMux $displayVersion is installed."
Write-Host '    Open it from the Start menu (search "InterMux"). Windows asks for administrator rights when it starts.'
Write-Host '    Update later by running this installer again; uninstall from Settings > Apps.'
