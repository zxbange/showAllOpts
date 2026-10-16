@echo off
setlocal
if not exist "%~dp0backend\.venv\Scripts\python.exe" (
    echo Python environment not found. See README.md for installation.
    pause
    exit /b 1
)
"%~dp0backend\.venv\Scripts\python.exe" "%~dp0scripts\dev.py"
if errorlevel 1 pause
