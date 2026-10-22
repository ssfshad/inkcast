# Downloads the official scrcpy release for Windows from GitHub (Genymobile/scrcpy)
# into camera\scrcpy\ next to this script. Verifies the SHA-256 checksum that
# GitHub publishes for the file. Nothing is installed system-wide.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$headers = @{ 'User-Agent' = 'InkCast' }
$dest    = Join-Path $PSScriptRoot 'scrcpy'
$work    = Join-Path $PSScriptRoot '.scrcpy-download'

try {
    $release = Invoke-RestMethod 'https://api.github.com/repos/Genymobile/scrcpy/releases/latest' -Headers $headers
    $asset = $release.assets | Where-Object { $_.name -like 'scrcpy-win64-*.zip' } | Select-Object -First 1
    if (-not $asset) { throw 'Could not find the Windows download in the latest scrcpy release.' }

    if (Test-Path $work) { Remove-Item $work -Recurse -Force }
    New-Item -ItemType Directory -Path $work | Out-Null
    $zip = Join-Path $work $asset.name

    Write-Host "  Downloading $($asset.name) from GitHub..."
    Invoke-WebRequest $asset.browser_download_url -OutFile $zip -Headers $headers -UseBasicParsing

    if ($asset.digest -and $asset.digest -like 'sha256:*') {
        $expected = $asset.digest.Substring(7).ToLower()
        $actual   = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
        if ($expected -ne $actual) { throw 'Checksum mismatch: the download may be damaged. Please try again.' }
        Write-Host '  Checksum verified.'
    }

    Expand-Archive $zip -DestinationPath $work -Force
    $folder = Get-ChildItem $work -Directory | Where-Object { Test-Path (Join-Path $_.FullName 'scrcpy.exe') } | Select-Object -First 1
    if (-not $folder) { throw 'scrcpy.exe was not found in the download.' }

    if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
    Move-Item $folder.FullName $dest
    Write-Host "  scrcpy $($release.tag_name) is ready."
    exit 0
}
catch {
    Write-Host "  $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
finally {
    if (Test-Path $work) { Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue }
}
