@echo off
title InkCast - Cloudflare Tunnel
cd /d "%~dp0"

echo ================================================================
echo               InkCast - Cloudflare Remote Tunnel
echo ================================================================
echo.
echo Make sure InkCast is running first!
echo (Double-click "Start InkCast.bat" before running this)
echo.

set "CF_CMD="

if exist "%~dp0cloudflared.exe" (
  set "CF_CMD=%~dp0cloudflared.exe"
) else (
  where cloudflared >nul 2>nul
  if not errorlevel 1 (
    set "CF_CMD=cloudflared"
  ) else if exist "%ProgramFiles(x86)%\cloudflared\cloudflared.exe" (
    set "CF_CMD=%ProgramFiles(x86)%\cloudflared\cloudflared.exe"
  ) else if exist "%ProgramFiles%\cloudflared\cloudflared.exe" (
    set "CF_CMD=%ProgramFiles%\cloudflared\cloudflared.exe"
  )
)

if "%CF_CMD%"=="" (
  echo cloudflared not found on this PC.
  echo Downloading cloudflared for first-time use (about 20MB)...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; (New-Object Net.WebClient).DownloadFile('https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe', '%~dp0cloudflared.exe')"
  if exist "%~dp0cloudflared.exe" (
    set "CF_CMD=%~dp0cloudflared.exe"
    echo Download complete!
    echo.
  ) else (
    echo Could not download cloudflared automatically.
    echo Please install it from: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
    pause
    exit /b
  )
)

echo Starting secure public tunnel to InkCast (port 8765)...
echo.
echo Look for the public https://....trycloudflare.com link below.
echo Open that link on your phone or tablet from ANY network!
echo.
echo Press Ctrl+C to stop the tunnel.
echo ================================================================
echo.

"%CF_CMD%" tunnel --url http://localhost:8765
pause
