#!/usr/bin/env bash
set -e

echo "[*] Setting up clip-watcher for Linux/macOS..."

# 1. Install FFmpeg and Python venv tools if missing
if command -v apt-get &> /dev/null; then
    sudo apt-get update
    sudo apt-get install -y ffmpeg python3-venv python3-pip
elif command -v dnf &> /dev/null; then
    sudo dnf install -y ffmpeg python3-pip
elif command -v pacman &> /dev/null; then
    sudo pacman -Sy --noconfirm ffmpeg python-pip
elif command -v brew &> /dev/null; then
    brew install ffmpeg
else
    echo "[!] Please install 'ffmpeg' manually for your OS."
fi

# 2. Create virtual environment
python3 -m venv venv
source venv/bin/activate

# 3. Upgrade pip and install requirements
pip install --upgrade pip
pip install -r requirements.txt

# 4. Make launcher executable
chmod +x watch.sh 2>/dev/null || true

echo ""
echo "[DONE] Setup complete. Run ./watch.sh to open the streams board, or ./watch.sh <channel> to watch one in the terminal."

