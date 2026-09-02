# DiscStation Platform Support

## Linux

Full host support: DVD-Video, data discs, ISO images, audio CDs, ripping,
playback, HTTPS web UI, ESP32 serial, and ESP32 Wi-Fi/OTA.

Run `./install.sh` on Debian, Ubuntu, or Raspberry Pi OS.

## macOS

The Python host and web/control workflow run with Homebrew dependencies. Data
and DVD image burning use `hdiutil`; DVD-Video discs can be mounted read-only
for playback and VIDEO_TS ripping; audio CD burning uses cdrdao. Optical-device
discovery and video/audio workflows are experimental; use `DISC_DEVICE`
when automatic detection does not find the external drive. Apple Music owns
audio CD playback and ripping on macOS, so DiscStation does not probe or access
audio tracks for those operations. Audio burns include CD-TEXT from the source
folder/file name and track metadata. Apple Music may still show generic track
names when its online CD lookup has no match; CD-TEXT-capable players can read
the embedded names. The macOS installer installs a persistent `launchd` service.

## Windows

The Python host and web/control workflow can run from PowerShell. Optical
burning needs a Windows IMAPI backend or a separately installed compatible
burning tool. The Windows installer deliberately reports this limitation
instead of silently attempting Linux commands.

## Shared Components

- DiscStation workflows and web UI
- ESP32 serial protocol
- File upload and folder preservation
- Video/audio metadata handling
- QR-code web URL display

The optical-drive backend is the platform boundary. It should be completed per
OS before claiming full native burning support on that platform.
