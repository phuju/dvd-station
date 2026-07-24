#!/usr/bin/env python3
import datetime
import json
import os
import pwd
import re
import select
import serial
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path

CLEANUP_DAYS = int(os.environ.get("DVD_CLEANUP_DAYS", "2"))


def reset_drive(device="/dev/sr0"):
    for path in Path("/sys/bus/usb/devices").glob("*/authorized"):
        try:
            target = path.parent / "product"
            if target.is_file():
                prod = target.read_text().strip()
                if "Slim" in prod or "DVD" in prod or "MediaTek" in prod or "ASUS" in prod or "SDRW" in prod or "YzWy" in prod or "Disk" in prod:
                    with open(path, "w") as f:
                        f.write("0\n")
                    time.sleep(2)
                    with open(path, "w") as f:
                        f.write("1\n")
                    time.sleep(6)
                    return True
        except (OSError, PermissionError):
            pass
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
    override = os.environ.get("DVD_PORT")
    if override:
        return override

    port = _esp32_port_from_sysfs()
    if port:
        return port

    for pattern in ("/dev/ttyACM*", "/dev/ttyUSB*"):
        ports = sorted(Path("/dev").glob(pattern.strip("/")))
        if ports:
            return str(ports[0])

    return "/dev/ttyACM0"

PORT = detect_esp32_port()
BAUD   = 115200


def check_cancel(ser):
    try:
        if ser and ser.in_waiting:
            line = ser.readline().decode(errors="ignore").strip()
            return line in ("CANCEL", "PLAY_STOP")
    except OSError:
        pass
    return False


def iter_proc_or_cancel(proc, ser):
    proc_fd = proc.stdout.fileno()
    ser_fd = ser.fileno()
    buffer = ""
    last_ping = time.time()

    while proc.poll() is None:
        if time.time() - last_ping >= 5:
            last_ping = time.time()
            send(ser, "PING")

        ready, _, _ = select.select([proc_fd, ser_fd], [], [], 0.5)

        if ser_fd in ready and check_cancel(ser):
            stop_process(proc)
            return

        if proc_fd in ready:
            try:
                chunk = os.read(proc_fd, 4096).decode(errors="ignore")
            except OSError:
                break
            if not chunk:
                break
            for char in chunk:
                if char in "\r\n":
                    if buffer:
                        yield buffer
                        buffer = ""
                else:
                    buffer += char

    if buffer:
        yield buffer

    for line in proc.stdout:
        yield line.rstrip("\r\n")


DVD_DEVICE = os.environ.get("DVD_DEVICE")
DVD_SPEED = os.environ.get("DVD_SPEED")
DVD_DISC_BYTES = 4_700_000_000
DVD_DL_BYTES = 8_500_000_000
DVD_TARGET_BYTES = int(os.environ.get("DVD_TARGET_BYTES", "4300000000"))
DVD_MUX_SAFETY = float(os.environ.get("DVD_MUX_SAFETY", "0.92"))
AUDIO_BITRATE_K = int(os.environ.get("DVD_AUDIO_KBPS", "192"))
MIN_VIDEO_BITRATE_K = 500
MAX_VIDEO_BITRATE_K = 7150
MAX_VIDEO_PEAK_K = 9000
YTDLP_FORMAT = "bv*+ba/b"
MODE_SETTINGS = {
    "AUTO": {
        "target_bytes": DVD_TARGET_BYTES,
        "safety": DVD_MUX_SAFETY,
        "audio_k": AUDIO_BITRATE_K,
        "min_video_k": MIN_VIDEO_BITRATE_K,
        "max_video_k": MAX_VIDEO_BITRATE_K,
        "peak_video_k": MAX_VIDEO_PEAK_K,
        "burn": True,
    },
    "BEST": {
        "target_bytes": int(os.environ.get("DVD_BEST_TARGET_BYTES", "4450000000")),
        "safety": float(os.environ.get("DVD_BEST_SAFETY", "0.95")),
        "audio_k": int(os.environ.get("DVD_BEST_AUDIO_KBPS", "224")),
        "min_video_k": 700,
        "max_video_k": 8000,
        "peak_video_k": 9000,
        "burn": True,
    },
    "LONG": {
        "target_bytes": int(os.environ.get("DVD_LONG_TARGET_BYTES", "4300000000")),
        "safety": float(os.environ.get("DVD_LONG_SAFETY", "0.90")),
        "audio_k": int(os.environ.get("DVD_LONG_AUDIO_KBPS", "128")),
        "min_video_k": 350,
        "max_video_k": 3500,
        "peak_video_k": 6000,
        "burn": True,
    },
    "TEST": {
        "target_bytes": DVD_TARGET_BYTES,
        "safety": DVD_MUX_SAFETY,
        "audio_k": AUDIO_BITRATE_K,
        "min_video_k": MIN_VIDEO_BITRATE_K,
        "max_video_k": MAX_VIDEO_BITRATE_K,
        "peak_video_k": MAX_VIDEO_PEAK_K,
        "burn": False,
    },
}

