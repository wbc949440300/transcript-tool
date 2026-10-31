@echo off
title Transcript Tool
cd /d "%~dp0"

rem ==== One-click launcher (logic lives in scripts\launch.py) ====
where python >nul 2>nul
if errorlevel 1 (
    echo [!] Python not found. Please install Python 3.10+ and enable "Add to PATH".
    echo     https://www.python.org/downloads/
    pause
    exit /b 1
)

python scripts\launch.py
