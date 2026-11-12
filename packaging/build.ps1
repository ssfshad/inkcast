# Builds InkCast.exe (no Python needed on the user's PC) and zips it for a GitHub release.
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1
# Output: dist\InkCast\InkCast.exe and dist\InkCast-windows-x64.zip

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

Write-Host '== Making a clean build environment'
# Only InkCast's own packages go in, so nothing else installed on this PC ends up in the .exe
$venv = Join-Path $root 'build\venv'
if (-not (Test-Path (Join-Path $venv 'Scripts\python.exe'))) {
    # Prefer the python.org Python via the py launcher: an Anaconda-based environment loads
    # Anaconda's own Qt DLLs and PySide6 fails to start.
    if (Get-Command py -ErrorAction SilentlyContinue) { py -3 -m venv $venv } else { python -m venv $venv }
    if ($LASTEXITCODE) { throw 'could not create the build environment' }
}
$py = Join-Path $venv 'Scripts\python.exe'
& $py -m pip install --quiet --disable-pip-version-check -r requirements.txt pyinstaller
if ($LASTEXITCODE) { throw 'pip install failed' }
& $py -c 'import PySide6.QtCore'
if ($LASTEXITCODE) {
    throw 'PySide6 does not load in the build environment. Install Python from python.org (not Anaconda), delete build\venv and run this again.'
}

Write-Host '== Drawing the icon'
$ico = Join-Path $root 'build\inkcast.ico'
& $py packaging\make_icon.py $ico
if ($LASTEXITCODE) { throw 'icon failed' }

Write-Host '== Building InkCast.exe'
# A folder build (not --onefile): it starts instantly instead of unpacking ~100 MB on every launch.
& $py -m PyInstaller --noconfirm --clean --windowed --name InkCast --icon $ico `
    --distpath dist --workpath build\pyinstaller --specpath build `
    --exclude-module tkinter `
    inkcast.py
if ($LASTEXITCODE) { throw 'PyInstaller failed' }

Write-Host '== Adding the launchers and docs'
$app = Join-Path $root 'dist\InkCast'
Copy-Item 'InkCast USB.bat', 'README.md', 'LICENSE' $app
New-Item -ItemType Directory -Force (Join-Path $app 'camera') | Out-Null
Copy-Item 'camera\InkCast Camera.bat', 'camera\get-scrcpy.ps1' (Join-Path $app 'camera')

Write-Host '== Zipping'
$zip = Join-Path $root 'dist\InkCast-windows-x64.zip'
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path $app -DestinationPath $zip
$mb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Host "== Done: $zip ($mb MB)"
