@echo off
:: BatchGotAdmin
:-------------------------------------
REM --> Check for permissions
>nul 2>&1 "%SYSTEMROOT%\system32\cacls.exe" "%SYSTEMROOT%\system32\config\system"

REM --> If error flag set, we do not have admin.
if '%errorlevel%' NEQ '0' (
    echo Requesting administrative privileges...
    goto UACPrompt
) else ( goto gotAdmin )

:UACPrompt
    echo Set UAC = CreateObject^("Shell.Application"^) > "%temp%\getadmin.vbs"
    set params=%*
    echo UAC.ShellExecute "cmd.exe", "/c ""%~s0"" %params%", "", "runas", 1 >> "%temp%\getadmin.vbs"

    "%temp%\getadmin.vbs"
    del "%temp%\getadmin.vbs"
    exit /B

:gotAdmin
    pushd "%CD%"
    CD /D "%~dp0"
:--------------------------------------

title InkCast Firewall Setup
echo ====================================================
echo   Configuring Windows Firewall for InkCast
echo ====================================================
echo.

echo 1. Removing any existing blocking rules...
powershell -NoProfile -Command "Get-NetFirewallRule | Where-Object { $_.DisplayName -match 'inkcast' -and $_.Action -eq 'Block' } | Remove-NetFirewallRule"

echo 2. Allowing TCP Port 8765 for InkCast...
powershell -NoProfile -Command "Remove-NetFirewallRule -DisplayName 'InkCast Port 8765' -ErrorAction SilentlyContinue; New-NetFirewallRule -DisplayName 'InkCast Port 8765' -Direction Inbound -LocalPort 8765 -Protocol TCP -Action Allow -Profile Any"

echo 3. Allowing InkCast executable...
powershell -NoProfile -Command "Remove-NetFirewallRule -DisplayName 'InkCast App' -ErrorAction SilentlyContinue; New-NetFirewallRule -DisplayName 'InkCast App' -Direction Inbound -Program '%LOCALAPPDATA%\Programs\InkCast\InkCast.exe' -Action Allow -Profile Any"
powershell -NoProfile -Command "New-NetFirewallRule -DisplayName 'InkCast App (Dist)' -Direction Inbound -Program '%~dp0dist\InkCast.exe' -Action Allow -Profile Any -ErrorAction SilentlyContinue"

echo 4. Setting Ethernet network to Private (allows local device sharing)...
powershell -NoProfile -Command "Set-NetConnectionProfile -InterfaceAlias 'Ethernet' -NetworkCategory Private -ErrorAction SilentlyContinue"

echo.
echo ====================================================
echo   Done! Local access (192.168.1.2:8765) is now unblocked.
echo ====================================================
echo.
pause

