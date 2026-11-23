#!/usr/bin/env bash
# ./watch.sh                      opens the settings window
# ./watch.sh <channel> [options]  runs in the terminal
cd "$(dirname "$0")" || exit 1

[ -d tools/ffmpeg/bin ] && export PATH="$PWD/tools/ffmpeg/bin:$PATH"

# Activate virtual environment if present
if [ -f venv/bin/activate ]; then
    source venv/bin/activate
elif [ -f venv/Scripts/activate ]; then
    source venv/Scripts/activate
fi

PYTHON=$(command -v python || command -v python3)
if [ -z "$1" ]; then
    exec "$PYTHON" gui.py
fi
if [ "$1" = "--lan" ]; then
    exec "$PYTHON" gui.py --lan
fi
exec "$PYTHON" watcher.py "$@"
