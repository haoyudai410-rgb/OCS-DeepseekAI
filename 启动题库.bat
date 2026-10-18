@echo off
chcp 65001 >nul
title OCS AI Bank Server
cd /d "%~dp0"

netstat -ano | findstr /C:":8899 " | findstr /I "LISTENING" >nul 2>&1
if %errorlevel%==0 (
  echo [i] Backend is ALREADY running on port 8899. Nothing to do.
  echo     Close that other window first if you want to restart it.
  timeout /t 6 >nul
  exit /b 0
)

set "PYCMD="
for %%C in ("python" "py -3" "python3") do (
  if not defined PYCMD %%~C -c "import sys" >nul 2>&1 && set "PYCMD=%%~C"
)

if not defined PYCMD (
  echo [ERROR] No usable Python found. Install Python 3.8+ and add it to PATH,
  echo         or edit this file and set PYCMD to the full path of python.exe.
  pause
  exit /b 1
)

set PYTHONIOENCODING=utf-8
echo Starting backend with: %PYCMD%
echo.
%PYCMD% server.py
echo.
echo Server stopped. If an error is shown above, that is the reason.
pause
