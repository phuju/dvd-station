"""Host-specific discovery and paths for DiscStation.

The workflows use this module instead of assuming Linux device names or
home-directory layout. Optical burning commands remain backend-specific.
"""

import os
import platform
import re
import shutil
import subprocess
from pathlib import Path

from serial.tools import list_ports


def system_name():
    return platform.system().lower()


def user_home():
    return Path.home()


def config_dir():
    override = os.environ.get("DISCSTATION_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    system = system_name()
    if system == "windows":
        return Path(os.environ.get("APPDATA", user_home())) / "DiscStation"
    if system == "darwin":
        return user_home() / "Library" / "Application Support" / "DiscStation"
    return user_home() / ".local" / "share" / "discstation"


def cache_dir():
    override = os.environ.get("DISCSTATION_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    system = system_name()
    if system == "windows":
        return Path(os.environ.get("LOCALAPPDATA", user_home())) / "DiscStation" / "cache"
    if system == "darwin":
        return user_home() / "Library" / "Caches" / "DiscStation"
    return Path(os.environ.get("XDG_CACHE_HOME", user_home() / ".cache")) / "discstation"


def serial_port():
    override = os.environ.get("DISC_PORT")
    if override:
        path = Path(override).expanduser()
        if path.exists():
            return str(path)

    ports = list(list_ports.comports())
    preferred = []
    for port in ports:
        vid_pid = (port.vid, port.pid)
        description = (port.description or "").lower()
        if vid_pid == (0x303A, 0x1001) or "esp32" in description or "cp210" in description:
            preferred.append(port.device)
    if not preferred:
        return None
    return _stable_serial_path(sorted(preferred)[0])


def _stable_serial_path(device):
    """Map a volatile /dev/ttyUSBN to its stable /dev/serial/by-id/ symlink so a
    USB re-enumeration (ttyUSB1 -> ttyUSB0) doesn't strand the reconnect loop."""
    try:
        target = Path(device).resolve()
        for link in Path("/dev/serial/by-id").iterdir():
            try:
                if link.resolve() == target:
                    return str(link)
            except OSError:
                continue
    except OSError:
        pass
    return device


def _mac_optical_device():
    try:
        result = subprocess.run(
            ["/usr/sbin/ioreg", "-r", "-c", "IODVDServices", "-l"],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r'"BSD Name"\s*=\s*"(disk\d+)"', result.stdout)
    return f"/dev/{match.group(1)}" if match else None


_last_disc_device = None


def disc_device():
    global _last_disc_device
    system = system_name()
    override = os.environ.get("DISC_DEVICE") or os.environ.get("DVD_DEVICE")
    if system == "darwin":
        detected = _mac_optical_device()
        if detected:
            _last_disc_device = detected
            return detected
        if override:
            return override
        # macOS: the /dev/diskN node only exists while media is loaded. Once a
        # disc is ejected there is nothing to detect — keep returning the last
        # known node so the caller can still probe it (it just reports no media).
        if _last_disc_device:
            return _last_disc_device
    elif override:
        return override

    if system == "linux":
        for name in ("/dev/dvd", "/dev/cdrom"):
            path = Path(name)
            if path.exists():
                return str(path.resolve())
        drives = sorted(Path("/dev").glob("sr*"))
        if drives:
            return str(drives[0])
    elif system == "darwin":
        try:
            status = subprocess.run(["/usr/bin/drutil", "status"], capture_output=True, text=True, check=False, timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            status = None
        if status:
            match = re.search(r"Name:\s+(/dev/disk\S+)", status.stdout + status.stderr)
            if match:
                return match.group(1)
        result = subprocess.run(["/usr/sbin/diskutil", "list"], capture_output=True, text=True, check=False, timeout=5)
        for line in result.stdout.splitlines():
            if "/dev/disk" in line and ("CD" in line or "DVD" in line or "optical" in line.lower()):
                return line.strip().split()[0]
    elif system == "windows":
        raise RuntimeError("Set DISC_DEVICE to the optical drive letter on Windows")
    raise FileNotFoundError("No optical disc drive found; set DISC_DEVICE explicitly")


def drive_status():
    """Non-Linux equivalent of the CDROM_DRIVE_STATUS ioctl.
    Returns 'disc' | 'no_disc' | 'unknown'. macOS: parse `drutil status`."""
    if system_name() != "darwin":
        return "unknown"
    try:
        r = subprocess.run(["/usr/bin/drutil", "status"], capture_output=True,
                           text=True, check=False, timeout=4)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    text = (r.stdout + r.stderr).lower()
    if "no media" in text or "media is not present" in text:
        return "no_disc"
    if "name: /dev/disk" in text or "type: cd" in text or "type: dvd" in text \
       or "type: bd" in text:
        return "disc"
    return "unknown"


def _tag_rewritable(props, media_type):
    """Set the ID_CDROM_MEDIA_*_RW key that discstation.is_rewritable_disc reads,
    from a lowercased media-type string (drutil 'Type:' or diskutil 'Optical
    Media Type'). ponytail: substring match — same heuristic as the rest of this
    module."""
    if "cd-rw" in media_type or "cdrw" in media_type:
        props["ID_CDROM_MEDIA_CD_RW"] = "1"
    elif "dvd-rw" in media_type or "dvd-ram" in media_type:
        props["ID_CDROM_MEDIA_DVD_RW"] = "1"
    elif "dvd+rw" in media_type or "bd-re" in media_type:
        props["ID_CDROM_MEDIA_DVD_PLUS_RW"] = "1"


def _drutil_media_properties():
    result = subprocess.run(["/usr/bin/drutil", "status"], capture_output=True, text=True, check=False, timeout=3)
    text = result.stdout + result.stderr
    lowered = text.lower()
    if result.returncode != 0 or "no media" in lowered:
        return {}
    props = {"ID_CDROM": "1", "ID_CDROM_MEDIA": "1"}
    blank = "space used:" in lowered and "00:00:00" in lowered.split("space used:", 1)[1][:24]
    if blank:
        props["ID_CDROM_MEDIA_STATE"] = "blank"
    match = re.search(r"type:\s*([a-z0-9+\-]+)", lowered)
    media_type = match.group(1) if match else ""
    if media_type.startswith("dvd") or media_type.startswith("bd"):
        props["ID_CDROM_MEDIA_TYPE"] = "dvd"
    elif media_type.startswith("cd") and not blank and "-r" not in media_type:
        props["ID_CDROM_MEDIA_TYPE"] = "audio"
    _tag_rewritable(props, media_type)
    return props


def media_properties(device):
    """Return udev-like media properties on non-Linux hosts."""
    if system_name() == "darwin":
        try:
            disk = subprocess.run(["/usr/sbin/diskutil", "info", device], capture_output=True, text=True, check=False, timeout=3)
        except subprocess.TimeoutExpired:
            return _drutil_media_properties()
        if disk.returncode != 0:
            return _drutil_media_properties()
        fields = {}
        for line in disk.stdout.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                fields[key.strip()] = value.strip()
        optical = fields.get("Optical Media Type", "")
        if not optical and not fields.get("Device / Media Name"):
            return _drutil_media_properties()
        props = {"ID_CDROM": "1", "ID_CDROM_MEDIA": "1"}
        label = fields.get("Volume Name", "")
        filesystem = fields.get("Type (Bundle)") or fields.get("File System Personality")
        if label and label not in ("Not applicable (no file system)", ""):
            props["ID_FS_LABEL"] = label
        if filesystem:
            normalized = re.sub(r"[^a-z0-9]+", "", filesystem.lower())
            if "udf" in normalized or "universaldiskformat" in normalized:
                props["ID_FS_TYPE"] = "udf"
            elif "iso9660" in normalized:
                props["ID_FS_TYPE"] = "iso9660"
            else:
                props["ID_FS_TYPE"] = filesystem.lower()
        mount_point = fields.get("Mount Point", "")
        if mount_point and mount_point != "Not applicable":
            props["ID_MOUNT_POINT"] = mount_point
        if label.lower() == "audio cd":
            props["ID_CDROM_MEDIA_TYPE"] = "audio"
        elif "cd" in optical.lower() and "r" not in optical.lower():
            props["ID_CDROM_MEDIA_TYPE"] = "audio"
        elif "dvd" in optical.lower():
            props["ID_CDROM_MEDIA_TYPE"] = "dvd"
        if not label or label == "Not applicable (no file system)":
            props["ID_CDROM_MEDIA_STATE"] = "blank"
        optical = optical.lower()
        if "dvd+r dl" in optical or "dvd-r dl" in optical:
            props["ID_CDROM_MEDIA_DVD_PLUS_R_DL"] = "1"
        elif ("dvd+r" in optical or "dvd-r" in optical) and "rw" not in optical:
            props["ID_CDROM_MEDIA_DVD_PLUS_R"] = "1"
        _tag_rewritable(props, optical)
        return props
    return {}


def media_capacity_bytes(device):
    if system_name() == "darwin":
        try:
            result = subprocess.run(["/usr/sbin/diskutil", "info", device], capture_output=True, text=True, check=False, timeout=3)
        except subprocess.TimeoutExpired:
            result = subprocess.run(["/usr/bin/drutil", "status"], capture_output=True, text=True, check=False, timeout=3)
            match = re.search(r"blocks:\s*(\d+)\s*/", result.stdout + result.stderr, re.IGNORECASE)
            if match:
                return int(match.group(1)) * 2048
            return None
        match = re.search(r"Disk Size:.*?\((\d+) Bytes\)", result.stdout, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def tool(name):
    local = user_home() / ".local" / "bin" / name
    if local.exists():
        return str(local)
    path = shutil.which(name)
    if path:
        return path
    if system_name() == "darwin":
        candidates = [
            Path("/opt/homebrew/bin") / name,
            Path("/usr/local/bin") / name,
            Path("/opt/homebrew/opt/cdrtools/bin") / name,
            Path("/usr/local/opt/cdrtools/bin") / name,
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
    raise FileNotFoundError(
        f"{name} is not installed on {system_name()}; install the DiscStation host dependencies"
    )


def null_device():
    return "NUL" if system_name() == "windows" else "/dev/null"


def build_data_image(source_paths, output_path, label, video=False):
    system = system_name()
    if system in ("darwin", "windows"):
        # xorriso's mkisofs emulation is the same lineage as Linux's
        # genisoimage/growisofs; -dvd-video gives a set-top-compatible
        # VIDEO_TS layout (the arg is the dir *containing* VIDEO_TS).
        command = [tool("xorriso"), "-as", "mkisofs", "-V", label, "-o", str(output_path)]
        if video:
            command += ["-dvd-video", "-udf", str(source_paths[0])]
        else:
            command += ["-iso-level", "3", "-J", "-R", "-udf",
                        *[str(path) for path in source_paths]]
    else:
        raise RuntimeError("Image building is only used by non-Linux optical backends")
    subprocess.run(command, check=True, capture_output=True, text=True)
    return output_path


def iso_burn_command(device, image_path):
    system = system_name()
    if system == "darwin":
        # -puppetstrings emits machine-readable PERCENT: / MESSAGE: lines.
        return [tool("hdiutil"), "burn", "-puppetstrings", str(image_path)]
    if system == "windows":
        return [tool("isoburn.exe"), "/Q", device, str(image_path)]
    raise RuntimeError("ISO command requested on Linux; use growisofs backend")


def audio_output_device():
    """Return an MPV audio-device override for a connected Bluetooth sink."""
    override = os.environ.get("DISC_AUDIO_DEVICE")
    if override:
        return override
    if system_name() == "linux":
        pactl = shutil.which("pactl")
        if pactl:
            result = subprocess.run([pactl, "list", "short", "sinks"], capture_output=True, text=True, check=False)
            for line in result.stdout.splitlines():
                fields = line.split()
                if len(fields) >= 2 and "bluez_output." in fields[1]:
                    return f"pipewire/{fields[1]}"
    return None


def cdrdao_device(device):
    if system_name() != "darwin":
        return device
    cdrdao = tool("cdrdao")
    for attempt in range(2):
        result = subprocess.run([cdrdao, "scanbus"], capture_output=True, text=True, check=False, timeout=10)
        for line in (result.stdout + result.stderr).splitlines():
            if "IODVDServices" in line and " : " in line:
                return line.split(" : ", 1)[0].strip()
        if attempt == 0 and device:
            subprocess.run(
                ["/usr/sbin/diskutil", "unmountDisk", "force", device],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
    raise RuntimeError("cdrdao could not find the macOS optical writer")


def unmount_device(device):
    """Release an auto-mounted optical volume before handing the raw device to a
    writer or ripper. Media stays loaded."""
    if not device:
        return True

    if system_name() == "darwin":
        disk = re.sub(r"^/dev/r", "/dev/", device)  # diskutil wants the block node
        subprocess.run(
            ["/usr/sbin/diskutil", "unmountDisk", "force", disk],
            capture_output=True, text=True, check=False, timeout=30,
        )
        return True

    if system_name() != "linux":
        return True

    udisksctl = shutil.which("udisksctl")
    if udisksctl:
        try:
            result = subprocess.run(
                [udisksctl, "unmount", "--block-device", device],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            if result.returncode == 0:
                return True
        except (OSError, subprocess.TimeoutExpired):
            pass

    try:
        mounts = subprocess.run(
            ["findmnt", "--source", device, "--output", "TARGET", "--noheadings", "--raw"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Could not inspect optical mount: {exc}") from exc

    if mounts.returncode not in (0, 1):
        detail = (mounts.stderr or mounts.stdout or "findmnt failed").strip()
        raise RuntimeError(f"Could not inspect optical mount: {detail}")

    targets = [line.strip() for line in mounts.stdout.splitlines() if line.strip()]
    for target in reversed(targets):
        result = subprocess.run(
            ["umount", "--", target],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "umount failed").strip()
            raise RuntimeError(f"Could not unmount optical disc: {detail}")

    return True


def eject_device(device, close=False):
    system = system_name()
    if system == "linux":
        command = ["eject"]
        if close:
            command.append("-t")
        command.append(device)
    elif system == "darwin":
        # `drutil tray open` is a no-op on slot-load drives; `drutil eject`
        # works on both. `diskutil eject` is the fallback for a mounted disc.
        if close:
            commands = [["/usr/bin/drutil", "tray", "close"]]
        else:
            commands = [["/usr/bin/drutil", "eject"]]
            if device:
                commands.append(["/usr/sbin/diskutil", "eject", device])
        for command in commands:
            try:
                if subprocess.run(command, capture_output=True, text=True, timeout=10).returncode == 0:
                    return True
            except (OSError, subprocess.TimeoutExpired):
                pass
        return False
    else:
        raise RuntimeError("Automatic optical-drive eject is not implemented on Windows")
    return subprocess.run(command, capture_output=True, text=True, timeout=10).returncode == 0
