@echo off
setlocal
title O-Cleaner
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

echo.
echo   ============================================
echo    O-Cleaner  -  Disk Cleanup WebUI
echo   ============================================
echo.

where python >nul 2>nul
if errorlevel 1 goto nopython

echo   Starting... your browser will open automatically.
echo.
echo   KEEP THIS WINDOW OPEN. Closing it stops the service.
echo.
python "%~dp0app.py" %*
echo.
echo   ----------------------------------------------------------
echo   Service stopped. You may close this window now.
echo   ----------------------------------------------------------
echo.
pause
goto :eof

:nopython
echo   [ERROR] python was not found in PATH.
echo.
echo   Please install Python 3 from https://www.python.org/downloads/
echo   and make sure "python" is available in PATH.
echo.
echo   Note: O-Cleaner needs no pip packages. Python 3.8+ is enough.
echo.
pause