def user_home():
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and sudo_user != "root":
        return Path(pwd.getpwnam(sudo_user).pw_dir)
    return Path.home()

USER_HOME = user_home()
WORK = USER_HOME / ".cache" / "dvd-station"

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
    local = USER_HOME / ".local" / "bin" / name
    if local.exists():
        return str(local)
    found = shutil.which(name)
    if found:
        return found
    raise FileNotFoundError(f"{name} not found")

def js_runtime_arg():
    override = os.environ.get("YTDLP_JS_RUNTIME")
    if override:
        return ["--js-runtimes", override]
    for name in ("node", "/usr/bin/node", "/bin/node"):
        path = shutil.which(name) if not name.startswith("/") else name
        if path and Path(path).exists():
            return ["--js-runtimes", f"node:{path}"]
    return []

def remote_components_arg():
    value = os.environ.get("YTDLP_REMOTE_COMPONENTS", "ejs:github")
    if value.lower() in ("", "0", "false", "none", "off"):
        return []
    return ["--remote-components", value]

def ytdlp_base_args():
    return [tool('yt-dlp'), "--no-playlist", *js_runtime_arg(), *remote_components_arg()]

def dvd_device():
    if DVD_DEVICE:
        return DVD_DEVICE
    for name in ("/dev/dvd", "/dev/cdrom"):
        path = Path(name)
        if path.exists():
            return str(path.resolve())
    drives = sorted(Path("/dev").glob("sr*"))
    if drives:
        return str(drives[0])
    raise FileNotFoundError("No DVD drive found")

def _udevadm_props(device):
    r = subprocess.run(
        ["udevadm", "info", "--query=property", "--name", device],
        capture_output=True, text=True, timeout=2,
    )
    props = {}
    for line in r.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            props[k] = v
    return props


def disc_capacity_bytes(device):
    override = os.environ.get("DVD_DISC_BYTES")
    if override:
        try:
            return int(override)
        except ValueError:
            pass

    props = _udevadm_props(device)
    is_dl = (
        props.get("ID_CDROM_MEDIA_DVD_PLUS_R_DL") == "1" or
        props.get("ID_CDROM_MEDIA_DVD_R_DL") == "1" or
        props.get("ID_CDROM_MEDIA_DVD_R_DL_SEQ") == "1"
    )
    expected_min = 1_000_000_000
    expected_max = DVD_DL_BYTES if is_dl else DVD_DISC_BYTES

    best = None
    for attempt in range(3):
        r = subprocess.run(
            ["dvd+rw-mediainfo", device],
            capture_output=True, text=True, timeout=5,
        )
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
        elif is_dl and best < DVD_DISC_BYTES:
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
        return DVD_DISC_BYTES
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
        capacity = DVD_DL_BYTES if is_dl else (DVD_DISC_BYTES if is_sl else None)

    return {
        "is_dual_layer": is_dl,
        "is_single_layer": is_sl,
        "is_blank": is_blank,
        "status": status,
        "media_type": media_type,
        "capacity": capacity,
    }

def is_dual_layer(device):
    return detect_disc_type(device)["is_dual_layer"]

def send(ser, msg):
    try:
        ser.write((msg + '\n').encode())
    except Exception as e:
        print(f"serial send error ('{msg[:30]}'): {e}")

def safe_send(ser, msg):
    if not ser:
        return
    try:
        send(ser, msg)
    except Exception:
        pass

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



def bitrate_plan(duration, mode="AUTO", disc_bytes=None):
    mode = normalize_mode(mode)
    settings = MODE_SETTINGS[mode]

    # Use the actual detected disc capacity when we have it (DL vs SL),
    # falling back to the mode's static default only when disc_bytes
    # wasn't passed in. The existing settings["safety"] factor below
    # still applies on top of whichever value we use, so DL discs get
    # the same safety margin instead of being silently capped at the
    # SL-sized default.
    target_bytes = disc_bytes if disc_bytes else settings["target_bytes"]

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

