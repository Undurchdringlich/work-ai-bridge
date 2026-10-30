@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Work-AI Bridge - Test Suite

echo ============================================================================
echo                 Work-AI Bridge: Running Automated Tests
echo ============================================================================
echo.

python -m unittest discover -s tests -p "test_*.py" -v %*
if %ERRORLEVEL% equ 0 goto :success

py -3 -m unittest discover -s tests -p "test_*.py" -v %*
if %ERRORLEVEL% equ 0 goto :success

echo.
echo ============================================================================
echo [NOTICE] Tests finished with errors, or Python could not be executed.
echo ============================================================================
echo.
pause
exit /b 1

:success
echo.
echo ============================================================================
echo [SUCCESS] All tests completed successfully!
echo ============================================================================
echo.
pause
exit /b 0
