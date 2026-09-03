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

The Python host and web/control workflow can run from PowerShell. Optical
burning needs a Windows IMAPI backend or a separately installed compatible
burning tool. The Windows installer deliberately reports this limitation
instead of silently attempting Linux commands.

## Shared Components

- DiscStation workflows and web UI
- ESP32 serial protocol
- File upload and folder preservation
- Video/audio metadata handling (MusicBrainz, TMDb)
- QR-code web URL display

The optical-drive backend is the platform boundary. Linux and macOS are
complete (bar macOS audio-CD burning); Windows still needs its writer backend.
