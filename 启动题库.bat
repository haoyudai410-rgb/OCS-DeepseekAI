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

rem 1) local override: put the full path of python.exe into python-path.txt (same folder)
if exist "%~dp0python-path.txt" for /f "usebackq delims=" %%P in ("%~dp0python-path.txt") do if not defined PYCMD if exist "%%~fP" set "PYCMD="%%~fP""

rem 2) commands on PATH
if not defined PYCMD for %%C in ("python" "py -3" "python3") do if not defined PYCMD %%~C -c "import sys" >nul 2>&1 && set "PYCMD=%%~C"

rem 3) usual install locations
if not defined PYCMD for /d %%D in ("%LocalAppData%\Programs\Python\Python*" "C:\Python*" "%ProgramFiles%\Python*" "%ProgramFiles(x86)%\Python*") do if not defined PYCMD if exist "%%~fD\python.exe" set "PYCMD="%%~fD\python.exe""

if not defined PYCMD (
  echo [ERROR] No usable Python 3.8+ found.
  echo         Install Python from https://www.python.org/downloads/  ^(tick "Add python.exe to PATH"^),
  echo         or write the full path of your python.exe into python-path.txt next to this file.
  echo         Example: C:\Python312\python.exe
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
