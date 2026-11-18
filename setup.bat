@echo off
echo [*] Setting up clip-watcher for Windows PC...

:: 1. Install FFmpeg via winget if not already installed
where ffmpeg >nul 2>nul
if %errorlevel% neq 0 (
    echo [*] Installing FFmpeg via winget...
    winget install --id Gyan.FFmpeg -e --source winget --accept-package-agreements --accept-source-agreements
) else (
    echo [*] FFmpeg is already installed.
)

:: 2. Create virtual environment
python -m venv venv
call venv\Scripts\activate.bat

:: 3. Upgrade pip and install requirements
python -m pip install --upgrade pip
pip install -r requirements.txt

echo.
echo [DONE] Setup complete! Run: watch.bat ^<twitch_channel^>
pause

