# DiscStation

DiscStation is a physical-media appliance for DVD-Video, data DVDs, audio CDs,
playback, ripping, phone uploads, and ESP32 remote control.

## Hardware

- **Brain:** Raspberry Pi 4/5 (or any Linux box)
- **Remote:** ESP32-C6 with SSD1306 OLED, rotary encoder, and buttons
- **Drive:** ATAPI DVD writer (e.g. iHAS124) over USB

## Features

| Mode | Description |
|------|-------------|
| **Burn Video DVD** | YouTube URL or local file → ffmpeg 2-pass → DVD-Video disc |
| **Burn Data DVD** | Any files/folders → ISO/Joliet data disc (no quality loss) |
| **Burn MPG** | Re-burn a previously converted movie.mpg |
| **Play** | Playback via mpv (DVD-Video, Audio CD, VCD, SVCD) |
| **Rip** | Audio CD → FLAC (MusicBrainz); DVD-Video → VIDEO_TS mirror or HandBrake MKV (TMDb naming) |

## Project Structure

```
.
├── src/
│   ├── discstation.py        # Main orchestrator, serial, web server
│   ├── discstation_burn.py   # Burn pipeline (download, convert, author, burn)
│   ├── discstation_host.py   # Per-OS device/path/backend abstraction
│   ├── discstation_meta.py   # TMDb video metadata
│   └── static/               # Built-in web UI (index.html, app.js, style.css)
├── mobile/                   # Expo (React Native) companion app
├── arduino/
│   ├── c6/                   # ESP32-C6 firmware
│   └── v1/                   # ESP32 DevKit firmware
├── scripts/setup.mjs         # `discstation-setup` — cross-OS installer dispatch
├── docs/                     # Platform support notes
├── install.sh / install-macos.sh / install-windows.ps1
├── requirements.txt / requirements-optional.txt
├── systemd/                  # Linux auto-start unit
└── README.md
```

## Setup

### Install (any OS)

```bash
npm install -g discstation
discstation-setup          # dispatches to the installer for your OS, then opens the UI
```

The host runs as a background service; the UI is the built-in web app at
`http://localhost:8081`. `discstation-setup` opens it when it finishes, and the
`discstation` command re-opens it any time. It's a PWA — use your browser's
**Install DiscStation** (or the in-page **INSTALL APP** button on Chromium) to
get a standalone app window with a dock/taskbar icon on macOS, Linux, and
Windows.

`discstation-setup --help` lists the forwarded env vars. From a git clone,
`npm run setup` does the same thing. Support by OS:

| OS | What runs | Optical support |
|----|-----------|-----------------|
| Linux | `install.sh` — apt deps, venv, systemd `--user` service, self-signed cert | full burn / rip / play |
| macOS | `install-macos.sh` — Homebrew deps, venv, launchd agent, cert | burn / rip / play (audio-CD *burning* is best-effort; set `DISC_DEVICE` if detection fails) |
| Windows | `install-windows.ps1` — Python + venv, self-signed cert, firewall rules, auto-start Scheduled Task | burn (ISO / data / audio CD) via IMAPI2 on Windows 10/11 and 7; DVD rip (HandBrake main-feature mode) and play (mpv) use the shared cross-platform path; full VIDEO_TS mirror rip and audio-CD rip aren't implemented on Windows yet |

### Or run the platform script directly

```bash
# Linux host
./install.sh

# Arduino
arduino-cli lib install QRCode
# Upload arduino/c6/DiscStation_C6.ino or arduino/v1/DiscStation.ino
# to the matching ESP32 board

# macOS host
./install-macos.sh

# Windows host
PowerShell -ExecutionPolicy Bypass -File .\install-windows.ps1
```

## Platform Support

Linux and macOS both run the complete optical workflow (burn, rip, play) —
macOS via `xorriso` / `hdiutil` / `cd-paranoia` / `dvdbackup` / HandBrake, with
audio-CD *burning* the one best-effort area. Windows burns (ISO / data / audio
CD) via IMAPI2 and plays via mpv; DVD ripping works in HandBrake main-feature
mode but not full VIDEO_TS mirror or audio-CD ripping yet. See
`docs/PLATFORM_SUPPORT.md`.

## Web Interface

Built-in web server on port 8080 (HTTPS with self-signed cert):

- Upload files from any device on the LAN for data DVD burning
- Submit YouTube URLs for video DVD burning
- Dark theme, mobile-responsive, PWA (installable on phone)

## Disc Support

- DVD-R, DVD+R (single and dual-layer)
- DVD-RW, DVD+RW
- CD-R, CD-RW (audio CD ripping)

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DISC_SPEED` | auto | Burn speed (e.g. `4x`, `8x`) |
| `DISC_TARGET_BYTES` | 4300000000 | Target size for AUTO mode |
| `DISC_CLEANUP_DAYS` | 2 | Auto-cleanup old job directories |
| `DISC_OUTPUT_LIMIT_BYTES` | 4300000000 | Conservative DVD5 payload limit |
| `DISC_DL_OUTPUT_LIMIT_BYTES` | 8000000000 | Conservative DVD9 payload limit |
| `DISC_DEVICE` | auto | Optical drive node override |
| `DISC_PORT` | auto | ESP32 serial port override |
| `DISCSTATION_HTTP_PORT` | 8081 | Plain-HTTP port for the mobile app (`0` disables) |
| `DISCSTATION_DVD_RIP_MODE` | mirror | `mkv` = HandBrake main-feature transcode instead of a full VIDEO_TS mirror |
| `DISC_AUDIO_DEVICE` | auto | mpv audio-output device override |

### YouTube Download Configuration

The host prefers an embeddable YouTube client and small HTTP chunks to reduce
current YouTube 403 failures. It falls back to the Android VR client if needed.

| Variable | Default | Description |
|----------|---------|-------------|
| `YTDLP_PLAYER_CLIENTS` | `web_embedded,android_vr` | Comma-separated clients tried in order |
| `YTDLP_HTTP_CHUNK_SIZE` | `1M` | HTTP chunk size for media downloads |
| `YTDLP_FORMAT` | H.264/AAC up to 720p, then generic MP4 | yt-dlp format selector override |
| `YTDLP_RETRIES` | `3` | HTTP download retries per client |
| `YTDLP_FRAGMENT_RETRIES` | `3` | DASH/HLS fragment retries |
| `YTDLP_COOKIES` | unset | Netscape-format cookies file |
| `YTDLP_COOKIES_FROM_BROWSER` | unset | Browser source such as `firefox` or `chrome` |
| `YTDLP_USER_AGENT` | unset | Optional browser user-agent matching the cookies |
| `YTDLP_PO_TOKEN` | unset | Client/context PO token in yt-dlp format |
| `YTDLP_EXTRACTOR_ARGS` | unset | Additional yt-dlp extractor arguments |

Cookie files and PO tokens must stay outside the repository and service logs.

## Modes (Video DVD)

| Mode | Quality | Use Case |
|------|---------|----------|
| AUTO | Standard ~4300kbps | Default |
| BEST | Higher ~4450MB target | Quality priority |
| LONG | Lower ~350kbps min | Long videos (>2.5h) |
| DATA | Original quality | Files as-is on disc |

## License

MIT
