# Install Vesper on Windows 10/11.
#
#   irm https://raw.githubusercontent.com/Kernel-Hunter/vesper/main/install.ps1 | iex
#
# By default this installs the `vesper` terminal agent: uv (https://docs.astral.sh/uv/) if
# it is missing, then Vesper from the latest release as an isolated uv tool. uv brings its
# own Python. Nothing needs admin rights.
#
# Desktop app too:   $env:VESPER_APP = '1'; irm https://raw.githubusercontent.com/Kernel-Hunter/vesper/main/install.ps1 | iex
# Desktop app only:  $env:VESPER_APP = 'only'; irm ... | iex
#
# A file downloaded by PowerShell carries no "downloaded from the internet" mark
# (Mark-of-the-Web), so SmartScreen does not stop the app installer, and the installer
# is checked against SHA256SUMS.txt from the same release first.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$repo = 'Kernel-Hunter/vesper'
$api = "https://api.github.com/repos/$repo/releases/latest"
$wantApp = $env:VESPER_APP -in @('1', 'only', 'true', 'yes')
$wantCli = $env:VESPER_APP -ne 'only'
function Say($text) { Write-Host "==> $text" -ForegroundColor Magenta }

# Download a large file. With curl.exe (Windows 10 1803 and later), a transfer slower than
# 10 KB/s for a minute is dropped and resumed from where it stopped; either way a failed
# download is tried five times before giving up, so a stalled connection never hangs here.
function Get-File($url, $out) {
  if (Test-Path $out) { Remove-Item $out -Force }
  $curl = Get-Command curl.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1
  for ($try = 1; $try -le 5; $try++) {
    if ($curl) {
      $ErrorActionPreference = 'Continue'
      & $curl -fsSL --connect-timeout 20 --speed-limit 10240 --speed-time 60 -C - -o $out $url 2>$null
      $ok = $LASTEXITCODE -eq 0
      $ErrorActionPreference = 'Stop'
    } else {
      try { Invoke-WebRequest -Uri $url -OutFile $out -UseBasicParsing -TimeoutSec 120; $ok = $true } catch { $ok = $false }
    }
    if ($ok) { return }
    if ($try -lt 5) { Say "The download stalled; trying again ($($try + 1) of 5)..."; Start-Sleep -Seconds 3 }
  }
  throw 'The download kept stalling. Check your connection and run this again.'
}

function Get-Release {
  Invoke-WebRequest -Uri $api -UseBasicParsing -TimeoutSec 60 -Headers @{ Accept = 'application/vnd.github+json' } |
    Select-Object -ExpandProperty Content | ConvertFrom-Json
}

function Install-Cli {
  $uv = Get-Command uv -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1
  if (-not $uv) {
    $candidate = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
    if (Test-Path $candidate) { $uv = $candidate }
  }
  if (-not $uv) {
    Say 'Installing uv (Python package manager from Astral)...'
    $ErrorActionPreference = 'Continue'
    powershell -NoProfile -ExecutionPolicy Bypass -Command 'irm https://astral.sh/uv/install.ps1 | iex' 2>&1 | Out-Null
    $ErrorActionPreference = 'Stop'
    $uv = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
    if (-not (Test-Path $uv)) { throw 'uv did not install. See https://docs.astral.sh/uv/ and run this again.' }
  }
  $ref = 'main'
  try { $ref = (Get-Release).tag_name } catch { }
  Say "Installing the vesper command ($ref)..."
  # uv reports progress on stderr; Windows PowerShell 5.1 turns that into errors under 'Stop'.
  $ErrorActionPreference = 'Continue'
  $log = & $uv tool install --force --python 3.12 "vesper-agent @ https://github.com/$repo/archive/$ref.tar.gz" 2>&1
  $ErrorActionPreference = 'Stop'
  if ($LASTEXITCODE -ne 0) { $log | Write-Host; throw 'Installing vesper failed (output above).' }
  $ErrorActionPreference = 'Continue'
  & $uv tool update-shell 2>$null | Out-Null
  $ErrorActionPreference = 'Stop'
  Say 'Installed the vesper command.'
}

function Install-App {
  $release = Get-Release
  $asset = $release.assets | Where-Object { $_.name -like 'Vesper-*-win-*.exe' } | Select-Object -First 1
  if (-not $asset) { throw 'Could not find the Windows installer in the latest release.' }

  $setup = Join-Path $env:TEMP $asset.name
  Write-Host "==> Downloading Vesper $($release.tag_name)..." -ForegroundColor Magenta
  Get-File $asset.browser_download_url $setup

  # Check the installer against SHA256SUMS.txt from the same release.
  $sumsAsset = $release.assets | Where-Object { $_.name -eq 'SHA256SUMS.txt' } | Select-Object -First 1
  if (-not $sumsAsset) { Remove-Item $setup -Force; throw 'The release has no SHA256SUMS.txt; refusing to install an unchecked file.' }
  $sums = (Invoke-WebRequest -Uri $sumsAsset.browser_download_url -UseBasicParsing -TimeoutSec 60).Content
  if ($sums -is [byte[]]) { $sums = [System.Text.Encoding]::UTF8.GetString($sums) }
  $line = ($sums -split "`n") | Where-Object { $_ -match ('\s\*?' + [regex]::Escape($asset.name) + '\s*$') } | Select-Object -First 1
  $want = if ($line) { ($line -split '\s+')[0].ToLower() } else { '' }
  $got = (Get-FileHash -Algorithm SHA256 -Path $setup).Hash.ToLower()
  if (-not $want -or $want -ne $got) { Remove-Item $setup -Force; throw "Checksum mismatch for $($asset.name). Nothing was installed." }
  Write-Host '==> Checksum OK' -ForegroundColor Magenta

  Write-Host '==> Installing...' -ForegroundColor Magenta
  # /S = NSIS silent install, per user, into %LOCALAPPDATA%\Programs\Vesper.
  $p = Start-Process -FilePath $setup -ArgumentList '/S' -Wait -PassThru
  Remove-Item $setup -Force
  if ($p.ExitCode -ne 0) { throw "The installer failed (exit code $($p.ExitCode))." }

  $exe = Join-Path $env:LOCALAPPDATA 'Programs\Vesper\Vesper.exe'
  Write-Host "==> Desktop app installed: $exe (also in the Start menu)." -ForegroundColor Magenta
}

if ($wantCli) { Install-Cli }
if ($wantApp) { Install-App }
Write-Host ''
if ($wantCli) {
  Say 'Done. Open a new terminal, go to any folder and run: vesper'
  if (-not $wantApp) { Say "Desktop app: set `$env:VESPER_APP = '1' and run this again, or type /app inside vesper." }
} else {
  Say 'Done.'
}
