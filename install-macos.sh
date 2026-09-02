#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
APP_DIR="${DISCSTATION_APP_DIR:-$HOME/Library/Application Support/DiscStation/app}"
VENV_DIR="${DISCSTATION_VENV_DIR:-$HOME/Library/Application Support/DiscStation/venv}"
CONFIG_DIR="${DISCSTATION_CONFIG_DIR:-$HOME/Library/Application Support/DiscStation}"
PLIST="$HOME/Library/LaunchAgents/com.discstation.agent.plist"

if [[ "$(uname -s)" != "Darwin" ]]; then
  printf 'Run this script on macOS.\n' >&2
  exit 1
fi
if ! command -v brew >/dev/null 2>&1; then
  printf 'Homebrew is missing. Starting the official Homebrew installer...\n'
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi

if [[ -x /opt/homebrew/bin/brew ]]; then
  eval "$(/opt/homebrew/bin/brew shellenv)"
elif [[ -x /usr/local/bin/brew ]]; then
  eval "$(/usr/local/bin/brew shellenv)"
fi

if ! command -v brew >/dev/null 2>&1; then
  printf 'Homebrew installation did not complete.\n' >&2
  exit 1
fi

brew install python ffmpeg cdrdao dvdauthor node yt-dlp xorriso mpv libdiscid handbrake
mkdir -p "$APP_DIR" "$VENV_DIR" "$CONFIG_DIR"
cp -R "$ROOT_DIR/src/." "$APP_DIR/"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
if [[ -f "$ROOT_DIR/requirements.txt" ]]; then
  "$VENV_DIR/bin/python" -m pip install -r "$ROOT_DIR/requirements.txt"
else
  "$VENV_DIR/bin/python" -m pip install pyserial mutagen requests yt-dlp
fi
# Optional metadata deps — best effort. python-libdiscid (a C extension) is
# Linux-only in requirements-optional.txt; on macOS install the pure-Python
# bits and let brew's libdiscid cover disc IDs via the CLI tools.
if [[ -f "$ROOT_DIR/requirements-optional.txt" ]]; then
  "$VENV_DIR/bin/python" -m pip install musicbrainzngs tmdbsimple \
    || printf 'Optional metadata deps skipped (host still works).\n'
fi

DISC_DEVICE="${DISC_DEVICE:-}"
DISC_PORT="${DISC_PORT:-$(ls /dev/cu.usbserial-* /dev/cu.usbmodem-* 2>/dev/null | head -1)}"

if [[ ! -f "$CONFIG_DIR/server.crt" || ! -f "$CONFIG_DIR/server.key" ]]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout "$CONFIG_DIR/server.key" \
    -out "$CONFIG_DIR/server.crt" \
    -subj "/CN=DiscStation"
  chmod 600 "$CONFIG_DIR/server.key"
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.discstation.agent</string>
  <key>ProgramArguments</key><array>
    <string>$VENV_DIR/bin/python</string>
    <string>-u</string>
    <string>$APP_DIR/discstation.py</string>
    <string>--port</string><string>8080</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>PYTHONUNBUFFERED</key><string>1</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>DISC_DEVICE</key><string>${DISC_DEVICE:-}</string>
    <key>DISC_PORT</key><string>${DISC_PORT:-}</string>
  </dict>
  <key>WorkingDirectory</key><string>$APP_DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/DiscStation.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/DiscStation.log</string>
</dict></plist>
PLIST
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

printf '\nDiscStation macOS host files installed at:\n%s\n' "$APP_DIR"
printf 'Optical-device support is experimental; set DISC_DEVICE if automatic detection fails.\n'
printf 'Run: %q %q\n' "$VENV_DIR/bin/python" "$APP_DIR/discstation.py"