def dvd_video_bitrate(duration, mode="AUTO"):
    return bitrate_plan(duration, mode)["video_k"]

def tree_size(path):
    return sum(p.stat().st_size for p in Path(path).rglob("*") if p.is_file())

def sanitize_disc_label(title):
    label = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    label = re.sub(r"[^A-Za-z0-9]+", "_", label.upper()).strip("_")
    return (label or "DVD_VIDEO")[:32]

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
    r = subprocess.run([*ytdlp_base_args(), '--dump-single-json', '--skip-download', source],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "Could not get video info")
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Could not parse video info: {e}") from e
    return {
        "title": data.get("title") or "Untitled video",
        "duration": float(data.get("duration") or 0),
    }

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
                    raise RuntimeError("Cancelled")
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
    proc = subprocess.Popen(
        [*ytdlp_base_args(), '-f', YTDLP_FORMAT, '-o', out, source],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    last_prog = 0
    try:
        for line in iter_proc_or_cancel(proc, ser):
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
    if proc.wait() != 0:
        if proc.returncode == -15:
            safe_send(ser, "CANCELLED:Download cancelled")
            raise RuntimeError("Cancelled")
        raise RuntimeError("Download failed")
    files = [p for p in download_dir.iterdir()
             if p.is_file() and not p.name.endswith(('.part', '.ytdl'))]
    if not files:
        raise RuntimeError("No downloaded file")
    return max(files, key=lambda p: p.stat().st_mtime)


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
            raise RuntimeError("Cancelled")
        raise RuntimeError("FFmpeg encode failed")


def convert(ser, infile, job_dir, mode, disc_bytes=None):
    send(ser, "STATUS:Converting...")
    out = job_dir / "movie.mpg"
    total = probe_duration(infile)
    plan = bitrate_plan(total, mode, disc_bytes)
    print(f"Convert: mode={mode} dur={total:.0f}s video_k={plan['video_k']}k "
          f"peak_k={plan['peak_video_k']}k audio_k={plan['audio_k']}k "
          f"target_bytes={MODE_SETTINGS[mode]['target_bytes']}", flush=True)
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
                    '-an', '-f', 'null', '/dev/null']
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
            raise RuntimeError("Cancelled")
        raise RuntimeError("DVD authoring failed")
    return dvd_dir

def check_dvd_size(ser, dvd_dir, disc_bytes=None):
    send(ser, "STATUS:Checking size...")
    size = tree_size(dvd_dir)
    limit = disc_bytes if disc_bytes and disc_bytes > 0 else DVD_DISC_BYTES
    send(ser, f"PROGRESS:{size / 1_000_000_000:.2f}GB / {limit / 1_000_000_000:.1f}GB")
    if size > limit:
        raise RuntimeError("Output too large for disc")
    return size

def wait_for_burn_confirm(ser, dvd_dir, disc_capacity):
    data_size = tree_size(dvd_dir)
    actual_cap = disc_capacity_bytes(dvd_device()) or disc_capacity
    cap_gb = actual_cap / 1_000_000_000
    data_gb = data_size / 1_000_000_000
    line = f"WAITING:{data_gb:.2f}GB / {cap_gb:.1f}GB"
    send(ser, line)
    last_ping = time.time()
    while True:
        if time.time() - last_ping >= 5:
            last_ping = time.time()
            send(ser, "PING")
        try:
            if ser and ser.in_waiting:
                resp = ser.readline().decode(errors="ignore").strip()
                if resp == "CONFIRM" or resp == "START":
                    return True
                if resp == "CANCEL":
                    raise RuntimeError("Burn cancelled by user")
        except OSError:
            raise RuntimeError("Serial error during burn confirm")
        time.sleep(0.05)

