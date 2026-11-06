#!/bin/bash
# Run ReadyStamp on a Mac straight from the source code (no prebuilt app).
# Double-click this file in Finder, or run it from Terminal.
# First run creates a private Python environment in .venv (takes a minute).
set -e
cd "$(dirname "$0")/.."

PY=""
for cand in python3.13 python3.12 python3.11 python3; do
  if command -v "$cand" >/dev/null 2>&1 && \
     "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PY="$cand"; break
  fi
done
if [ -z "$PY" ]; then
  echo "ReadyStamp needs Python 3.11 or newer."
  echo "Install it from https://www.python.org/downloads/macos/ and run this again."
  read -r -p "Press Return to close." _
  exit 1
fi

if [ ! -x .venv/bin/readystamp ]; then
  echo "Setting up ReadyStamp with $("$PY" --version)..."
  "$PY" -m venv .venv
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet .
fi

echo "Starting ReadyStamp. Your browser will open in a moment."
echo "Keep this window open while you work. Close it to quit."
exec .venv/bin/readystamp ui
