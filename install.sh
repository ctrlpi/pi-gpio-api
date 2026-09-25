#!/bin/bash
# Usage: curl -fsSL https://raw.githubusercontent.com/ctrlpi/pi-gpio-api/main/install.sh | bash
#
# Downloads the release files (no git required) and (re)installs venv dependencies.
# Creates scripts/ with a cpu-temp.sh example. Downloads only when piped into a shell
# or run with --upgrade. Prompts for an API key + agent name on first install (-y for defaults).
set -e

BASE_URL="https://raw.githubusercontent.com/ctrlpi/pi-gpio-api/main"



FILES="main.py requirements.txt run.sh install.sh LICENSE README.md"
DIR="pi-gpio-api"
SHELLS=(bash sh dash zsh ksh ash)

RUN_AS="$(basename "$0")"
DOWNLOAD=false
for s in "${SHELLS[@]}"; do
  [ "$RUN_AS" = "$s" ] && DOWNLOAD=true
done
ASSUME_YES=false
for arg in "$@"; do
  [ "$arg" = "--upgrade" ] && DOWNLOAD=true
  [ "$arg" = "-y" ] && ASSUME_YES=true
done

if [ "$DOWNLOAD" = true ] && ! command -v curl >/dev/null 2>&1; then
  echo "Error: curl is required. Install it first (e.g. sudo apt install curl)." >&2
  exit 1
fi

# Ensure Python 3.9+ before downloading.
PYTHON_MIN="3.9"
# Colour only when stderr is a terminal, so a redirected log stays escape-code free.
red() {
  if [ -t 2 ]; then printf '\033[1;31m%s\033[0m\n' "$1" >&2; else printf '%s\n' "$1" >&2; fi
}

if ! command -v python3 >/dev/null 2>&1; then
  red "Error: python3 was not found, and pi-gpio-api needs Python $PYTHON_MIN or newer."
  red "Install it and re-run:"
  red "  sudo apt-get update && sudo apt-get install -y python3 python3-venv"
  exit 1
fi
if ! python3 -c "import sys; raise SystemExit(0 if sys.version_info >= tuple(map(int, '$PYTHON_MIN'.split('.'))) else 1)"; then
  red "Error: Python $(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])') is too old."
  red "pi-gpio-api needs Python $PYTHON_MIN or newer. Please upgrade, then re-run this installer:"
  red "  sudo apt-get update && sudo apt-get full-upgrade"
  red "On Raspberry Pi OS Buster or older, apt cannot get you there - flash a current"
  red "Raspberry Pi OS image (Bookworm) instead."
  exit 1
fi

# Update in place if already inside the project folder.
if [ "$(basename "$PWD")" = "$DIR" ] || { [ -f main.py ] && [ -f install.sh ]; }; then
  DIR="."
else
  mkdir -p "$DIR"
fi

if [ "$DOWNLOAD" = true ]; then
  for f in $FILES; do
    echo "Downloading $f..."
    curl -fsSL "$BASE_URL/$f" -o "$DIR/$f.tmp"
    mv "$DIR/$f.tmp" "$DIR/$f"   # only replace the old copy once the download completed
  done
  chmod +x "$DIR/run.sh" "$DIR/install.sh"
fi

# Prompt for API key and agent name on first install (use -y for defaults).
CONFIG_FILE="$DIR/config.json"
DEFAULT_KEY="your-secret-key"
DEFAULT_NAME="$(hostname)"
DEFAULT_NAME="${DEFAULT_NAME%.local}"   # macOS hostnames are "Name.local"; no-op elsewhere

ask() {
  local ans=""
  # Probe /dev/tty quietly first, so a failure (no controlling terminal, e.g. CI)
  # doesn't also swallow the real read's prompt.
  if [ "$ASSUME_YES" != true ] && { : < /dev/tty; } 2>/dev/null; then
    read -r -p "$1 [$2]: " ans < /dev/tty || ans=""
  fi
  printf '%s' "${ans:-$2}"
}

if [ ! -f "$CONFIG_FILE" ]; then
  echo "No config.json yet - let's set an agent name and API key (Enter to accept the default)."
  AGENT_NAME="$(ask "Agent name" "$DEFAULT_NAME")"
  API_KEY="$(ask "API key" "$DEFAULT_KEY")"
  printf '{\n  "api_key": "%s",\n  "name": "%s"\n}\n' "$API_KEY" "$AGENT_NAME" > "$CONFIG_FILE"
  echo "Saved $CONFIG_FILE - API key '$API_KEY', name '$AGENT_NAME'."
fi

