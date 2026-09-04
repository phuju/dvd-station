#!/usr/bin/env python3
import datetime
import json
import os
import re
import serial
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path
from queue import Empty, Queue

import discstation_host

CLEANUP_DAYS = int(os.environ.get("DISC_CLEANUP_DAYS", "2"))


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)


class CancelError(Exception):
    pass


SERIAL_WRITE_LOCK = threading.Lock()

# Shared "last time we actually heard from the ESP32" timestamp. Every place
# that reads a line off the serial port (station_loop's main loop, eject's
# tray-wait loop, burn/rip/play sub-loops, wait_for_burn_confirm, etc.) marks
# this on receipt of ANY line. The main watchdog in station_loop checks the
# age of this shared value instead of a loop-local variable, so a long
# blocking call (e.g. waiting up to 60s for the user to close the tray)
# can't make the watchdog think the ESP32 went silent the instant that call
# returns, even though it was responding the whole time.
_SERIAL_ACTIVITY_LOCK = threading.Lock()
_last_serial_activity = time.monotonic()
_serial_write_failed = False


def note_serial_activity():
    global _last_serial_activity
    with _SERIAL_ACTIVITY_LOCK:
        _last_serial_activity = time.monotonic()


def serial_activity_age():
    with _SERIAL_ACTIVITY_LOCK:
        return time.monotonic() - _last_serial_activity


def reset_serial_state():
    global _serial_write_failed
    _serial_write_failed = False
    note_serial_activity()


def serial_write_failed():
    return _serial_write_failed


_USB_OPTICAL_TOKENS = (
    "slim", "dvd", "mediatek", "asus", "sdrw", "yzwy", "disk",
    "ugreen", "asmedia", "ihas", "atapi", "optiarc", "lite-on", "liteon",
    "hl-dt-st", "tsstcorp", "pioneer", "plextor", "nec", "bd-re", "blu-ray",
    "sata bridge", "storage device", "external", "optical",
)
_USB_OPTICAL_VIDS = {"174c", "152d", "0480", "1c6b", "13fd", "04e8", "05e3", "357d"}


def _read_attr(directory, name):
    try:
        return (directory / name).read_text().strip()
    except OSError:
        return ""


def _run_usb_reset_hook(hook, device, vid, pid, busnode):
    cmd = hook.format(dev=device, vid=vid, pid=pid, busnode=str(busnode))
    print(f"reset_drive: running DISCSTATION_USB_RESET_CMD: {cmd}")
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"reset_drive: hook failed: {e}")
        return False
    if r.returncode != 0:
        print(f"reset_drive: hook exited {r.returncode}: {r.stderr.strip()}")
        return False
    time.sleep(6)
    return True


def _toggle_usb_authorized(auth_path, label):
    try:
        with open(auth_path, "w") as f:
            f.write("0\n")
        time.sleep(2)
        with open(auth_path, "w") as f:
            f.write("1\n")
        time.sleep(6)
        print(f"reset_drive: re-authorized USB node {auth_path.parent} ({label})")
        return True
    except PermissionError:
        user = os.environ.get("USER", "the service user")
        print(f"reset_drive: no permission to re-authorize USB node {auth_path.parent} "
              f"({label}); install a udev rule granting '{user}' write access to that "
              f"node's 'authorized', or set DISCSTATION_USB_RESET_CMD to a privileged helper")
        return False
    except OSError as e:
        print(f"reset_drive: failed to toggle {auth_path}: {e}")
        return False


def reset_drive(device=None):
    """Power-cycle the USB optical enclosure by toggling its sysfs 'authorized'
    flag (or via DISCSTATION_USB_RESET_CMD). Best-effort; returns True on a
    completed toggle/hook, False otherwise."""
    if discstation_host.system_name() == "darwin":
        # ponytail: no USB re-enumeration on macOS; an eject/reload is the only
        # soft reset available and it drops whatever disc is loaded.
        for cmd in (["/usr/bin/drutil", "eject"], ["/usr/bin/drutil", "tray", "close"]):
            subprocess.run(cmd, capture_output=True, timeout=15, check=False)
        return True
    if discstation_host.system_name() != "linux":
        return False
    if not device:
        try:
            device = disc_device()
        except Exception:
            device = "/dev/sr0"
    hook = os.environ.get("DISCSTATION_USB_RESET_CMD")

    # Primary: walk sysfs up from the block device to its USB device node.
    try:
        cur = (Path("/sys/block") / Path(device).name / "device").resolve()
        for _ in range(12):
            if (cur / "idVendor").is_file() and (cur / "authorized").is_file():
                vid = _read_attr(cur, "idVendor")
                pid = _read_attr(cur, "idProduct")
                label = f"{vid}:{pid} {_read_attr(cur, 'product')!r}"
                if hook:
                    return _run_usb_reset_hook(hook, device, vid, pid, cur)
                return _toggle_usb_authorized(cur / "authorized", label)
            if cur.parent == cur or str(cur) in ("/sys", "/"):
                break
            cur = cur.parent
    except OSError:
        pass

    # Fallback: scan every USB device, match on identity strings / known VIDs.
    for auth in Path("/sys/bus/usb/devices").glob("*/authorized"):
        parent = auth.parent
        vid = _read_attr(parent, "idVendor").lower()
        pid = _read_attr(parent, "idProduct")
        product = _read_attr(parent, "product")
        haystack = f"{product} {_read_attr(parent, 'manufacturer')}".lower()
        if vid in _USB_OPTICAL_VIDS or any(tok in haystack for tok in _USB_OPTICAL_TOKENS):
            label = f"{vid}:{pid} {product!r}"
            if hook:
                return _run_usb_reset_hook(hook, device, vid, pid, parent)
            if _toggle_usb_authorized(auth, label):
                return True
    print("reset_drive: no matching USB optical device found to reset")
    return False

def _esp32_port_from_sysfs():
    """Find the ESP32's USB serial port by matching VID/PID 303a:1001
    in sysfs. Returns the device path (e.g. /dev/ttyACM0) or None."""
    for tty in Path("/sys/class/tty").glob("ttyACM*"):
        uevent = tty / "device" / "uevent"
        if uevent.exists():
            modalias = uevent.read_text()
            if "303a/1001" in modalias or "303a:1001" in modalias:
                dev = Path("/dev") / tty.name
                if dev.exists():
                    return str(dev)
    return None


def detect_esp32_port():
    return discstation_host.serial_port() or ""

PORT = detect_esp32_port()
BAUD   = 115200


def check_cancel(ser):
    try:
        if ser and ser.in_waiting:
            line = ser.readline().decode(errors="ignore").strip()
            note_serial_activity()
            return line in ("CANCEL", "PLAY_STOP")
    except (serial.SerialException, OSError):
        pass
    return False


def iter_proc_or_cancel(proc, ser):
    lines = Queue()
    finished = object()

    def read_output():
        try:
            for line in proc.stdout:
                lines.put(line.rstrip("\r\n"))
        finally:
            lines.put(finished)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    last_ping = time.time()
    output_done = False
    while proc.poll() is None or not output_done:
        if time.time() - last_ping >= 5:
            last_ping = time.time()
            send(ser, "PING")

        if check_cancel(ser):
            stop_process(proc)
            return
        try:
            line = lines.get(timeout=0.2)
        except Empty:
            continue
        if line is finished:
            output_done = True
        else:
            yield line
    reader.join(timeout=1)


