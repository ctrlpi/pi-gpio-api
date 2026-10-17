#!/bin/bash
set -e

# Run from the script's own directory to ensure correct paths.
cd "$(dirname "$0")"

VENV_DIR="venv"

# Parse arguments to check for --lite flag.
RUN_LITE=false
ARGS=()
for arg in "$@"; do
  if [ "$arg" = "--lite" ]; then
    RUN_LITE=true
  else
    ARGS+=("$arg")
  fi
done

# Recreate the venv if missing or broken.
if [ ! -x "$VENV_DIR/bin/python" ] || ! "$VENV_DIR/bin/python" -c '' 2>/dev/null; then
  [ -d "$VENV_DIR" ] && echo "Rebuilding virtual environment..." || echo "Creating virtual environment..."
  rm -rf "$VENV_DIR"
  python3 -m venv --system-site-packages "$VENV_DIR"
  if ! "$VENV_DIR/bin/python" -c '' 2>/dev/null; then
    echo "Error: the virtual environment is not usable (dangling bin/python)." >&2
    echo "Install the venv package and re-run: sudo apt-get install -y python3-venv" >&2
    exit 1
  fi
  echo "Installing dependencies..."
  # Skip gpiozero installation on non-Pi machines.
  FILTER_EXPR='^$'
  if [ "$RUN_LITE" = true ]; then
    FILTER_EXPR='^(fastapi|pydantic|uvicorn)$'
  fi
  if grep -qi raspberry /proc/device-tree/model 2>/dev/null; then
    # Check for required GPIO backend.
    if ! python3 -c "import lgpio" 2>/dev/null && ! python3 -c "import RPi.GPIO" 2>/dev/null; then
      echo "Error: a GPIO backend is required. Install one, then re-run this script:" >&2
      echo "  sudo apt-get update && sudo apt-get install -y python3-lgpio     # Bookworm or newer (required on a Pi 5)" >&2
      echo "  sudo apt-get update && sudo apt-get install -y python3-rpi.gpio  # Bullseye or older (no python3-lgpio there)" >&2
      echo "  ./run.sh" >&2
      exit 1
    fi
    if [ "$RUN_LITE" = true ]; then
      grep -viE "$FILTER_EXPR" requirements.txt | "$VENV_DIR/bin/python" -m pip install --quiet -r /dev/stdin
    else
      "$VENV_DIR/bin/python" -m pip install --quiet -r requirements.txt
    fi
  else
    echo "Not a Raspberry Pi - skipping gpiozero."
    FILTER_EXPR='^gpiozero$'
    if [ "$RUN_LITE" = true ]; then
      FILTER_EXPR='^(gpiozero|fastapi|pydantic|uvicorn)$'
    fi
    grep -viE "$FILTER_EXPR" requirements.txt | "$VENV_DIR/bin/python" -m pip install --quiet -r /dev/stdin
  fi
fi

# Auto-detect if fastapi and pydantic are installed; if not, automatically run lite.py regardless of --lite flag.
if ! "$VENV_DIR/bin/python" -c "import fastapi, pydantic" 2>/dev/null; then
  RUN_LITE=true
fi

IP=$(hostname -I 2>/dev/null | awk '{print $1}' || ipconfig getifaddr en0 2>/dev/null || echo "localhost")

echo ""
echo "Starting server at http://$IP:8314"
echo ""

if [ "$RUN_LITE" = true ]; then
  "$VENV_DIR/bin/python" lite.py "${ARGS[@]}"
else
  "$VENV_DIR/bin/python" main.py "${ARGS[@]}"
fi