# Create scripts folder and seed one example (does not overwrite).
SCRIPTS_DIR="$DIR/scripts"
mkdir -p "$SCRIPTS_DIR"
if [ ! -f "$SCRIPTS_DIR/cpu-temp.sh" ]; then
  cat > "$SCRIPTS_DIR/cpu-temp.sh" <<'SCRIPT'
#!/bin/sh
# CPU temperature in degrees Celsius, as a bare number (e.g. 52.3), not JSON.
# Use it as a sensor: POST /sensor/config/cpu_temp {"script": "cpu-temp.sh"}
awk '{ printf "%.1f\n", $1/1000 }' /sys/class/thermal/thermal_zone0/temp
SCRIPT
  echo "Created $SCRIPTS_DIR/cpu-temp.sh - configure it with {\"script\": \"cpu-temp.sh\"}"
fi

# Reinstall dependencies into the venv (using --system-site-packages for GPIO).
VENV_DIR="$DIR/venv"
# Recreate the venv if missing or broken.
if [ ! -x "$VENV_DIR/bin/python" ] || ! "$VENV_DIR/bin/python" -c '' 2>/dev/null; then
  [ -d "$VENV_DIR" ] && echo "Rebuilding virtual environment..." || echo "Creating virtual environment..."
  rm -rf "$VENV_DIR"
  python3 -m venv --system-site-packages "$VENV_DIR"
fi
if ! "$VENV_DIR/bin/python" -c '' 2>/dev/null; then
  echo "Error: the virtual environment is not usable (dangling bin/python)." >&2
  echo "Install the venv package and re-run:" >&2
  echo "  sudo apt-get update && sudo apt-get install -y python3-venv" >&2
  echo "  ./install.sh" >&2
  exit 1
fi
echo "Installing dependencies..."
# gpiozero is Pi-only; on non-Pi machines main.py uses mock GPIO classes,
# so skip it here without touching requirements.txt.
if grep -qi raspberry /proc/device-tree/model 2>/dev/null; then
  # Verify apt-installed GPIO backend (python3-lgpio or python3-rpi.gpio).
  if ! python3 -c "import lgpio" 2>/dev/null && ! python3 -c "import RPi.GPIO" 2>/dev/null; then
    echo "Error: a GPIO backend is required. Install one, then re-run this script:" >&2
    echo "  sudo apt-get update && sudo apt-get install -y python3-lgpio     # Bookworm or newer (required on a Pi 5)" >&2
    echo "  sudo apt-get update && sudo apt-get install -y python3-rpi.gpio  # Bullseye or older (no python3-lgpio there)" >&2
    echo "  ./install.sh" >&2
    exit 1
  fi
  "$VENV_DIR/bin/python" -m pip install --quiet -r "$DIR/requirements.txt"
else
  echo "Not a Raspberry Pi - skipping gpiozero."
  grep -viE '^gpiozero$' "$DIR/requirements.txt" | "$VENV_DIR/bin/python" -m pip install --quiet -r /dev/stdin
fi

echo "Done."

# Print systemd instructions on Raspberry Pi.
if grep -qi raspberry /proc/device-tree/model 2>/dev/null; then
  SERVICE_UNIT="/etc/systemd/system/pi-gpio-api.service"
  if [ -f "$SERVICE_UNIT" ]; then
    # Already a service - new code only takes effect after a restart.
    echo ""
    echo "pi-gpio-api is set up as a systemd service (pi-gpio-api.service)."
    if command -v systemctl >/dev/null 2>&1; then
      echo "  enabled: $(systemctl is-enabled pi-gpio-api 2>/dev/null || echo unknown), active: $(systemctl is-active pi-gpio-api 2>/dev/null || echo unknown)"
    fi
    echo ""
    echo "Please run the following command to restart the service and pick up this install:"
    echo "sudo systemctl restart pi-gpio-api"
  else
    RUN_SH_PATH="$(cd "$DIR" && pwd)/run.sh"
    echo ""
    echo "To start it now and have it start automatically on every boot, copy-paste this:"
    echo ""
    # Rules mark exactly what to copy - the block would run into the surrounding output otherwise.
    echo "------------------------------------------------------------------------"
    cat <<EOF
sudo tee /etc/systemd/system/pi-gpio-api.service > /dev/null <<UNIT
[Unit]
Description=pi-gpio-api GPIO REST server
After=network.target

[Service]
User=$(whoami)
ExecStart=$RUN_SH_PATH
Restart=on-failure

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload && sudo systemctl enable --now pi-gpio-api
EOF
    echo "------------------------------------------------------------------------"
  fi
fi
echo ""