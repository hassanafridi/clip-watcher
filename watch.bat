@echo off
rem watch.bat                      opens the settings window
rem watch.bat <channel> [options]  runs in the terminal
cd /d "%~dp0"

if exist "tools\ffmpeg\bin" set "PATH=%CD%\tools\ffmpeg\bin;%PATH%"

if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

if "%~1"=="" (
    python gui.py
) else if "%~1"=="--lan" (
    python gui.py --lan
) else (
    python watcher.py %*
)
