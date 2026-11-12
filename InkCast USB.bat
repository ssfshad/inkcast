@echo off
setlocal
title InkCast USB
cd /d "%~dp0"

rem Connects an Android tablet to InkCast over a USB cable instead of WiFi.
rem "adb reverse" makes http://localhost:PORT on the tablet reach InkCast on this PC.
set "PORT=8765"
if defined INKCAST_PORT set "PORT=%INKCAST_PORT%"

rem ---- find adb: it ships with scrcpy in camera\scrcpy\, or anywhere on PATH
set "ADB="
if exist "%~dp0camera\scrcpy\adb.exe" set "ADB=%~dp0camera\scrcpy\adb.exe"
if not defined ADB for /f "delims=" %%P in ('where adb 2^>nul') do if not defined ADB set "ADB=%%P"
if defined ADB goto ready

echo.
echo   InkCast USB uses adb, Android's official USB tool, to connect your
echo   tablet over the cable. Nothing gets installed on your tablet.
echo.
echo   adb is not on this PC yet. It comes with scrcpy, a free open-source
echo   tool that InkCast can download from its official GitHub release now.
echo.
choice /c YN /m "  Download it now"
if errorlevel 2 exit /b
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0camera\get-scrcpy.ps1"
if errorlevel 1 goto download_failed
set "ADB=%~dp0camera\scrcpy\adb.exe"
if not exist "%ADB%" goto download_failed

:ready
echo.
echo   InkCast USB - Android tablet over a USB cable (lower, steadier lag)
echo.
echo   Keep InkCast running on this PC, and keep this window open.
echo.
echo   First time only, on the tablet:
echo     Settings - About tablet - tap Build number 7 times, then
echo     Developer options - turn on USB debugging.
echo.
"%ADB%" start-server >nul 2>&1
set "FIRST=1"

:wait
echo   Waiting for the tablet. Plug it in with a USB cable.
echo   If the tablet asks "Allow USB debugging?", tap Allow (tick Always allow).
"%ADB%" wait-for-usb-device
"%ADB%" -d reverse tcp:%PORT% tcp:%PORT% >nul
if errorlevel 1 (
  echo   Couldn't connect. If a phone is also plugged in, unplug it. Retrying...
  timeout /t 3 /nobreak >nul
  goto wait
)
if defined FIRST "%ADB%" -d shell am start -a android.intent.action.VIEW -d "http://localhost:%PORT%" >nul 2>&1
set "FIRST="
echo.
echo   Connected over USB. On the tablet, InkCast is at  http://localhost:%PORT%
echo   (it opened in the browser for you). Leave this window open while you draw.
echo.
"%ADB%" -d wait-for-usb-disconnect
echo   The cable was unplugged. Plug it back in to reconnect.
echo.
goto wait

:download_failed
echo.
echo   The download did not work. You can get scrcpy yourself from
echo   https://github.com/Genymobile/scrcpy/releases - unzip it into
echo   the camera folder as "scrcpy", then run this file again.
pause
exit /b
