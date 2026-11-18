#!/usr/bin/env bash
# ./watch.sh                      opens the settings window
# ./watch.sh <channel> [options]  runs in the terminal
cd "$(dirname "$0")" || exit 1

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
exec "$PYTHON" watcher.py "$@"
