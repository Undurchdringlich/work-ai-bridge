@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Work-AI Bridge Proxy

set "SCRIPT_PATH=%~dp0scripts\bridge_proxy.py"

python "%SCRIPT_PATH%" %*
if %ERRORLEVEL% equ 0 goto :end

py -3 "%SCRIPT_PATH%" %*
if %ERRORLEVEL% equ 0 goto :end

echo.
echo ============================================================================
echo [ERROR] Could not start bridge_proxy.py.
echo Make sure Python 3.8+ is installed and accessible.
echo ============================================================================
echo.
pause
exit /b 1

:end
pause
