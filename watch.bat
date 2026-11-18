@echo off
rem watch.bat                      opens the settings window
rem watch.bat <channel> [options]  runs in the terminal
cd /d "%~dp0"

if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

if "%~1"=="" (
    python gui.py
) else (
    python watcher.py %*
)