def burn(ser, dvd_dir, disc_label, speed=None, is_dual_layer=False):
    send(ser, "STATUS:Burning disc...")
    send(ser, "PROGRESS:Starting burn")
    send(ser, f"INFO:Label {disc_label[:13]}")
    growisofs_cmd = [tool('growisofs'), '-dvd-compat', '-Z', dvd_device()]
    speed = speed or DVD_SPEED
    if speed and speed.lower() != "auto":
        if is_dual_layer:
            speed_num = int(re.sub(r'[^0-9]', '', speed) or '6')
            if speed_num > 4:
                speed = "4x"
                send(ser, "INFO:Capped DL speed to 4x")
        growisofs_cmd += ['-speed', speed.rstrip('x')]
    growisofs_cmd += ['-V', disc_label, '-dvd-video', str(dvd_dir)]
    log_path = dvd_dir.parent / "growisofs.log"
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
    if rc != 0:
        with open(log_path, 'w') as f:
            f.write('\n'.join(out_lines))
        for line in out_lines[-10:]:
            print(f"growisofs: {line}")
        if rc == -15:
            safe_send(ser, "CANCELLED:Burn cancelled")
            raise RuntimeError("Cancelled")
        # "unable to reload tray" is a false positive — the data was already
        # written and the disc finalized before growisofs attempted the
        # reload-verify.  The disc is good; the user just has to reinsert it
        # manually if the tray didn't catch it on the way back in.
        if "unable to reload tray" in "\n".join(out_lines):
            safe_send(ser, "INFO:Disc written OK (tray reload skipped)")
            print("growisofs: tray reload failed after successful write — disc is fine")
        else:
            safe_send(ser, "INFO:Burn failed, check log")
            raise RuntimeError("Disc burn failed")

def burn_data(ser, source_paths, disc_label, speed=None, is_dual_layer=False):
    """Burn files as a data DVD — no conversion, no authoring, original quality."""
    send(ser, "STATUS:Burning data disc...")
    send(ser, "PROGRESS:Starting")
    send(ser, f"INFO:Label {disc_label[:13]}")
    growisofs_cmd = [tool('growisofs'), '-dvd-compat', '-Z', dvd_device()]
    speed = speed or DVD_SPEED
    if speed and speed.lower() != "auto":
        if is_dual_layer:
            speed_num = int(re.sub(r'[^0-9]', '', speed) or '6')
            if speed_num > 4:
                speed = "4x"
                send(ser, "INFO:Capped DL speed to 4x")
        growisofs_cmd += ['-speed', speed.rstrip('x')]
    growisofs_cmd += ['-R', '-J', '-joliet-long', '-V', disc_label]
    growisofs_cmd += [str(p) for p in source_paths]
    log_path = source_paths[0].parent / "growisofs.log"
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
    if rc != 0:
        with open(log_path, 'w') as f:
            f.write('\n'.join(out_lines))
        for line in out_lines[-10:]:
            print(f"growisofs: {line}")
        if rc == -15:
            safe_send(ser, "CANCELLED:Burn cancelled")
            raise RuntimeError("Cancelled")
        if "unable to reload tray" in "\n".join(out_lines):
            safe_send(ser, "INFO:Disc written OK (tray reload skipped)")
            print("growisofs: tray reload failed after successful write — disc is fine")
        else:
            safe_send(ser, "INFO:Burn failed, check log")
            raise RuntimeError("Disc burn failed")

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
    send(ser, f"TITLE:{disc_label}")
    send(ser, f"INFO:Burning {mpg.parent.name}")

    send(ser, "STATUS:Remuxing to fix timestamps...")
    v_es = Path(f"/tmp/video_{os.getpid()}.m2v")
    a_es = Path(f"/tmp/audio_{os.getpid()}.ac3")
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
        print("Usage: python3 dvd_burn.py 'video URL or file path'")
        sys.exit(1)
    url = sys.argv[1]
    WORK.mkdir(parents=True, exist_ok=True)
    job_dir = WORK / time.strftime("job_%Y%m%d_%H%M%S")
    job_dir.mkdir()
    ser = None
    try:
        ser = serial.Serial(PORT, BAUD, timeout=1)
        time.sleep(2)
        print("Connected to DVD Station")
        print("Running preflight...")
        send(ser, "STATUS:Preflight...")
        info = get_video_info(url)
        title = info["title"]
        duration = info["duration"]
        duration_line, fit_line, can_fit = preflight_lines(duration)
        print(f"Title: {title}")
        print(f"Duration: {format_duration(duration)}")
        print(f"Preflight: {fit_line}")
        print(f"DVD drive: {dvd_device()}")
        disc_label = sanitize_disc_label(title)
        print(f"Disc label: {disc_label}")
        send(ser, f"TITLE:{title}")
        send(ser, f"META:{duration_line}")
        send(ser, f"FIT:{fit_line}")
        if not can_fit:
            raise RuntimeError("Video too long for DVD5")

        device = dvd_device()
        dl_info = detect_disc_type(device)
        if dl_info["is_dual_layer"]:
            sl_target = int(os.environ.get("DVD_TARGET_BYTES", "4300000000"))
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
            dvd    = author(ser, mpg, job_dir, dvd_aspect)
            check_dvd_size(ser, dvd, disc_bytes)
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