DVD_DEVICE = os.environ.get("DISC_DEVICE") or os.environ.get("DVD_DEVICE")
DISC_SPEED = os.environ.get("DISC_SPEED")
DISC_DISC_BYTES = 4_700_000_000
DVD_DL_BYTES = 8_500_000_000
DISC_TARGET_BYTES = int(os.environ.get("DISC_TARGET_BYTES", "4300000000"))
DVD_MUX_SAFETY = float(os.environ.get("DVD_MUX_SAFETY", "0.92"))
AUDIO_BITRATE_K = int(os.environ.get("DVD_AUDIO_KBPS", "192"))
MIN_VIDEO_BITRATE_K = 500
MAX_VIDEO_BITRATE_K = 7150
MAX_VIDEO_PEAK_K = 9000
YTDLP_FORMAT = os.environ.get(
    "YTDLP_FORMAT",
    "bestvideo[height<=720][vcodec^=avc1][ext=mp4]+bestaudio[acodec^=mp4a][ext=m4a]/"
    "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/"
    "best[height<=720][ext=mp4]/best[ext=mp4]/bv*+ba/b",
)
YTDLP_PLAYER_CLIENTS = tuple(
    client.strip()
    for client in os.environ.get("YTDLP_PLAYER_CLIENTS", "web_embedded,android_vr").split(",")
    if client.strip()
)
YTDLP_HTTP_CHUNK_SIZE = os.environ.get("YTDLP_HTTP_CHUNK_SIZE", "1M")
YTDLP_RETRIES = os.environ.get("YTDLP_RETRIES", "3")
YTDLP_FRAGMENT_RETRIES = os.environ.get("YTDLP_FRAGMENT_RETRIES", "3")
YTDLP_COOKIES = os.environ.get("YTDLP_COOKIES")
YTDLP_COOKIES_FROM_BROWSER = os.environ.get("YTDLP_COOKIES_FROM_BROWSER")
YTDLP_USER_AGENT = os.environ.get("YTDLP_USER_AGENT")
YTDLP_PO_TOKEN = os.environ.get("YTDLP_PO_TOKEN")
YTDLP_EXTRACTOR_ARGS = os.environ.get("YTDLP_EXTRACTOR_ARGS")
DISC_OUTPUT_LIMIT_BYTES = int(
    os.environ.get("DISC_OUTPUT_LIMIT_BYTES", str(DISC_TARGET_BYTES))
)
DISC_DL_OUTPUT_LIMIT_BYTES = int(
    os.environ.get("DISC_DL_OUTPUT_LIMIT_BYTES", "8000000000")
)
MODE_SETTINGS = {
    "AUTO": {
        "target_bytes": DISC_TARGET_BYTES,
        "safety": DVD_MUX_SAFETY,
        "audio_k": AUDIO_BITRATE_K,
        "min_video_k": MIN_VIDEO_BITRATE_K,
        "max_video_k": MAX_VIDEO_BITRATE_K,
        "peak_video_k": MAX_VIDEO_PEAK_K,
        "burn": True,
    },
    "BEST": {
        "target_bytes": int(os.environ.get("DISC_BEST_TARGET_BYTES", "4450000000")),
        "safety": float(os.environ.get("DISC_BEST_SAFETY", "0.95")),
        "audio_k": int(os.environ.get("DISC_BEST_AUDIO_KBPS", "224")),
        "min_video_k": 700,
        "max_video_k": 8000,
        "peak_video_k": 9000,
        "burn": True,
    },
    "LONG": {
        "target_bytes": int(os.environ.get("DISC_LONG_TARGET_BYTES", "4300000000")),
        "safety": float(os.environ.get("DISC_LONG_SAFETY", "0.90")),
        "audio_k": int(os.environ.get("DISC_LONG_AUDIO_KBPS", "128")),
        "min_video_k": 350,
        "max_video_k": 3500,
        "peak_video_k": 6000,
        "burn": True,
    },
    "TEST": {
        "target_bytes": DISC_TARGET_BYTES,
        "safety": DVD_MUX_SAFETY,
        "audio_k": AUDIO_BITRATE_K,
        "min_video_k": MIN_VIDEO_BITRATE_K,
        "max_video_k": MAX_VIDEO_BITRATE_K,
        "peak_video_k": MAX_VIDEO_PEAK_K,
        "burn": False,
    },
}

USER_HOME = discstation_host.user_home()
WORK = discstation_host.cache_dir()

def cleanup_old_jobs():
    cutoff = time.time() - CLEANUP_DAYS * 86400
    if not WORK.is_dir():
        return
    removed = 0
    for entry in WORK.iterdir():
        if entry.name.startswith("job_") and entry.is_dir():
            try:
                mtime = entry.stat().st_mtime
                if mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
                    removed += 1
            except OSError:
                pass
    if removed:
        print(f"Cleaned up {removed} old job(s) (> {CLEANUP_DAYS}d)")

def tool(name):
    return discstation_host.tool(name)

def js_runtime_arg():
    override = os.environ.get("YTDLP_JS_RUNTIME")
    if override:
        return ["--js-runtimes", override]
    for name in ("node", "/opt/homebrew/bin/node", "/usr/local/bin/node", "/usr/bin/node", "/bin/node"):
        path = shutil.which(name) if not name.startswith("/") else name
        if path and Path(path).exists():
            return ["--js-runtimes", f"node:{path}"]
    return []

def remote_components_arg():
    value = os.environ.get("YTDLP_REMOTE_COMPONENTS", "ejs:github")
    if value.lower() in ("", "0", "false", "none", "off"):
        return []
    return ["--remote-components", value]

def ffmpeg_location_arg():
    try:
        return ["--ffmpeg-location", str(Path(tool("ffmpeg")).parent)]
    except FileNotFoundError:
        return []

def ytdlp_base_args(player_client=None):
    args = [tool('yt-dlp'), "--no-playlist", *js_runtime_arg(), *remote_components_arg(), *ffmpeg_location_arg()]
    if player_client or YTDLP_PO_TOKEN or YTDLP_EXTRACTOR_ARGS:
        extractor_args = []
        if player_client:
            extractor_args.append(f"youtube:player_client={player_client}")
        if YTDLP_PO_TOKEN:
            extractor_args.append(f"youtube:po_token={YTDLP_PO_TOKEN}")
        if YTDLP_EXTRACTOR_ARGS:
            extractor_args.append(YTDLP_EXTRACTOR_ARGS)
        args += ["--extractor-args", ";".join(extractor_args)]
    if YTDLP_HTTP_CHUNK_SIZE:
        args += ["--http-chunk-size", YTDLP_HTTP_CHUNK_SIZE]
    args += ["--retries", YTDLP_RETRIES, "--fragment-retries", YTDLP_FRAGMENT_RETRIES]
    if YTDLP_COOKIES:
        args += ["--cookies", YTDLP_COOKIES]
    elif YTDLP_COOKIES_FROM_BROWSER:
        args += ["--cookies-from-browser", YTDLP_COOKIES_FROM_BROWSER]
    if YTDLP_USER_AGENT:
        args += ["--user-agent", YTDLP_USER_AGENT]
    return args

def disc_device():
    return discstation_host.disc_device()

