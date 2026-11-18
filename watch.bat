@echo off
if "%~1"=="" (
    echo Usage: watch.bat ^<twitch_channel^> [--min 5]
    exit /b 1
)

if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

python watcher.py %*

