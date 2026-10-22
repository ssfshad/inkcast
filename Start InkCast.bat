@echo off
title InkCast
cd /d "%~dp0"

where python >/dev/null 2>nul
if errorlevel 1 (
  echo Python is not installed.
  echo Install it from https://www.python.org/downloads/  and tick "Add python.exe to PATH".
  pause
  exit /b
)

echo Checking requirements (first run takes a minute)...
python -m pip install --quiet --disable-pip-version-check PySide6 aiohttp mss pillow
if errorlevel 1 (
  echo Could not install requirements. Check your internet connection and try again.
  pause
  exit /b
)

python inkcast.py
pause
