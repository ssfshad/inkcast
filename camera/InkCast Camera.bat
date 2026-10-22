@echo off
setlocal
title InkCast Camera
cd /d "%~dp0"

rem ---- find scrcpy: camera\scrcpy\, this folder, or anywhere on PATH
set "SCRCPY="
if exist "%~dp0scrcpy\scrcpy.exe" set "SCRCPY=%~dp0scrcpy\scrcpy.exe"
if not defined SCRCPY if exist "%~dp0scrcpy.exe" set "SCRCPY=%~dp0scrcpy.exe"
if not defined SCRCPY for /f "delims=" %%P in ('where scrcpy 2^>nul') do if not defined SCRCPY set "SCRCPY=%%P"
if defined SCRCPY goto ready

echo.
echo   InkCast Camera uses scrcpy, a free open-source tool, to read your
echo   phone camera over USB. Nothing gets installed on your phone.
echo.
echo   scrcpy is not on this PC yet. InkCast can download the official
echo   release from GitHub into this folder now.
echo.
choice /c YN /m "  Download it now"
if errorlevel 2 exit /b
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0get-scrcpy.ps1"
if errorlevel 1 goto download_failed
set "SCRCPY=%~dp0scrcpy\scrcpy.exe"
if not exist "%SCRCPY%" goto download_failed

:ready
rem ---- defaults (your last choices are remembered in camera-settings.bat)
set "MODE=1"
set "ROT_FRONT=270"
set "ROT_BACK=90"
set "ZOOM=1"
if exist "%~dp0camera-settings.bat" call "%~dp0camera-settings.bat"

echo.
echo   InkCast Camera - your Android phone camera over USB, no audio
echo   Press Enter to keep the value shown in [brackets].
echo.
echo   1 = Front camera  - face cam, in a window
echo   2 = Back camera   - document cam, in a window
echo   3 = Back camera, full screen - draw over it with InkCast
set "IN="
set /p "IN=  Choose 1, 2 or 3 [%MODE%]: "
if not "%IN%"=="" set "MODE=%IN%"
if not "%MODE%"=="2" if not "%MODE%"=="3" set "MODE=1"
if "%MODE%"=="1" (set "FACING=front") else (set "FACING=back")
if "%FACING%"=="front" (set "ROT=%ROT_FRONT%") else (set "ROT=%ROT_BACK%")

echo.
echo   Rotation turns the picture clockwise: 0, 90, 180 or 270
echo   Picture lying on its side? Try the other one of 90 / 270.
set "IN="
set /p "IN=  Rotation [%ROT%]: "
if not "%IN%"=="" set "ROT=%IN%"
set "OK="
for %%R in (0 90 180 270) do if "%ROT%"=="%%R" set "OK=1"
if not defined OK set "ROT=0"

echo.
echo   Zoom: 1 = no zoom, or e.g. 1.5, 2, 3
set "IN="
set /p "IN=  Zoom [%ZOOM%]: "
if not "%IN%"=="" set "ZOOM=%IN%"

if "%FACING%"=="front" (set "ROT_FRONT=%ROT%") else (set "ROT_BACK=%ROT%")
>"%~dp0camera-settings.bat" (
  echo set "MODE=%MODE%"
  echo set "ROT_FRONT=%ROT_FRONT%"
  echo set "ROT_BACK=%ROT_BACK%"
  echo set "ZOOM=%ZOOM%"
)

set "EXTRA="
if "%MODE%"=="3" set "EXTRA=--fullscreen"
if not "%ZOOM%"=="1" set "EXTRA=%EXTRA% --camera-zoom=%ZOOM%"

echo.
echo   Plug your phone in with a USB cable. First time only: tap Allow
echo   on the "Allow USB debugging?" message on the phone.
echo.
echo   While it runs:  Alt + Up / Down     zoom in / out
echo                   Alt + Left / Right  rotate
echo                   Alt + F             full screen on / off
echo.
"%SCRCPY%" --video-source=camera --camera-facing=%FACING% --no-audio -m1920 --camera-ar=16:9 --orientation=%ROT% --window-title="Camera" %EXTRA% --pause-on-exit=if-error
exit /b

:download_failed
echo.
echo   The download did not work. You can get scrcpy yourself from
echo   https://github.com/Genymobile/scrcpy/releases - unzip it into
echo   this camera folder as "scrcpy", then run this file again.
pause
exit /b
