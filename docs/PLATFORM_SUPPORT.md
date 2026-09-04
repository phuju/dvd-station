# DiscStation Platform Support

## Linux

Full host support: DVD-Video, data discs, ISO images, audio CDs, ripping,
playback, HTTPS web UI, ESP32 serial, and ESP32 Wi-Fi/OTA.

Run `./install.sh` on Debian, Ubuntu, or Raspberry Pi OS.

## macOS

Near-complete native support via Homebrew dependencies (`install-macos.sh`
installs them and a persistent `launchd` agent):

- **Detect** — optical device via `ioreg` / `drutil` / `diskutil`; blank and
  rewritable media are classified, so the OLED offers the burn menu.
- **Burn** — data and DVD-Video images are built with `xorriso -as mkisofs`
  (`-dvd-video` for a set-top-compatible VIDEO_TS layout) and written with
  `hdiutil burn` (progress streamed from its `-puppetstrings` output).
- **Rip** — audio CD via `cd-paranoia` → FLAC with MusicBrainz metadata and
  cover art; DVD-Video via `dvdbackup -M` (or HandBrake main-feature with
  `DISCSTATION_DVD_RIP_MODE=mkv`), `libdvdcss` handling CSS.
- **Play** — `mpv`, with `dvdnav://` menu navigation for DVD-Video and a
  mounted-VOB fallback.

Set `DISC_DEVICE` if automatic drive detection fails. The one known limitation
is **audio-CD *burning*** — `cdrdao` is the only option and often cannot claim
the drive on recent macOS; DiscStation reports this clearly instead of hanging.

## Windows

Runs the same Python host and web UI as Linux/macOS, triggered the same way
via the ESP32 OLED remote. `install-windows.ps1` sets up Python + venv, a
self-signed cert, firewall rules for 8080/8081, and a per-user auto-start
Scheduled Task (headless `pythonw.exe`, no admin needed).

- **Detect / eject** — WMI (`Win32_CDROMDrive`, `Win32_LogicalDisk`) + IMAPI2
  for media type, blank/rewritable state, and capacity
  (`src/win/disc-info.ps1`, `src/win/eject.ps1`).
- **Burn** — data, ISO, and audio CD all go through IMAPI2
  (`src/win/burn-{image,data,audio}.ps1`), with live progress streamed to the
  OLED; `isoburn.exe /q` is the zero-dependency fallback for a raw ISO.
  Works on Windows 7 SP1 and 10/11.
- **Rip** — DVD main-feature mode via HandBrakeCLI (winget on 10/11) works;
  a full unencrypted VIDEO_TS mirror and audio-CD ripping aren't implemented
  yet (no Windows path for `dvdbackup`/`cd-paranoia`).
- **Play** — `mpv`, via the same code path as Linux/macOS (device letter for
  DVD-Video, `cdda://` for audio CD, mounted drive for data/VCD).

Windows 7 (no `winget`) gets the host, detection, eject, and all three burn
modes; rip/play need tools that don't install cleanly on 7, so those OLED
actions report "not supported" there. Set `DISC_DEVICE` to override drive
auto-detection.

## Shared Components

- DiscStation workflows and web UI
- ESP32 serial protocol
- File upload and folder preservation
- Video/audio metadata handling (MusicBrainz, TMDb)
- QR-code web URL display

The optical-drive backend is the platform boundary. Linux and macOS are
complete (bar macOS audio-CD burning); Windows burns and plays, with DVD-mirror
and audio-CD ripping still to come.
