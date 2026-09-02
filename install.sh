#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
APP_DIR="${DISCSTATION_APP_DIR:-$HOME/.local/share/discstation/app}"
VENV_DIR="${DISCSTATION_VENV_DIR:-$HOME/.local/share/discstation/venv}"
CONFIG_DIR="${DISCSTATION_CONFIG_DIR:-$HOME/.local/share/discstation}"

if [[ "$(uname -s)" != "Linux" ]]; then
  printf 'This installer is for Linux. Use install-macos.sh or install-windows.ps1.\n' >&2
  exit 1
fi

if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y \
    cdrdao dvdauthor ffmpeg genisoimage growisofs handbrake-cli lsdvd mpv \
    nodejs openssl python3 python3-pip python3-serial python3-mutagen \
    python3-requests python3-pyudev python3-libdiscid python3-musicbrainzngs \
    libdiscid0 python3-venv wodim
else
  printf 'Unsupported Linux package manager. Install the DiscStation dependencies manually.\n' >&2
  exit 1
fi

mkdir -p "$APP_DIR" "$CONFIG_DIR" "$VENV_DIR"
cp -R "$ROOT_DIR/src/." "$APP_DIR/"
if [[ ! -f "$CONFIG_DIR/discstation.env" && -f "$ROOT_DIR/discstation.env.example" ]]; then
  cp "$ROOT_DIR/discstation.env.example" "$CONFIG_DIR/discstation.env"
fi

python3 -m venv --system-site-packages "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
if [[ -f "$ROOT_DIR/requirements.txt" ]]; then
  "$VENV_DIR/bin/python" -m pip install -r "$ROOT_DIR/requirements.txt"
else
  "$VENV_DIR/bin/python" -m pip install pyserial mutagen requests yt-dlp
fi
# Optional metadata/detection deps — best effort, never fatal.
if [[ -f "$ROOT_DIR/requirements-optional.txt" ]]; then
  "$VENV_DIR/bin/python" -m pip install -r "$ROOT_DIR/requirements-optional.txt" \
    || printf 'Optional metadata deps skipped (host still works).\n'
fi

if [[ ! -f "$CONFIG_DIR/server.crt" || ! -f "$CONFIG_DIR/server.key" ]]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout "$CONFIG_DIR/server.key" \
    -out "$CONFIG_DIR/server.crt" \
    -subj "/CN=DiscStation"
  chmod 600 "$CONFIG_DIR/server.key"
fi

sudo usermod -aG dialout,cdrom "$USER" || true
mkdir -p "$HOME/.config/systemd/user"
cp "$ROOT_DIR/systemd/discstation.service" "$HOME/.config/systemd/user/discstation.service"
systemctl --user daemon-reload
systemctl --user enable --now discstation.service

printf '\nDiscStation installed. Log out/in if device permissions changed.\n'
printf 'App: %s\n' "$APP_DIR"
printf 'Web: https://%s:8080\n' "$(hostname -I | awk '{print $1}')"
