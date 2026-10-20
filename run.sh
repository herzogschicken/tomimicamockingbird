#!/usr/bin/env bash
# Start tomimicamockingbird on Linux. The first run sets everything up (takes a few minutes).
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8765}"
HOST="${HOST:-127.0.0.1}"

# 1. Find Python 3.10 or newer
PY=""
for c in python3.12 python3.13 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "I can't find Python 3.10 or newer. See 'Step 1' in README.md."; exit 1
fi

# 2. Make a private Python folder (.venv) for the app, once
if [ ! -x .venv/bin/python ]; then
  echo "First run: setting up (this takes a few minutes)..."
  if ! "$PY" -m venv .venv; then
    echo
    echo "Python can't make a venv. On Ubuntu/Debian run:  sudo apt install python3-venv"
    rm -rf .venv; exit 1
  fi
fi

# 3. Install or update the app's packages when they change
WANT_GPU=0
if [ "${TMM_NO_GPU:-0}" != "1" ] && command -v nvidia-smi >/dev/null 2>&1; then WANT_GPU=1; fi
STAMP="$(cat requirements.txt requirements-gpu.txt | sha256sum | cut -d' ' -f1)-gpu$WANT_GPU"
if [ ! -f .venv/.installed ] || [ "$(cat .venv/.installed)" != "$STAMP" ]; then
  echo "Installing packages..."
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/python -m pip install -q -r requirements.txt
  if [ "$WANT_GPU" = "1" ]; then
    echo "NVIDIA card found: adding GPU support (about 600 MB, one time only)..."
    .venv/bin/python -m pip install -q -r requirements-gpu.txt
  else
    echo "No NVIDIA card found: transcription will use the CPU."
  fi
  echo "$STAMP" > .venv/.installed
fi

# 4. Check the helpers
command -v ffprobe >/dev/null 2>&1 || echo "Warning: ffmpeg is not installed. See 'Step 1' in README.md."

echo
echo "tomimicamockingbird is running. Open this in your browser:  http://127.0.0.1:$PORT"
echo "To stop it, press Ctrl+C in this window."
echo
( sleep 2; xdg-open "http://127.0.0.1:$PORT" >/dev/null 2>&1 || true ) &
exec .venv/bin/python -m uvicorn app.main:app --host "$HOST" --port "$PORT"
