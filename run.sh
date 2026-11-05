#!/usr/bin/env bash
# ClipSmith launcher for macOS / Linux: bash run.sh
cd "$(dirname "$0")" || exit 1
[ -d .venv ] || python3 -m venv .venv || exit 1
# shellcheck disable=SC1091
source .venv/bin/activate
if [ ! -f .venv/.deps_ok ]; then
  echo "Installing dependencies - first run only, this can take a few minutes..."
  pip install --disable-pip-version-check --no-cache-dir -r requirements.txt || { echo "Install failed - see the messages above."; exit 1; }
  touch .venv/.deps_ok
fi
echo "ClipSmith: open http://127.0.0.1:5000 in your browser"
python app.py
