# DVD Station

Physical disc burning appliance — standalone DVD/Blu-ray burner with ESP32 remote control.

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
| **Rip** | Audio CD → FLAC with MusicBrainz metadata |

## Project Structure

```
.
├── src/
│   ├── dvd_station.py    # Main orchestrator, serial, web server
│   └── dvd_burn.py       # Burn pipeline (download, convert, author, burn)
├── arduino/
│   ├── c6/               # ESP32-C6 firmware (current)
│   └── v1/               # ESP32 V1 firmware (legacy)
├── systemd/              # dvd-station.service for auto-start
└── README.md
```

## Setup

```bash
# Dependencies
sudo apt install growisofs ffmpeg dvdauthor lsdvd wodim mpv genisoimage python3-serial python3-mutagen python3-requests

# Arduino
# Upload arduino/c6/DVD_Station_C6.ino to ESP32-C6 via Arduino IDE

# Systemd
cp systemd/dvd-station.service ~/.config/systemd/user/
systemctl --user enable --now dvd-station.service
```

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
| `DVD_SPEED` | auto | Burn speed (e.g. `4x`, `8x`) |
| `DVD_TARGET_BYTES` | 4300000000 | Target size for AUTO mode |
| `DVD_CLEANUP_DAYS` | 2 | Auto-cleanup old job directories |

## Modes (Video DVD)

| Mode | Quality | Use Case |
|------|---------|----------|
| AUTO | Standard ~4300kbps | Default |
| BEST | Higher ~4450MB target | Quality priority |
| LONG | Lower ~350kbps min | Long videos (>2.5h) |
| DATA | Original quality | Files as-is on disc |

## License

MIT
