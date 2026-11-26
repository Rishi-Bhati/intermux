# InterMux installer for Windows.
# Installs:
#   - InterMux.exe (unprivileged GUI/CLI) into %LOCALAPPDATA%\Programs\InterMux
#   - intermux-net.exe + wintun.dll (elevated engine) into admin-only %ProgramFiles%\InterMux (1 UAC prompt)

param(
    [string]$Version = $env:INTERMUX_VERSION,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Repo         = 'Rishi-Bhati/intermux'
$AssetName    = 'InterMux.exe'
$EngineAsset  = 'intermux-net.exe'
$WintunAsset  = 'wintun.dll'
$InstallDir   = Join-Path $env:LOCALAPPDATA 'Programs\InterMux'
$ExePath      = Join-Path $InstallDir $AssetName
$EngineDir    = Join-Path $env:ProgramFiles 'InterMux'
$Shortcut     = Join-Path ([Environment]::GetFolderPath('Programs')) 'InterMux.lnk'
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
    if (Test-Path -LiteralPath $EngineDir) {
        $psCmd = "Remove-Item -LiteralPath '$($EngineDir.Replace('''',''''''))' -Recurse -Force -ErrorAction SilentlyContinue"
        Start-Process -FilePath 'powershell.exe' -Verb RunAs -Wait -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-Command',$psCmd
    }
    Write-Ok 'InterMux has been removed.'
}

if (-not [Environment]::Is64BitOperatingSystem) {
    throw 'InterMux for Windows needs 64-bit Windows 10 or 11.'
}

if ($Uninstall) {
    Remove-InterMux
    return
}

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
    if ($want) { throw "No release $Version with $AssetName was found." }
    throw "No release with $AssetName was found."
}

function Save-VerifiedAsset($assetObj, $destPath) {
    Invoke-WebRequest -UseBasicParsing -Headers @{ 'User-Agent' = 'intermux-installer' } -Uri $assetObj.browser_download_url -OutFile $destPath
    $digest = "$($assetObj.digest)"
    if ($digest -like 'sha256:*') {
        $expected = $digest.Substring(7).ToLowerInvariant()
        $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $destPath).Hash.ToLowerInvariant()
        if ($expected -ne $actual) { throw "Checksum mismatch for $($assetObj.name)" }
    }
}

$tmpDir = Join-Path ([IO.Path]::GetTempPath()) ("intermux-inst-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null
try {
    $guiAsset = $release.assets | Where-Object { $_.name -eq $AssetName } | Select-Object -First 1
    $tmpGui = Join-Path $tmpDir $AssetName
    Write-Step "Downloading $AssetName ($($release.tag_name))"
    Save-VerifiedAsset $guiAsset $tmpGui

    Stop-InterMux
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    Move-Item -LiteralPath $tmpGui -Destination $ExePath -Force
    Unblock-File -LiteralPath $ExePath -ErrorAction SilentlyContinue

    $engAsset = $release.assets | Where-Object { $_.name -eq $EngineAsset } | Select-Object -First 1
    $winAsset = $release.assets | Where-Object { $_.name -eq $WintunAsset } | Select-Object -First 1
    if ($engAsset -and $winAsset) {
        $tmpEng = Join-Path $tmpDir $EngineAsset
        $tmpWin = Join-Path $tmpDir $WintunAsset
        Write-Step "Downloading $EngineAsset and $WintunAsset"
        Save-VerifiedAsset $engAsset $tmpEng
        Save-VerifiedAsset $winAsset $tmpWin
        Write-Step "Installing elevated engine to admin-only $EngineDir (UAC prompt)"
        $instCmd = "New-Item -ItemType Directory -Force -Path '$($EngineDir.Replace('''',''''''))' | Out-Null; " +
                   "Copy-Item -LiteralPath '$($tmpEng.Replace('''',''''''))' -Destination '$((Join-Path $EngineDir $EngineAsset).Replace('''',''''''))' -Force; " +
                   "Copy-Item -LiteralPath '$($tmpWin.Replace('''',''''''))' -Destination '$((Join-Path $EngineDir $WintunAsset).Replace('''',''''''))' -Force"
        Start-Process -FilePath 'powershell.exe' -Verb RunAs -Wait -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-Command',$instCmd
    }
} finally {
    Remove-Item -LiteralPath $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
}

$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($Shortcut)
$lnk.TargetPath = $ExePath
$lnk.WorkingDirectory = $InstallDir
$lnk.IconLocation = "$ExePath,0"
$lnk.Description = 'Bind applications to specific network interfaces'
$lnk.Save()

Write-Ok "InterMux $($release.tag_name.TrimStart('v')) is installed."
