@echo off
title Transcript Tool
cd /d "%~dp0"

rem ==== One-click launcher (logic lives in scripts\launch.py) ====
rem Finds Python; if missing, scripts\setup_python.ps1 auto-downloads and
rem installs it (per-user, silent, no admin) then prints its path on stdout.
rem Chinese status text lives in the .ps1 (bat keeps pure ASCII on purpose).

set "PYTHON_EXE="
for /f "usebackq delims=" %%i in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_python.ps1"`) do set "PYTHON_EXE=%%i"

if not defined PYTHON_EXE (
    echo [!] Python auto-setup failed. Read the messages above / check network.
    echo     Manual fix: install Python 3.10+ and enable "Add python.exe to PATH".
    echo     https://www.python.org/downloads/
    pause
    exit /b 1
)

"%PYTHON_EXE%" scripts\launch.py
