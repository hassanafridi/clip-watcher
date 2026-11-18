#!/usr/bin/env bash
if [ -z "$1" ]; then
    echo "Usage: ./watch.sh <twitch_channel> [--min 5]"
    exit 1
fi

# Activate virtual environment if present
if [ -d "venv" ]; then
    source venv/bin/activate
fi

python3 watcher.py "$@"
