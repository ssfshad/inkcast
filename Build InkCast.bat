@echo off
setlocal
title InkCast Builder
cd /d "%~dp0"

rem Optional arguments: setup or portable
set "CHOICE="
set "INTERACTIVE=1"
if not "%~1"=="" set "INTERACTIVE=0"
if /i "%~1"=="1" set "CHOICE=1"
if /i "%~1"=="setup" set "CHOICE=1"
if /i "%~1"=="installer" set "CHOICE=1"
if /i "%~1"=="2" set "CHOICE=2"
if /i "%~1"=="portable" set "CHOICE=2"
if /i "%~1"=="zip" set "CHOICE=2"

if not defined CHOICE if "%INTERACTIVE%"=="1" (
  echo.
  echo   ============================================
  echo     InkCast build - what do you want to make?
  echo   ============================================
  echo.
  echo     1 = Setup installer   -^>  installer\InkCastSetup.exe
  echo     2 = Portable version  -^>  portable\InkCast-Portable.zip
  echo.
  choice /c 12 /n /m "  Choose 1 or 2: "
  set "CHOICE=%errorlevel%"
)
if not defined CHOICE (
  echo   Unknown build option: %~1
  echo   Use: setup or portable
  goto fail
)

rem ---------- check python ----------
where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo   Python is not installed.
  echo   Install it from https://www.python.org/downloads/  and tick "Add python.exe to PATH".
  goto fail
)

echo.
echo   Installing build and app dependencies...
python -m pip install --quiet --disable-pip-version-check --upgrade -r requirements.txt pyinstaller
if errorlevel 1 (
  echo   Could not install PyInstaller. Check your internet connection.
  goto fail
)

echo.
echo   Building InkCast.exe ...  (this can take a few minutes)
echo.
python -m PyInstaller --noconfirm --clean ^
  --onefile --windowed ^
  --name "InkCast" ^
  --icon inkcast.ico ^
  --hidden-import PIL.Image ^
  inkcast.py
if errorlevel 1 (
  echo.
  echo   Build failed. See the messages above.
  goto fail
)

if "%CHOICE%"=="1" call :do_setup
if "%CHOICE%"=="2" call :do_portable

if errorlevel 1 goto fail

echo.
echo   ============================================
echo     Done.
if "%CHOICE%"=="1" echo       Setup:    installer\InkCastSetup.exe
if "%CHOICE%"=="2" echo       Portable: portable\InkCast-Portable.zip
echo   ============================================
echo.
pause
exit /b 0


rem ============================================================
:do_setup
rem ---------- find the Inno Setup compiler ----------
set "ISCC="
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe"      set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not defined ISCC for /f "delims=" %%I in ('where ISCC 2^>nul') do if not defined ISCC set "ISCC=%%I"

if not defined ISCC (
  echo.
  echo   Inno Setup 6 is not installed - cannot build the installer.
  echo   Install it with:  winget install JRSoftware.InnoSetup
  exit /b 1
)

echo.
echo   Building installer with Inno Setup...
"%ISCC%" installer.iss
if errorlevel 1 exit /b 1
exit /b 0


rem ============================================================
:do_portable
echo.
echo   Assembling portable package...
if exist "portable\InkCast" rmdir /s /q "portable\InkCast"
if not exist "portable" mkdir "portable"
mkdir "portable\InkCast"
mkdir "portable\InkCast\camera"
copy /y "dist\InkCast.exe" "portable\InkCast\InkCast.exe" >nul
if exist "camera\InkCast Camera.bat" copy /y "camera\InkCast Camera.bat" "portable\InkCast\camera\InkCast Camera.bat" >nul
if errorlevel 1 exit /b 1
if exist "camera\get-scrcpy.ps1" copy /y "camera\get-scrcpy.ps1" "portable\InkCast\camera\get-scrcpy.ps1" >nul
if errorlevel 1 exit /b 1

echo   Zipping portable\InkCast-Portable.zip ...
if exist "portable\InkCast-Portable.zip" del "portable\InkCast-Portable.zip"
powershell -NoProfile -Command "Compress-Archive -Path 'portable\InkCast' -DestinationPath 'portable\InkCast-Portable.zip' -Force"
if errorlevel 1 (
  echo   Zip failed - the portable folder is still ready at portable\InkCast\
  exit /b 1
)
exit /b 0


rem ============================================================
:fail
echo.
pause
exit /b 1