def _udevadm_props(device):
    # Prefer the shared implementation in discstation (pyudev-backed, with a
    # cdrom_id refresh). Lazy import to avoid the import cycle with discstation.
    try:
        from discstation import udev_cdrom_properties
        return udev_cdrom_properties(device)
    except Exception:
        pass
    if discstation_host.system_name() != "linux":
        return discstation_host.media_properties(device)
    try:
        r = subprocess.run(
            ["udevadm", "info", "--query=property", "--name", device],
            capture_output=True, text=True, timeout=3,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return {}
    props = {}
    for line in r.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            props[k] = v
    return props


def disc_capacity_bytes(device):
    override = os.environ.get("DISC_DISC_BYTES")
    if override:
        try:
            return int(override)
        except ValueError:
            pass

    if discstation_host.system_name() != "linux":
        return discstation_host.media_capacity_bytes(device)
    props = _udevadm_props(device)
    is_dl = (
        props.get("ID_CDROM_MEDIA_DVD_PLUS_R_DL") == "1" or
        props.get("ID_CDROM_MEDIA_DVD_R_DL") == "1" or
        props.get("ID_CDROM_MEDIA_DVD_R_DL_SEQ") == "1"
    )
    expected_min = 1_000_000_000
    expected_max = DVD_DL_BYTES if is_dl else DISC_DISC_BYTES

    mediainfo_timeout = _env_int("DISCSTATION_PROBE_TIMEOUT_MEDIAINFO", 12)
    best = None
    for attempt in range(3):
        try:
            r = subprocess.run(
                ["dvd+rw-mediainfo", device],
                capture_output=True, text=True, timeout=mediainfo_timeout,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            time.sleep(0.5)
            continue
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                line = line.strip()
                if "Free Blocks:" in line:
                    parts = line.split()
                    if len(parts) >= 3:
                        try:
                            blocks = int(parts[2].split("*")[0])
                            cap = blocks * 2048
                            if best is None or cap > best:
                                best = cap
                        except (ValueError, IndexError):
                            pass
        if best is not None and best > expected_max // 2:
            break
        time.sleep(0.5)

    if best is not None:
        if best > expected_max + 100_000_000:
            best = expected_max
        elif best < expected_min:
            best = None
        elif is_dl and best < DISC_DISC_BYTES:
            best = None

    if best is not None:
        return best

    if props.get("ID_CDROM_MEDIA_STATE") == "blank":
        if is_dl:
            return DVD_DL_BYTES
    if is_dl:
        return DVD_DL_BYTES
    if props.get("ID_CDROM_MEDIA_DVD_PLUS_R") == "1" or \
       props.get("ID_CDROM_MEDIA_DVD_R") == "1":
        return DISC_DISC_BYTES

    # Last resort: a raw block size (works for finalized/pressed discs where
    # dvd+rw-mediainfo reports no free blocks; 0/absent for audio CDs).
    try:
        r = subprocess.run(["blockdev", "--getsize64", device],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            val = int(r.stdout.strip())
            if val >= expected_min:
                return min(val, expected_max)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
        pass
    return None

def detect_disc_type(device):
    props = _udevadm_props(device)
    is_dl = (
        props.get("ID_CDROM_MEDIA_DVD_PLUS_R_DL") == "1" or
        props.get("ID_CDROM_MEDIA_DVD_R_DL") == "1" or
        props.get("ID_CDROM_MEDIA_DVD_R_DL_SEQ") == "1"
    )
    is_sl = (
        props.get("ID_CDROM_MEDIA_DVD_PLUS_R") == "1" or
        props.get("ID_CDROM_MEDIA_DVD_R") == "1"
    )
    is_blank = props.get("ID_CDROM_MEDIA_STATE") == "blank"
    media_type = props.get("ID_CDROM_MEDIA", "")

    status = "blank" if is_blank else props.get("ID_CDROM_MEDIA_STATE", "unknown")

    capacity = disc_capacity_bytes(device)
    if capacity is None:
        capacity = DVD_DL_BYTES if is_dl else (DISC_DISC_BYTES if is_sl else None)

    return {
        "is_dual_layer": is_dl,
        "is_single_layer": is_sl,
        "is_blank": is_blank,
        "status": status,
        "media_type": media_type,
        "capacity": capacity,
    }

# discstation.py sets this to _record_web_status so every serial line the burn
# pipeline emits also updates the web/SSE status in real time.
status_sink = None


def send(ser, msg):
    global _serial_write_failed
    if os.environ.get("DISCSTATION_DEBUG_SERIAL"):
        print(f"[{time.time():.3f}] SEND {msg!r}", flush=True)
    if status_sink is not None:
        try:
            status_sink(msg)
        except Exception:
            pass
    if not ser:
        return False
    try:
        with SERIAL_WRITE_LOCK:
            ser.write((msg + '\n').encode())
    except (serial.SerialException, OSError) as e:
        if not _serial_write_failed:
            print(f"serial send error ('{msg[:30]}'): {e}")
        _serial_write_failed = True
        return False
    except Exception as e:
        print(f"serial send error ('{msg[:30]}'): {e}")
        return False
    return True

def safe_send(ser, msg):
    if not ser:
        if status_sink is not None:
            try:
                status_sink(msg)
            except Exception:
                pass
        return False
    try:
        return send(ser, msg)
    except Exception:
        return False

def stop_process(proc):
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()

def normalize_mode(mode):
    mode = (mode or "AUTO").strip().upper()
    return mode if mode in MODE_SETTINGS else "AUTO"

def probe_duration(infile):
    r = subprocess.run(
        [tool('ffprobe'), '-v', 'quiet', '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1', str(infile)],
        capture_output=True, text=True)
    return float(r.stdout.strip()) if r.stdout.strip() else 0



def disc_output_limit_bytes(disc_bytes=None):
    """Return the conservative payload limit used before authoring/burning."""
    if disc_bytes and disc_bytes > 6_000_000_000:
        return min(int(disc_bytes), DISC_DL_OUTPUT_LIMIT_BYTES)
    if disc_bytes:
        return min(int(disc_bytes), DISC_OUTPUT_LIMIT_BYTES)
    return DISC_OUTPUT_LIMIT_BYTES


def bitrate_plan(duration, mode="AUTO", disc_bytes=None):
    mode = normalize_mode(mode)
    settings = MODE_SETTINGS[mode]

    # Use detected capacity when available, but never exceed the configured
    # conservative payload limit for the disc layer.
    target_bytes = disc_bytes if disc_bytes else settings["target_bytes"]
    target_bytes = min(target_bytes, disc_output_limit_bytes(disc_bytes))

    if duration <= 0:
        return {
            "mode": mode,
            "video_k": min(3600, settings["max_video_k"]),
            "audio_k": settings["audio_k"],
            "max_video_k": settings["max_video_k"],
            "peak_video_k": settings.get("peak_video_k", settings["max_video_k"]),
            "burn": settings["burn"],
        }

    usable_bits = target_bytes * 8 * settings["safety"]
    total_kbps = usable_bits / duration / 1000
    video_kbps = int(total_kbps - settings["audio_k"])

    if video_kbps < settings["min_video_k"]:
        raise RuntimeError(f"Video too long for {mode}")

    return {
        "mode": mode,
        "video_k": min(video_kbps, settings["max_video_k"]),
        "audio_k": settings["audio_k"],
        "max_video_k": settings["max_video_k"],
        "peak_video_k": settings.get("peak_video_k", settings["max_video_k"]),
        "burn": settings["burn"],
    }

def tree_size(path):
    return sum(p.stat().st_size for p in Path(path).rglob("*") if p.is_file())


def check_encoded_size(ser, mpg, disc_bytes=None):
    size = Path(mpg).stat().st_size
    limit = disc_output_limit_bytes(disc_bytes)
    send(ser, f"PROGRESS:{size / 1_000_000_000:.2f}GB / {limit / 1_000_000_000:.2f}GB")
    if size > limit:
        raise RuntimeError(
            f"Video output {size / 1_000_000_000:.2f}GB exceeds safe limit "
            f"{limit / 1_000_000_000:.2f}GB"
        )
    return size

def sanitize_disc_label(title):
    label = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    label = re.sub(r"[^A-Za-z0-9]+", "_", label.upper()).strip("_")
    return (label or "DVD_VIDEO")[:32]


def audio_disc_title(title):
    label = unicodedata.normalize("NFKC", str(title or ""))
    label = re.sub(r"[\x00-\x1f\x7f\"]", " ", label)
    label = " ".join(label.split())
    return label[:64] or "Audio CD"


def cdrdao_text(value):
    return audio_disc_title(value).replace("\\", "\\\\").replace('"', '\\"')

def format_duration(seconds):
    if not seconds or seconds <= 0:
        return "Dur unknown"

    total_minutes = int(round(seconds / 60))
    hours = total_minutes // 60
    minutes = total_minutes % 60

    if hours:
        return f"Dur {hours}h{minutes:02d}m"
    return f"Dur {minutes}m"

def preflight_lines(duration, disc_bytes=None):
    duration_line = format_duration(duration)

    label = "DVD"
    if disc_bytes:
        if disc_bytes < 1_500_000_000:
            label = "CD"
        elif disc_bytes > 6_000_000_000:
            label = "DVD9"
        else:
            label = "DVD5"

    if not duration or duration <= 0:
        return duration_line, f"{label} estimate unknown", True

    ok_modes = []
    for mode in ("AUTO", "BEST", "LONG"):
        try:
            ok_modes.append(bitrate_plan(duration, mode, disc_bytes))
        except RuntimeError:
            pass

    if not ok_modes:
        return duration_line, f"Too long for {label}", False

    auto_plan = next((plan for plan in ok_modes if plan["mode"] == "AUTO"), None)
    if auto_plan:
        return duration_line, f"{label} OK {auto_plan['video_k']}k", True

    return duration_line, "Use LONG mode", True

VIDEO_EXTS = {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.m4v', '.mpg', '.mpeg', '.vob', '.ts', '.webm', '.ogv'}


def is_local_file(path):
    return Path(path).is_file()


def find_video_files(path):
    p = Path(path)
    if p.is_file():
        return [p] if p.suffix.lower() in VIDEO_EXTS else []
    if p.is_dir():
        files = sorted(p.iterdir())
        videos = [f for f in files if f.is_file() and f.suffix.lower() in VIDEO_EXTS]
        return videos
    return []


def get_local_video_info(path):
    p = Path(path)
    dur = probe_duration(p)
    stem = p.stem.replace("_", " ").title() or "Local file"
    return {"title": stem, "duration": dur}


def get_video_info(source):
    p = Path(source)
    if p.is_dir():
        videos = find_video_files(source)
        if not videos:
            raise RuntimeError(f"No video files found in directory: {source}")
        total_dur = sum(probe_duration(v) for v in videos)
        return {"title": p.name.replace("_", " ").title(), "duration": total_dur, "files": videos}
    if is_local_file(source):
        return get_local_video_info(source)
    errors = []
    for player_client in YTDLP_PLAYER_CLIENTS or (None,):
        r = subprocess.run(
            [*ytdlp_base_args(player_client), '--dump-single-json', '--skip-download', source],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            errors.append(r.stderr.strip() or f"{player_client or 'default'} client failed")
            continue
        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError as e:
            errors.append(f"Could not parse video info: {e}")
            continue
        return {
            "title": data.get("title") or "Untitled video",
            "duration": float(data.get("duration") or 0),
        }
    detail = next((error for error in reversed(errors) if error), "Could not get video info")
    raise RuntimeError(detail[:300])

def _start_keepalive(ser):
    """Start a background PING sender. The ESP32 firmware treats any
    received message as a heartbeat (lastMsgTime), and shows itself as
    disconnected after PING_TIMEOUT_MS (30s) of silence. Any blocking
    operation longer than that — a big file copy, an ffmpeg concat —
    needs one of these running, or the OLED will flip to "disconnected"
    partway through even though nothing is actually wrong.
    Returns (stop_event, thread); caller must stop_event.set() and
    thread.join() when the blocking operation finishes."""
    stop_ping = threading.Event()
    def _ping_thread():
        while not stop_ping.is_set():
            try:
                send(ser, "PING")
            except Exception:
                pass
            stop_ping.wait(5)
    pt = threading.Thread(target=_ping_thread, daemon=True)
    pt.start()
    return stop_ping, pt

def copy_with_keepalive(ser, src, dest, base_pct=0, pct_span=100):
    """Chunked copy with PROGRESS updates and a keepalive ping thread,
    so large copies don't sit silent long enough to trip the ESP32's
    connection watchdog. base_pct/pct_span let callers map one file's
    progress into a slice of an overall multi-file progress range."""
    src_size = src.stat().st_size
    copied = 0
    last_beat = time.time()
    stop_ping, pt = _start_keepalive(ser)
    try:
        with open(str(src), 'rb') as fin, open(str(dest), 'wb') as fout:
            while True:
                if check_cancel(ser):
                    fout.close()
                    dest.unlink(missing_ok=True)
                    safe_send(ser, "CANCELLED:Copy cancelled")
                    raise CancelError("Cancelled")
                chunk = fin.read(1024 * 1024)
                if not chunk:
                    break
                fout.write(chunk)
                copied += len(chunk)
                now = time.time()
                if now - last_beat >= 5:
                    last_beat = now
                    frac = min(copied / src_size, 1.0) if src_size else 1.0
                    pct = min(int(base_pct + frac * pct_span), 99)
                    send(ser, f"PROGRESS:{pct}%")
    except (KeyboardInterrupt, SystemExit):
        dest.unlink(missing_ok=True)
        raise
    finally:
        stop_ping.set()
        pt.join(timeout=3)

def concat_videos(ser, files, dest):
    if len(files) == 1:
        files[0].replace(dest)
        return dest
    flist = dest.parent / "concat.txt"
    flist.write_text("".join(f"file '{f.resolve()}'\n" for f in files))
    send(ser, "STATUS:Merging video files...")
    stop_ping, pt = _start_keepalive(ser)
    try:
        subprocess.run(
            [tool('ffmpeg'), '-y', '-f', 'concat', '-safe', '0', '-i', str(flist),
             '-c', 'copy', str(dest)],
            capture_output=True)
    finally:
        stop_ping.set()
        pt.join(timeout=3)
    flist.unlink()
    for f in files:
        f.unlink()
    return dest


def download(ser, source, job_dir):
    p = Path(source)
    if p.is_dir():
        videos = find_video_files(source)
        if not videos:
            raise RuntimeError(f"No video files in directory: {source}")
        download_dir = job_dir / "download"
        download_dir.mkdir(parents=True, exist_ok=True)
        send(ser, "STATUS:Copying files...")
        dest = download_dir / f"{p.name}.mp4"
        copies = []
        n = len(videos)
        for i, v in enumerate(videos):
            c = download_dir / v.name
            copy_with_keepalive(ser, v, c, base_pct=int(i * 100 / n), pct_span=100 / n)
            copies.append(c)
        for srt in sorted(p.glob("*.srt")):
            shutil.copy2(str(srt), str(download_dir / srt.name))
        return concat_videos(ser, copies, dest)

    if is_local_file(source):
        src = Path(source)
        download_dir = job_dir / "download"
        download_dir.mkdir(parents=True, exist_ok=True)
        dest = download_dir / src.name
        send(ser, "STATUS:Copying...")
        time.sleep(0.3)
        try:
            copy_with_keepalive(ser, src, dest)
        except (KeyboardInterrupt, SystemExit):
            raise
        for srt in sorted(src.parent.glob("*.srt")):
            shutil.copy2(str(srt), str(download_dir / srt.name))
        return dest

    send(ser, "STATUS:Downloading...")
    download_dir = job_dir / "download"
    download_dir.mkdir(parents=True, exist_ok=True)
    out = str(download_dir / "%(title)s.%(ext)s")
    all_lines = []
    errors = []
    clients = YTDLP_PLAYER_CLIENTS or (None,)
    for attempt, player_client in enumerate(clients, start=1):
        if attempt > 1:
            for existing in download_dir.iterdir():
                if existing.is_file():
                    existing.unlink(missing_ok=True)

        proc = subprocess.Popen(
            [*ytdlp_base_args(player_client), '-f', YTDLP_FORMAT, '-o', out, source],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        last_prog = 0
        attempt_lines = []
        try:
            for line in iter_proc_or_cancel(proc, ser):
                attempt_lines.append(line)
                print(f"yt-dlp: {line}")
                m = re.search(r'(\d+\.\d+)%', line)
                if m:
                    now = time.time()
                    if now - last_prog >= 0.2:
                        send(ser, f"PROGRESS:{m.group(1)}%")
                        last_prog = now
                if 'Merging' in line:
                    send(ser, "INFO:Merging streams...")
        except (KeyboardInterrupt, SystemExit):
            stop_process(proc)
            raise
        rc = proc.wait()
        all_lines += [f"[player_client={player_client or 'default'}]", *attempt_lines]
        if rc == 0:
            files = [p for p in download_dir.iterdir()
                     if p.is_file() and not p.name.endswith(('.part', '.ytdl'))]
            if files:
                (job_dir / "yt-dlp.log").write_text("\n".join(all_lines) + "\n")
                return max(files, key=lambda p: p.stat().st_mtime)
            errors.append("No downloaded file")
            continue
        if proc.returncode == -15:
            (job_dir / "yt-dlp.log").write_text("\n".join(all_lines) + "\n")
            safe_send(ser, "CANCELLED:Download cancelled")
            raise CancelError("Cancelled")
        detail = next(
            (line.strip() for line in reversed(attempt_lines)
             if "error" in line.lower() or line.startswith("ERROR:")),
            "yt-dlp exited unsuccessfully",
        )
        errors.append(f"{player_client or 'default'}: {detail[:180]}")

    (job_dir / "yt-dlp.log").write_text("\n".join(all_lines) + "\n")
    for partial in download_dir.glob("*.part"):
        partial.unlink(missing_ok=True)
    detail = " | ".join(errors) if errors else "yt-dlp exited unsuccessfully"
    raise RuntimeError(f"Download failed: {detail[:300]}")


def find_subtitle_files(video_path):
    p = Path(video_path)
    srt_files = sorted(p.parent.glob("*.srt"))
    # also check for .srt with same stem
    same_stem = p.parent.glob(f"{p.stem}.*.srt")
    for f in same_stem:
        if f not in srt_files:
            srt_files.append(f)
    eng = p.parent.glob("*.eng.srt")
    for f in eng:
        if f not in srt_files:
            srt_files.append(f)
    return srt_files


def extract_embedded_subtitles(video_path, job_dir):
    sub_dir = job_dir / "subtitles"
    sub_dir.mkdir(exist_ok=True)
    r = subprocess.run(
        [tool('ffmpeg'), '-i', str(video_path)],
        capture_output=True, text=True)
    count = 0
    for line in r.stderr.split('\n'):
        if 'Subtitle:' in line:
            count += 1
    extracted = []
    for i in range(count):
        out = sub_dir / f"sub_{i}.srt"
        subprocess.run(
            [tool('ffmpeg'), '-y', '-i', str(video_path),
             '-map', f'0:s:{i}', str(out)],
            capture_output=True)
        if out.exists() and out.stat().st_size > 10:
            extracted.append(out)
    return extracted


def add_subtitles(ser, mpg_path, srt_files, job_dir):
    if not srt_files:
        return mpg_path
    send(ser, "STATUS:Adding subtitles...")
    out_path = job_dir / "movie_subbed.mpg"
    streams = ""
    for srt in srt_files:
        streams += f'''
    <textsub filename="{srt}" characterset="UTF-8"
            fontsize="28" font="sans-serif"
            horizontal-align="center" vertical-align="bottom"
            left-margin="20" right-margin="20" top-margin="20" bottom-margin="30"/>'''
    xml = f'<subpictures><stream>{streams}\n  </stream>\n</subpictures>\n'
    xml_path = job_dir / "spumux.xml"
    xml_path.write_text(xml)
    with open(mpg_path, 'rb') as fin:
        with open(out_path, 'wb') as fout:
            r = subprocess.run(
                [tool('spumux'), '-m', 'dvd', '-s', '0', str(xml_path)],
                stdin=fin, stdout=fout, stderr=subprocess.PIPE)
    if r.returncode != 0:
        print(f"spumux error: {r.stderr.decode(errors='ignore')}")
        safe_send(ser, "WARNING:Subtitle failed, continuing")
        if out_path.exists():
            out_path.unlink()
        return mpg_path
    if out_path.exists() and out_path.stat().st_size > 0:
        mpg_path.unlink()
        return out_path
    return mpg_path


def probe_aspect(infile):
    r = subprocess.run(
        [tool('ffprobe'), '-v', 'error',
         '-select_streams', 'v:0',
         '-show_entries', 'stream=width,height,display_aspect_ratio',
         '-of', 'csv=p=0', str(infile)],
        capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        return None
    parts = r.stdout.strip().split(',')
    if len(parts) < 2:
        return None
    try:
        w, h = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    dar = parts[2] if len(parts) > 2 and parts[2] else ""
    if dar:
        try:
            n, d = dar.split(":")
            return float(n) / float(d)
        except (ValueError, ZeroDivisionError):
            pass
    return w / h if h else None


def _run_ffmpeg_pass(ser, cmd, total, pass_label):
    send(ser, f"INFO:{pass_label}")
    proc = subprocess.Popen(cmd,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    last_prog = 0
    try:
        for line in iter_proc_or_cancel(proc, ser):
            m = re.search(r'time=(\d+):(\d+):(\d+\.\d+)', line)
            if m and total > 0:
                now = time.time()
                if now - last_prog >= 0.2:
                    secs = int(m.group(1))*3600 + int(m.group(2))*60 + float(m.group(3))
                    pct = min(int(secs / total * 100), 99)
                    send(ser, f"PROGRESS:{pct}%")
                    last_prog = now
    except (KeyboardInterrupt, SystemExit):
        stop_process(proc)
        raise
    if proc.wait() != 0:
        if proc.returncode == -15:
            safe_send(ser, "CANCELLED:Convert cancelled")
            raise CancelError("Cancelled")
        raise RuntimeError("FFmpeg encode failed")


def convert(ser, infile, job_dir, mode, disc_bytes=None):
    send(ser, "STATUS:Converting...")
    out = job_dir / "movie.mpg"
    total = probe_duration(infile)
    plan = bitrate_plan(total, mode, disc_bytes)
    print(f"Convert: mode={mode} dur={total:.0f}s video_k={plan['video_k']}k "
           f"peak_k={plan['peak_video_k']}k audio_k={plan['audio_k']}k "
           f"target_bytes={disc_output_limit_bytes(disc_bytes)}", flush=True)
    send(ser, f"PROGRESS:{plan['mode']} {plan['video_k']}k")
    send(ser, f"INFO:AC3 {plan['audio_k']}k audio")
    aspect = probe_aspect(infile)
    is_wide = aspect is not None and aspect > 1.4
    dvd_aspect = "16:9" if is_wide else "4:3"
    logfile = str(job_dir / "2pass")
    # CBR pinning: b:v, minrate, and maxrate all equal to the planned video
    # bitrate. ffmpeg's native mpeg2video ratecontrol treats -b:v as a ceiling
    # it's free to undershoot, not a promise — the old -maxrate {peak_k}k left
    # a wide gap (e.g. 5515k target vs 9000k peak) that gave it room to do
    # exactly that. Pinning all three together forces it to spend the bitrate
    # the disc-capacity plan actually called for.
    bufsize_k = 1835  # DVD spec VBV buffer (224 KB = 1,835,008 bits) — fixed, not scaled by peak_k
    base = [
        tool('ffmpeg'), '-y', '-i', str(infile),
        '-map', '0:v:0', '-map', '0:a:0?', '-sn',
        '-c:v', 'mpeg2video', '-s', '720x576', '-r', '25', '-g', '15',
        '-aspect', dvd_aspect,
        '-b:v', f"{plan['video_k']}k",
        '-minrate', f"{plan['video_k']}k",
        '-maxrate', f"{plan['video_k']}k",
        '-bufsize', f'{bufsize_k}k',
        '-packetsize', '2048',
    ]
    pass1 = base + ['-pass', '1', '-passlogfile', logfile,
                    '-an', '-f', 'null', discstation_host.null_device()]
    pass2 = base + ['-pass', '2', '-passlogfile', logfile,
                    '-c:a', 'ac3', '-b:a', f"{plan['audio_k']}k", str(out)]
    _run_ffmpeg_pass(ser, pass1, total, "Pass 1/2 (analyze)")
    _run_ffmpeg_pass(ser, pass2, total, "Pass 2/2 (encode)")
    if out.stat().st_size < 100_000_000:
        print(f"WARNING: movie.mpg only {out.stat().st_size} bytes — possible encode failure", flush=True)
    else:
        print(f"movie.mpg: {out.stat().st_size / 1e9:.2f}GB for {total:.0f}s "
              f"({out.stat().st_size * 8 / total / 1000:.0f}k avg bitrate)", flush=True)
    for log_suffix in ('', '.log', '.log.mbtree'):
        p = job_dir / f"2pass{log_suffix}"
        if p.exists():
            p.unlink()
    return out, dvd_aspect

def author(ser, mpg, job_dir, aspect="4:3"):
    send(ser, "STATUS:Authoring DVD...")
    send(ser, "PROGRESS:Building IFO/VOB")
    dvd_dir = job_dir / "dvd_out"
    dvd_dir.mkdir(parents=True, exist_ok=True)
    xml = f"""<dvdauthor dest={chr(34) + str(dvd_dir) + chr(34)} format="pal">
  <vmgm />
  <titleset>
    <titles>
      <pgc>
        <vob file={chr(34) + str(mpg) + chr(34)} />
      </pgc>
    </titles>
  </titleset>
</dvdauthor>"""
    xml_path = job_dir / "dvd.xml"
    with open(xml_path, 'w') as f:
        f.write(xml)
    env = os.environ.copy()
    env['VIDEO_FORMAT'] = 'PAL'
    proc = subprocess.Popen(
        [tool('dvdauthor'), '-x', str(xml_path)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    for line in iter_proc_or_cancel(proc, ser):
        print(line, end="")
    if proc.wait() != 0:
        if proc.returncode == -15:
            safe_send(ser, "CANCELLED:Authoring cancelled")
            raise CancelError("Cancelled")
        raise RuntimeError("DVD authoring failed")
    return dvd_dir

def check_dvd_size(ser, dvd_dir, disc_bytes=None):
    send(ser, "STATUS:Checking size...")
    size = tree_size(dvd_dir)
    limit = disc_output_limit_bytes(disc_bytes)
    send(ser, f"PROGRESS:{size / 1_000_000_000:.2f}GB / {limit / 1_000_000_000:.2f}GB")
    if size > limit:
        raise RuntimeError(
            f"DVD output {size / 1_000_000_000:.2f}GB exceeds safe limit "
            f"{limit / 1_000_000_000:.2f}GB"
        )
    return size

def wait_for_burn_confirm(ser, dvd_dir, disc_capacity):
    data_size = tree_size(dvd_dir)
    detected_capacity = disc_capacity_bytes(disc_device())
    actual_cap = disc_output_limit_bytes(detected_capacity or disc_capacity)
    cap_gb = actual_cap / 1_000_000_000
    data_gb = data_size / 1_000_000_000
    line = f"WAITING:{data_gb:.2f}GB / {cap_gb:.1f}GB"
    send(ser, line)
    last_ping = time.time()
    while True:
        if serial_write_failed():
            raise serial.SerialException("ESP32 serial link lost before burn confirmation")
        if time.time() - last_ping >= 5:
            last_ping = time.time()
            send(ser, "PING")
        try:
            if ser and ser.in_waiting:
                resp = ser.readline().decode(errors="ignore").strip()
                note_serial_activity()
                if resp == "CONFIRM" or resp == "START":
                    return True
                if resp == "CANCEL":
                    raise RuntimeError("Burn cancelled by user")
        except OSError:
            raise RuntimeError("Serial error during burn confirm")
        time.sleep(0.05)

def _is_dvd_plus_rw(device):
    if discstation_host.system_name() != "linux":
        return False
    props = _udevadm_props(device)
    if props.get("ID_CDROM_MEDIA_DVD_PLUS_RW") == "1":
        return True
    try:
        result = subprocess.run(
            ["dvd+rw-mediainfo", device],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return bool(re.search(r"mounted media:.*dvd\+rw", result.stdout + result.stderr, re.IGNORECASE))


def _format_dvd_plus_rw(ser, device):
    if not _is_dvd_plus_rw(device):
        return False
    send(ser, "STATUS:Preparing rewritable disc...")
    proc = subprocess.Popen(
        [tool("dvd+rw-format"), "-force", device],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    out_lines = []
    try:
        for line in iter_proc_or_cancel(proc, ser):
            out_lines.append(line)
            print(f"dvd+rw-format: {line}")
    except (KeyboardInterrupt, SystemExit):
        stop_process(proc)
        raise
    rc = proc.wait()
    if rc == -15:
        safe_send(ser, "CANCELLED:Burn cancelled")
        raise CancelError("Cancelled")
    if rc != 0:
        detail = next((line for line in reversed(out_lines) if line.strip()), "dvd+rw-format failed")
        raise RuntimeError(f"DVD+RW preparation failed: {detail[:120]}")
    return True


def _run_growisofs(ser, growisofs_cmd, log_path, device=None):
    """Shared growisofs runner — unmount, format old DVD+RW media, then write."""
    write_device = device or disc_device()
    out_lines = []
    rc = 1
    for attempt in range(2):
        if discstation_host.system_name() == "linux":
            discstation_host.unmount_device(write_device)
        proc = subprocess.Popen(
            growisofs_cmd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        out_lines = []
        last_prog = 0
        try:
            for line in iter_proc_or_cancel(proc, ser):
                out_lines.append(line)
                m = re.search(r'(\d+\.\d+)%', line)
                if m:
                    now = time.time()
                    if now - last_prog >= 0.2:
                        send(ser, f"PROGRESS:{m.group(1)}%")
                        last_prog = now
        except (KeyboardInterrupt, SystemExit):
            stop_process(proc)
            raise
        rc = proc.wait()
        output_text = "\n".join(out_lines)
        if rc == 0:
            break
        if attempt == 0 and "already carries isofs" in output_text.lower():
            if _format_dvd_plus_rw(ser, write_device):
                continue
        break

    if rc != 0:
        output_text = "\n".join(out_lines)
        with open(log_path, 'w') as f:
            f.write(output_text)
        for line in out_lines[-10:]:
            print(f"growisofs: {line}")
        if rc == -15:
            safe_send(ser, "CANCELLED:Burn cancelled")
            raise CancelError("Cancelled")
        if "no such device" in output_text.lower() or "unable to test unit ready" in output_text.lower():
            raise RuntimeError("Optical drive disconnected during burn; check USB cable/hub")
        if "already carries isofs" in output_text.lower():
            raise RuntimeError("Optical disc still contains an ISO filesystem after preparation")
        if "unable to reload tray" in output_text:
            safe_send(ser, "INFO:Disc written OK (tray reload skipped)")
            print("growisofs: tray reload failed after successful write — disc is fine")
            return
        safe_send(ser, "INFO:Burn failed, check log")
        raise RuntimeError("Disc burn failed")
    try:
        discstation_host.eject_device(write_device)
    except Exception as e:
        print(f"Disc eject skipped: {e}")


def _run_hdiutil_burn(ser, image_path, device=None):
    """Burn a pre-built ISO on macOS via `hdiutil burn -puppetstrings`, streaming
    its PERCENT: lines to the ESP32 for both the write and verify passes."""
    send(ser, "STATUS:Burning image...")
    send(ser, "PROGRESS:0%")
    cmd = discstation_host.iso_burn_command(device or disc_device(), image_path)
    if discstation_host.system_name() == "darwin":
        # hdiutil block-buffers stdout to a pipe -> no progress until it exits.
        # Run it under a pty (script relays the child's exit status verbatim).
        cmd = ["/usr/bin/script", "-q", "/dev/null", *cmd]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out_lines = []
    last_pct = -1
    phase = "burn"
    try:
        for line in iter_proc_or_cancel(proc, ser):
            out_lines.append(line)
            low = line.lower()
            m = re.search(r"PERCENT:(-?[\d.]+)", line)
            pct = int(float(m.group(1))) if m else None
            if phase == "burn" and ("verif" in low or (pct is not None and last_pct > 90 and pct < 5)):
                phase = "verify"
                last_pct = -1
                send(ser, "STATUS:Verifying...")
            if pct is not None and 0 <= pct <= 100 and pct != last_pct:
                last_pct = pct
                send(ser, f"PROGRESS:{min(pct, 99)}%")
    except (KeyboardInterrupt, SystemExit):
        stop_process(proc)
        raise
    rc = proc.wait()
    if rc != 0:
        detail = next((l for l in reversed(out_lines) if l.strip()), "hdiutil burn failed")
        raise RuntimeError(f"Disc burn failed: {detail[:120]}")
    safe_send(ser, "PROGRESS:100%")
    try:
        discstation_host.eject_device(device or disc_device())
    except Exception:
        try:
            discstation_host.eject_device(None)  # device-less `drutil eject`
        except Exception as e:
            print(f"Disc eject skipped: {e}")


def burn(ser, dvd_dir, disc_label, speed=None, is_dual_layer=False):
    if discstation_host.system_name() != "linux":
        WORK.mkdir(parents=True, exist_ok=True)
        image_path = WORK / f"video_{time.strftime('%Y%m%d_%H%M%S')}.iso"
        try:
            discstation_host.build_data_image([dvd_dir], image_path, disc_label, video=True)
            burn_iso(ser, image_path, speed, is_dual_layer)
        except (RuntimeError, FileNotFoundError) as e:
            if discstation_host.system_name() != "windows":
                raise
            # no xorriso -> burn the VIDEO_TS tree as a plain data disc (plays on
            # modern players; not guaranteed on old set-tops).
            print(f"xorriso unavailable ({e}); burning VIDEO_TS as a data disc")
            _run_windows_burn(ser, "burn-data.ps1", disc_device(), str(dvd_dir),
                              disc_label, re.sub(r"\D", "", speed or ""))
        finally:
            image_path.unlink(missing_ok=True)
        return
    send(ser, "STATUS:Burning disc...")
    send(ser, "PROGRESS:Starting burn")
    send(ser, f"INFO:Label {disc_label[:13]}")
    device = disc_device()
    growisofs_cmd = [tool('growisofs'), '-dvd-compat', '-Z', device]
    speed = speed or DISC_SPEED
    if speed and speed.lower() != "auto":
        if is_dual_layer:
            speed_num = int(re.sub(r'[^0-9]', '', speed) or '6')
            if speed_num > 4:
                speed = "4x"
                send(ser, "INFO:Capped DL speed to 4x")
        growisofs_cmd += ['-speed', speed.rstrip('x')]
    growisofs_cmd += ['-V', disc_label, '-dvd-video', str(dvd_dir)]
    _run_growisofs(ser, growisofs_cmd, dvd_dir.parent / "growisofs.log", device)

def burn_data(ser, source_paths, disc_label, speed=None, is_dual_layer=False):
    """Burn files as a data DVD — no conversion, no authoring, original quality."""
    if discstation_host.system_name() != "linux":
        WORK.mkdir(parents=True, exist_ok=True)
        image_path = WORK / f"data_{time.strftime('%Y%m%d_%H%M%S')}.iso"
        try:
            discstation_host.build_data_image(source_paths, image_path, disc_label)
            burn_iso(ser, image_path, speed, is_dual_layer)
        except (RuntimeError, FileNotFoundError) as e:
            if discstation_host.system_name() != "windows":
                raise
            print(f"xorriso unavailable ({e}); using IMAPI2 data burn")
            src = str(source_paths[0]) if len(source_paths) == 1 else _stage_dir(source_paths)
            _run_windows_burn(ser, "burn-data.ps1", disc_device(), src, disc_label,
                              re.sub(r"\D", "", speed or ""))
        finally:
            image_path.unlink(missing_ok=True)
        return
    send(ser, "STATUS:Burning data disc...")
    send(ser, "PROGRESS:Starting")
    send(ser, f"INFO:Label {disc_label[:13]}")
    device = disc_device()
    growisofs_cmd = [tool('growisofs'), '-dvd-compat', '-Z', device]
    speed = speed or DISC_SPEED
    if speed and speed.lower() != "auto":
        if is_dual_layer:
            speed_num = int(re.sub(r'[^0-9]', '', speed) or '6')
            if speed_num > 4:
                speed = "4x"
                send(ser, "INFO:Capped DL speed to 4x")
        growisofs_cmd += ['-speed', speed.rstrip('x')]
    growisofs_cmd += ['-R', '-J', '-joliet-long', '-allow-limited-size', '-V', disc_label]
    growisofs_cmd += [str(p) for p in source_paths]
    _run_growisofs(ser, growisofs_cmd, source_paths[0].parent / "growisofs.log", device)

def burn_audio_cd(ser, audio_files, disc_label, speed=None):
    """Convert audio files to CD-DA WAV and burn via cdrdao with CD-TEXT."""
    send(ser, "STATUS:Reading tags...")
    track_meta = []
    album_artist = ""
    album_title = ""
    for f in audio_files:
        artist, title = "", f.stem
        try:
            if f.suffix.lower() == ".flac":
                from mutagen.flac import FLAC
                a = FLAC(str(f))
                artist = a.get("albumartist", [a.get("artist", [""])[0]])[0]
                title = a.get("title", [f.stem])[0]
                if not album_title:
                    album_title = a.get("album", [""])[0]
                    album_artist = artist
            elif f.suffix.lower() == ".mp3":
                from mutagen.mp3 import MP3
                a = MP3(str(f))
                artist = str(a.get("TPE1", a.get("TPE2", "")))
                title = str(a.get("TIT2", f.stem))
                if not album_title:
                    album_title = str(a.get("TALB", ""))
                    album_artist = str(a.get("TPE2", artist))
            elif f.suffix.lower() == ".m4a":
                from mutagen.mp4 import MP4
                a = MP4(str(f))
                artist = a.get("\xa9ART", [""])[0]
                title = a.get("\xa9nam", [f.stem])[0]
                if not album_title:
                    album_title = a.get("\xa9alb", [""])[0]
                    album_artist = a.get("aART", [artist])[0]
        except Exception:
            pass
        track_meta.append((artist, title))
    album_artist = album_artist or "Unknown Artist"
    # Keep the album name read from the file tags; only fall back to the
    # folder/disc label when the tags had nothing.
    album_title = album_title or audio_disc_title(disc_label)

    send(ser, "STATUS:Converting audio...")
    send(ser, "PROGRESS:0%")
    tmp_dir = Path(audio_files[0]).parent / ".cd_tmp"
    shutil.rmtree(str(tmp_dir), ignore_errors=True)
    tmp_dir.mkdir(exist_ok=True)
    total = len(audio_files)
    for i, f in enumerate(audio_files):
        wav = tmp_dir / f"track_{i + 1:02d}.wav"
        subprocess.run(
            [tool('ffmpeg'), '-y', '-i', str(f), '-ar', '44100', '-ac', '2',
             '-sample_fmt', 's16', str(wav)],
            capture_output=True, check=True)
        pct = int((i + 1) / total * 30)
        send(ser, f"PROGRESS:{pct}%")

    send(ser, "STATUS:Writing TOC...")
    # CD-TEXT LANGUAGE 0 is EN (single-byte). Fold to ASCII and cap at the
    # 160-char CD-TEXT field limit so cdrdao/the drive don't reject the pack.
    def _cdt(value):
        text = cdrdao_text(value)
        text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
        return text.strip()[:160]

    album_t = _cdt(album_title) or "Audio CD"
    album_p = _cdt(album_artist) or "Unknown Artist"
    toc_lines = ["CD_DA"]
    toc_lines.append("CD_TEXT {")
    toc_lines.append("  LANGUAGE_MAP { 0: EN }")
    toc_lines.append("  LANGUAGE 0 {")
    toc_lines.append(f'    TITLE "{album_t}"')
    toc_lines.append(f'    PERFORMER "{album_p}"')
    toc_lines.append("  }")
    toc_lines.append("}")
    toc_lines.append("")
    for i, (artist, title) in enumerate(track_meta):
        wav = tmp_dir / f"track_{i + 1:02d}.wav"
        track_t = _cdt(title) or f"Track {i + 1:02d}"
        track_p = _cdt(artist) or album_p
        toc_lines.append("TRACK AUDIO")
        toc_lines.append("CD_TEXT {")
        toc_lines.append("  LANGUAGE 0 {")
        toc_lines.append(f'    TITLE "{track_t}"')
        toc_lines.append(f'    PERFORMER "{track_p}"')
        toc_lines.append("  }")
        toc_lines.append("}")
        toc_lines.append(f'FILE "{wav}" 0')
        toc_lines.append("")
    toc_path = tmp_dir / "disc.toc"
    toc_path.write_text("\n".join(toc_lines) + "\n")
    print(f"CD-TEXT: album={album_t!r} performer={album_p!r}, "
          f"{len(track_meta)} track titles")
    send(ser, "PROGRESS:35%")

    if discstation_host.system_name() == "windows":
        # No cdrdao on Windows — burn the prepared WAVs via IMAPI2 Track-At-Once.
        send(ser, "STATUS:Burning audio CD...")
        _run_windows_burn(ser, "burn-audio.ps1", disc_device(), str(tmp_dir),
                          re.sub(r"\D", "", (speed or DISC_SPEED) or ""))
        for w in tmp_dir.glob("*.wav"):
            w.unlink(missing_ok=True)
        toc_path.unlink(missing_ok=True)
        safe_send(ser, "DONE:Audio CD complete!")
        return

    send(ser, "STATUS:Burning audio CD...")
    # The cooked generic-mmc writer does NOT lay down the CD-TEXT lead-in on most
    # ATAPI drives; the raw writer does. Override with DISCSTATION_CDRDAO_DRIVER
    # (set it empty to let cdrdao auto-pick).
    try:
        cdrdao_write_dev = discstation_host.cdrdao_device(disc_device())
    except RuntimeError:
        if discstation_host.system_name() == "darwin":
            raise RuntimeError("Audio CD burning is not supported on this Mac "
                               "(cdrdao cannot access the optical drive)")
        raise
    cdrdao_cmd = [tool('cdrdao'), 'write', '--buffers', '64',
                  '--device', cdrdao_write_dev]
    driver = os.environ.get("DISCSTATION_CDRDAO_DRIVER", "generic-mmc-raw")
    if driver:
        cdrdao_cmd += ['--driver', driver]
    speed_ = speed or DISC_SPEED
    if speed_ and speed_.lower() != "auto":
        cdrdao_cmd += ['--speed', speed_.rstrip('x')]
    cdrdao_cmd.append(str(toc_path))  # toc-file must come after all options

    proc = subprocess.Popen(cdrdao_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out_lines = []
    log_path = WORK / "cdrdao.log"
    last_prog = 0
    try:
        for line in iter_proc_or_cancel(proc, ser):
            out_lines.append(line)
            m = re.search(r'(\d+)\s*%', line)
            if m:
                pct = 35 + int(int(m.group(1)) * 0.65)
                now = time.time()
                if now - last_prog >= 0.2:
                    send(ser, f"PROGRESS:{pct}%")
                    last_prog = now
    except (KeyboardInterrupt, SystemExit):
        stop_process(proc)
        raise
    finally:
        for w in tmp_dir.glob("*.wav"):
            w.unlink(missing_ok=True)
        toc_path.unlink(missing_ok=True)
        shutil.rmtree(str(tmp_dir), ignore_errors=True)
    rc = proc.wait()
    log_path.write_text("\n".join(out_lines) + "\n")
    if rc != 0:
        for line in out_lines[-10:]:
            print(f"cdrdao: {line}")
        if rc == -15:
            safe_send(ser, "CANCELLED:Burn cancelled")
            raise CancelError("Cancelled")
        detail = next((line.strip() for line in reversed(out_lines) if line.strip()), "cdrdao failed")
        safe_send(ser, f"INFO:CD burn failed; see {log_path.name}")
        raise RuntimeError(f"Disc burn failed: {detail[:80]}")
    try:
        discstation_host.eject_device(disc_device())
    except Exception as e:
        print(f"CD eject skipped: {e}")


def _stage_dir(paths):
    """Copy several loose paths into one temp folder (IMAPI2 burn-data takes one)."""
    staging = WORK / f"stage_{time.strftime('%Y%m%d_%H%M%S')}"
    staging.mkdir(parents=True, exist_ok=True)
    for p in paths:
        p = Path(p)
        dest = staging / p.name
        if p.is_dir():
            shutil.copytree(p, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(p, dest)
    return str(staging)


def _run_windows_burn(ser, script, *script_args):
    """Run a src/win/<script> IMAPI2 burn helper, streaming its PROGRESS:<pct>
    lines to the ESP32. Raises RuntimeError on a non-zero exit."""
    send(ser, "STATUS:Burning...")
    send(ser, "PROGRESS:0%")
    cmd, kwargs = discstation_host.ps_cmd(script, *script_args)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **kwargs)
    out_lines, last_pct = [], -1
    try:
        for line in iter_proc_or_cancel(proc, ser):
            out_lines.append(line)
            m = re.search(r"PROGRESS:(-?\d+)", line)
            if m:
                pct = int(m.group(1))
                if 0 <= pct <= 100 and pct != last_pct:
                    last_pct = pct
                    send(ser, f"PROGRESS:{min(pct, 99)}%")
    except (KeyboardInterrupt, SystemExit):
        stop_process(proc)
        raise
    if proc.wait() != 0:
        detail = next((l for l in reversed(out_lines) if l.strip()), "burn failed")
        raise RuntimeError(f"Disc burn failed: {detail[:150]}")
    safe_send(ser, "PROGRESS:100%")


def burn_iso(ser, iso_path, speed=None, is_dual_layer=False):
    """Burn a pre-built ISO directly to disc — no filesystem building."""
    if discstation_host.system_name() == "darwin":
        _run_hdiutil_burn(ser, iso_path)
        return
    if discstation_host.system_name() == "windows":
        drive = disc_device()
        spd = re.sub(r"\D", "", speed or "")
        try:
            _run_windows_burn(ser, "burn-image.ps1", drive, str(iso_path), spd)
        except RuntimeError:
            isoburn = shutil.which("isoburn") or os.path.join(
                os.environ.get("SystemRoot", r"C:\Windows"), "System32", "isoburn.exe")
            send(ser, "STATUS:Burning image (isoburn)...")
            if subprocess.run([isoburn, "/Q", drive, str(iso_path)]).returncode != 0:
                raise
            safe_send(ser, "PROGRESS:100%")
        return
    if discstation_host.system_name() != "linux":
        send(ser, "STATUS:Burning image...")
        _run_growisofs(ser, discstation_host.iso_burn_command(disc_device(), iso_path), iso_path.parent / "discstation-burn.log")
        return
    send(ser, "STATUS:Burning ISO...")
    send(ser, "PROGRESS:Starting")
    device = disc_device()
    growisofs_cmd = [tool('growisofs'), '-dvd-compat', '-Z', f"{device}={iso_path}"]
    speed = speed or DISC_SPEED
    if speed and speed.lower() != "auto":
        if is_dual_layer:
            speed_num = int(re.sub(r'[^0-9]', '', speed) or '6')
            if speed_num > 4:
                speed = "4x"
                send(ser, "INFO:Capped DL speed to 4x")
        growisofs_cmd += ['-speed', speed.rstrip('x')]
    _run_growisofs(ser, growisofs_cmd, iso_path.parent / "growisofs.log", device)

def remux_and_author(ser, mpg, disc_label, disc_capacity, dvd_aspect=None):
    """Remux an existing DVD-compliant .mpg to fix mux-rate/timestamp
    issues (falling back to the original file if the remux itself
    fails), then author and size-check it. Returns the authored dvd_dir.

    Uses _run_ffmpeg_pass for each remux step — same as convert()'s
    encode passes — so the OLED gets real PROGRESS updates and the
    built-in PING keepalive from iter_proc_or_cancel, instead of a
    silent blocking subprocess call that lets the ESP32's 30s watchdog
    flip the display to "disconnected" partway through.

    This is the single place the remux+author sequence lives — the
    full download/convert pipeline, the OLED burn_mpg_flow picker, and
    any CLI entry point all call this (via remux_and_burn below, or
    directly), so a fix here only has to happen once.
    """
    check_encoded_size(ser, mpg, disc_capacity)
    send(ser, f"TITLE:{disc_label}")
    send(ser, f"INFO:Burning {mpg.parent.name}")

    send(ser, "STATUS:Remuxing to fix timestamps...")
    WORK.mkdir(parents=True, exist_ok=True)
    v_es = WORK / f"video_{os.getpid()}.m2v"
    a_es = WORK / f"audio_{os.getpid()}.ac3"
    fixed = mpg.parent / "movie_fixed.mpg"
    if fixed.exists():
        fixed.unlink()

    total = probe_duration(mpg)

    try:
        _run_ffmpeg_pass(ser,
            [tool("ffmpeg"), "-y", "-i", str(mpg), "-map", "0:v", "-c:v", "copy", str(v_es)],
            total, "Remux 1/3 (video)")
        _run_ffmpeg_pass(ser,
            [tool("ffmpeg"), "-y", "-i", str(mpg), "-map", "0:a", "-c:a", "copy", str(a_es)],
            total, "Remux 2/3 (audio)")
        _run_ffmpeg_pass(ser,
            [tool("ffmpeg"), "-y", "-i", str(v_es), "-i", str(a_es),
             "-c", "copy", "-muxrate", "10080k", "-f", "dvd", str(fixed)],
            total, "Remux 3/3 (mux)")
    except RuntimeError as e:
        if str(e) == "Cancelled":
            raise
        safe_send(ser, "ERROR:Remux failed, using original")
        time.sleep(2)
        fixed = mpg
    finally:
        for f in (v_es, a_es):
            if f.exists():
                f.unlink()

    check_encoded_size(ser, fixed, disc_capacity)
    dvd_out = fixed.parent / "dvd_out"
    if dvd_out.exists():
        shutil.rmtree(dvd_out)

    if dvd_aspect is None:
        aspect = probe_aspect(fixed)
        dvd_aspect = "16:9" if (aspect is not None and aspect > 1.4) else "4:3"

    dvd_dir = author(ser, fixed, fixed.parent, dvd_aspect)
    check_dvd_size(ser, dvd_dir, disc_capacity)
    return dvd_dir

def remux_and_burn(ser, mpg, disc_label, disc_capacity, dl_info, burn_speed=None, dvd_aspect=None):
    dvd_dir = remux_and_author(ser, mpg, disc_label, disc_capacity, dvd_aspect)
    wait_for_burn_confirm(ser, dvd_dir, disc_capacity)
    burn(ser, dvd_dir, disc_label, burn_speed, dl_info["is_dual_layer"])
    safe_send(ser, "DONE:Burn complete!")
    print(f"Burned {mpg} as {disc_label}")


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 discstation_burn.py 'video URL or file path'")
        sys.exit(1)
    url = sys.argv[1]
    WORK.mkdir(parents=True, exist_ok=True)
    job_dir = WORK / time.strftime("job_%Y%m%d_%H%M%S")
    job_dir.mkdir()
    ser = None
    try:
        ser = serial.Serial(PORT, BAUD, timeout=1)
        reset_serial_state()
        time.sleep(2)
        print("Connected to DiscStation")
        print("Running preflight...")
        send(ser, "STATUS:Preflight...")
        info = get_video_info(url)
        title = info["title"]
        duration = info["duration"]
        duration_line, fit_line, can_fit = preflight_lines(duration)
        print(f"Title: {title}")
        print(f"Duration: {format_duration(duration)}")
        print(f"Preflight: {fit_line}")
        print(f"DVD drive: {disc_device()}")
        disc_label = sanitize_disc_label(title)
        print(f"Disc label: {disc_label}")
        send(ser, f"TITLE:{title}")
        send(ser, f"META:{duration_line}")
        send(ser, f"FIT:{fit_line}")
        if not can_fit:
            raise RuntimeError("Video too long for DVD5")

        device = disc_device()
        dl_info = detect_disc_type(device)
        if dl_info["is_dual_layer"]:
            sl_target = int(os.environ.get("DISC_TARGET_BYTES", "4300000000"))
            try:
                sl_plan = bitrate_plan(duration, "AUTO", sl_target)
                if sl_plan:
                    warn = f"DL disc for SL content"
                    print(f"WARNING: {warn}")
                    safe_send(ser, f"WARNING:{warn}")
                    time.sleep(3)
            except RuntimeError:
                pass

        print("Waiting for button press...")
        selected_mode = "AUTO"
        burn_speed = None
        while True:
            if ser.in_waiting:
                resp = ser.readline().decode(errors='ignore').strip()
                note_serial_activity()
                if resp == "CANCEL":
                    print("Cancelled by user")
                    safe_send(ser, "CANCELLED:Cancelled")
                    sys.exit(130)
                elif resp.startswith("MODE:"):
                    selected_mode = normalize_mode(resp.split(":", 1)[1])
                    print(f"Mode: {selected_mode}")
                elif resp.startswith("SPEED:"):
                    burn_speed = resp.split(":", 1)[1].strip()
                    print(f"Burn speed: {burn_speed}")
                elif resp == "START" or resp.startswith("START:"):
                    if ":" in resp:
                        selected_mode = normalize_mode(resp.split(":", 1)[1])
                    print(f"Button pressed - starting in {selected_mode} mode!")
                    send(ser, f"STATUS:Starting {selected_mode}...")
                    break
            time.sleep(0.1)

        start_time = time.time()
        disc_type_label = "DL" if dl_info["is_dual_layer"] else "SL"
        try:
            disc_bytes = dl_info["capacity"]
            plan = bitrate_plan(duration, selected_mode, disc_bytes)
            video  = download(ser, url, job_dir)
            mpg, dvd_aspect = convert(ser, video, job_dir, selected_mode, disc_bytes)
            dvd    = remux_and_author(ser, mpg, disc_label, disc_bytes, dvd_aspect)
            if plan["burn"]:
                burn(ser, dvd, disc_label, burn_speed, dl_info["is_dual_layer"])
                send(ser, "DONE:Disc complete!")
                print("Done!")
            else:
                send(ser, "DONE:Test complete!")
                print("Test complete. DVD folder was built but not burned.")
            append_history({
                "timestamp": datetime.datetime.now().isoformat(),
                "title": title,
                "disc_type": disc_type_label,
                "mode": selected_mode,
                "speed": burn_speed or "Auto",
                "success": True,
                "duration_s": round(time.time() - start_time),
            })
        except (KeyboardInterrupt, SystemExit):
            append_history({
                "timestamp": datetime.datetime.now().isoformat(),
                "title": title,
                "disc_type": disc_type_label,
                "mode": selected_mode,
                "speed": burn_speed or "Auto",
                "success": False,
                "error": "Cancelled",
                "duration_s": round(time.time() - start_time),
            })
            raise
        except Exception as e:
            append_history({
                "timestamp": datetime.datetime.now().isoformat(),
                "title": title,
                "disc_type": disc_type_label,
                "mode": selected_mode,
                "speed": burn_speed or "Auto",
                "success": False,
                "error": str(e)[:100],
                "duration_s": round(time.time() - start_time),
            })
            raise
    except (KeyboardInterrupt, SystemExit):
        safe_send(ser, "CANCELLED:Stopped")
        print("Cancelled")
        sys.exit(130)
    except RuntimeError as e:
        msg = str(e)
        if msg == "Cancelled":
            safe_send(ser, "CANCELLED:Stopped")
        else:
            safe_send(ser, f"ERROR:{msg[:20]}")
        print(f"Error: {e}")
        sys.exit(1)
    except Exception as e:
        safe_send(ser, f"ERROR:{str(e)[:20]}")
        print(f"Error: {e}")
        sys.exit(1)
    finally:
        if ser:
            ser.close()


BURN_HISTORY = WORK / "burn_history.jsonl"


def append_history(entry):
    BURN_HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with open(BURN_HISTORY, "a") as f:
        f.write(json.dumps(entry) + "\n")


if __name__ == "__main__":
    main()
