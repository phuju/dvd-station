#!/usr/bin/env python3
import argparse
import atexit
import collections
import concurrent.futures
import errno
try:
    import fcntl  # POSIX-only; used only in drive_status()'s Linux branch
except ImportError:
    fcntl = None
import json
import mimetypes
import os
import signal
try:
    import pwd
except ImportError:
    pwd = None
import re
import shutil
import socket
try:
    import ssl  # optional: HTTPS on :8080. The plain-HTTP :8081 listener works without it.
except ImportError:
    ssl = None
import subprocess
import sys
import tempfile
import time
import datetime
from pathlib import Path
from queue import Queue, Empty, Full
import http.server
import socketserver
try:
    import termios
except ImportError:
    class _TermiosCompat:
        error = OSError
    termios = _TermiosCompat()
import threading
import urllib.parse

import requests
import serial
from mutagen.flac import FLAC, Picture

import discstation_burn
import discstation_host

# Web interface for URL input and file upload
_burn_url_queue = Queue()
_web_port = 8080
_web_server = None
_last_burn_result = None
_last_burn_result_time = 0
_last_upload_dir = None
_last_upload_label = None
_web_status = "READY"
_web_progress = -1
_web_progress_active = False
_operation_active = False  # a burn/rip/play flow is holding the drive
_last_disc_info = {"disc_present": False, "capacity_bytes": 0, "capacity_gb": 0, "type": "none"}
_active_ser = None
STATIC_DIR = Path(__file__).resolve().parent / "static"


class _WebHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == '/':
            self._serve_page()
        elif path == '/status':
            result = _last_burn_result
            self._respond(200, _web_status or result or 'Idle')
        elif path == '/progress':
            self._respond(200, json.dumps({
                "status": _web_status or "READY",
                "progress": _web_progress,
                "active": _web_progress_active,
            }), "application/json")
        elif path == '/disc-info':
            self._serve_disc_info()
        elif path == '/events':
            self._serve_sse()
        elif path == '/sw.js':
            self._serve_sw()
        elif path == '/manifest.json':
            self._serve_manifest()
        elif path.startswith('/static/'):
            self._serve_static(path[8:])
        else:
            self.send_error(404)

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == '/':
            content_type = self.headers.get('Content-Type', '')
            if 'multipart/form-data' in content_type:
                self._handle_upload()
            else:
                self._handle_url()
        elif path == '/set-label':
            self._handle_set_label()
        else:
            self.send_error(404)

    def _handle_url(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length).decode()
        params = urllib.parse.parse_qs(body)
        url = params.get('url', [''])[0].strip()
        if url:
            _burn_url_queue.put(url)
            self._respond(200, 'URL received. Starting burn...')
        else:
            self._respond(400, 'Missing URL')

    def _handle_upload(self):
        global _last_upload_dir
        _set_web_progress("UPLOADING", 0)
        files = self._parse_multipart(lambda percent: _set_web_progress("UPLOADING", percent))
        if not files:
            self._respond(400, 'No files uploaded')
            return
        paths_list = []
        for filename, data in list(files):
            if filename == '_paths':
                try:
                    parsed = json.loads(data.decode())
                    paths_list = parsed if isinstance(parsed, list) else []
                except Exception as e:
                    print(f"Upload path metadata error: {e}")
                files.remove((filename, data))
        upload_dir = Path(discstation_burn.WORK) / f"upload_{time.strftime('%Y%m%d_%H%M%S')}"
        upload_dir.mkdir(parents=True, exist_ok=True)
        total = 0
        for i, (filename, data) in enumerate(files):
            rel = filename
            if i < len(paths_list) and paths_list[i].get('p'):
                p = paths_list[i]['p']
                if p != paths_list[i].get('n', ''):
                    rel = p
            dest = (upload_dir / rel.lstrip('/')).resolve()
            if upload_dir.resolve() not in dest.parents:
                print(f"Skipping unsafe upload path: {rel}")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            total += len(data)
        _last_upload_dir = str(upload_dir)
        size_str = f"{total / 1e6:.1f}MB" if total > 1e6 else f"{total / 1e3:.0f}KB"
        _set_web_progress("UPLOAD READY", 100)
        self._respond(200, f'{len(files)} file(s) uploaded ({size_str}). Select BURN DATA on remote.')

    def _serve_disc_info(self):
        if _operation_active:
            # a burn/rip/play holds the drive — don't probe it, serve last-known.
            self._respond(200, json.dumps({**_last_disc_info, "busy": True}), "application/json")
            return
        info = {"disc_present": False, "capacity_bytes": 0, "capacity_gb": 0, "type": "none"}
        try:
            device = discstation_burn.disc_device()
            di = detect_disc(device, settle=False, budget=15)
            info["disc_present"] = di.present
            info["capacity_bytes"] = di.capacity_bytes
            info["capacity_gb"] = round(di.capacity_bytes / 1e9, 2)
            if not di.present:
                info["type"] = "none"
            elif di.transient:
                info["type"] = "reading"
            else:
                info["type"] = di.web_type
            info["kind"] = di.kind
            info["label"] = di.label
        except Exception as e:
            print(f"Disc info error: {e}")
        _last_disc_info.update(info)
        self._respond(200, json.dumps(info), "application/json")

    def _serve_sse(self):
        """Server-Sent Events stream: pushes status/progress snapshots and a
        'disc-changed' nudge the moment anything changes. Keepalive comment every
        15s so proxies don't drop the idle connection."""
        q = Queue(maxsize=64)
        with _sse_lock:
            if len(_sse_subs) >= 32:
                self.send_error(503)
                return
            _sse_subs.add(q)
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Accel-Buffering', 'no')
            self.end_headers()
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.write(("data: " + json.dumps(_status_snapshot()) + "\n\n").encode())
            self.wfile.flush()
            while True:
                try:
                    payload = q.get(timeout=15)
                    self.wfile.write(("data: " + payload + "\n\n").encode())
                except Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
            pass
        finally:
            with _sse_lock:
                _sse_subs.discard(q)

    def _handle_set_label(self):
        global _last_upload_label
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length).decode()
        params = urllib.parse.parse_qs(body)
        label = params.get('label', [''])[0].strip()
        if label:
            _last_upload_label = label
            self._respond(200, f'Label set: {label}')
        else:
            self._respond(400, 'Missing label')

    def _serve_sw(self):
        sw = '''self.addEventListener('install', e => {
  self.skipWaiting();
  caches.open('discstation-v8').then(c => c.addAll(['/','/static/style.css?v=8','/static/app.js?v=8']));
});
self.addEventListener('activate', e => e.waitUntil(clients.claim()));
self.addEventListener('fetch', e => {
  const path = new URL(e.request.url).pathname;
  if (path === '/events') return;            // never intercept the SSE stream
  if (path === '/' || path.startsWith('/static/')) {
    e.respondWith(fetch(e.request).then(r => {
      const copy = r.clone();
      caches.open('discstation-v8').then(c => c.put(e.request, copy));
      return r;
    }).catch(() => caches.match(e.request)));
  } else {
    e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
  }
});'''
        self._respond(200, sw, 'application/javascript')

    def _serve_manifest(self):
        icon_svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" fill="#f0ede4"/><circle cx="256" cy="256" r="210" fill="none" stroke="#1a1a1a" stroke-width="20"/><circle cx="256" cy="256" r="68" fill="none" stroke="#1a1a1a" stroke-width="16"/><path d="M256 188v68h68" fill="none" stroke="#1a1a1a" stroke-width="16"/></svg>'
        manifest = {
            "name": "DiscStation",
            "short_name": "DiscStation",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#f0ede4",
            "theme_color": "#f0ede4",
            "description": "DiscStation physical media instrument",
            "icons": [{
                "src": "data:image/svg+xml," + urllib.parse.quote(icon_svg),
                "sizes": "512x512",
                "type": "image/svg+xml",
                "purpose": "any maskable"
            }]
        }
        self._respond(200, json.dumps(manifest), 'application/json')

    def _parse_multipart(self, progress_callback=None):
        content_type = self.headers.get('Content-Type', '')
        boundary = None
        for part in content_type.split(';'):
            part = part.strip()
            if part.lower().startswith('boundary='):
                boundary = part[9:].strip('"')
        if not boundary:
            return []
        content_length = int(self.headers.get('Content-Length', 0))
        chunks = []
        received = 0
        while received < content_length:
            chunk = self.rfile.read(min(1024 * 1024, content_length - received))
            if not chunk:
                break
            chunks.append(chunk)
            received += len(chunk)
            if progress_callback and content_length:
                progress_callback(min(99, int(received * 100 / content_length)))
        raw = b"".join(chunks)
        boundary_b = ('--' + boundary).encode()
        parts = raw.split(boundary_b)[1:-1]
        files = []
        for part in parts:
            if part.startswith(b'--'):
                break
            header_end = part.find(b'\r\n\r\n')
            if header_end < 0:
                continue
            headers_raw = part[:header_end].decode(errors='ignore')
            body = part[header_end + 4:]
            if body.endswith(b'\r\n'):
                body = body[:-2]
            filename = None
            for line in headers_raw.split('\r\n'):
                if line.lower().startswith('content-disposition:'):
                    for attr in line.split(';'):
                        attr = attr.strip()
                        if attr.startswith('filename='):
                            filename = attr[10:].strip('"')
            field_name = None
            for line in headers_raw.split('\r\n'):
                if line.lower().startswith('content-disposition:'):
                    for attr in line.split(';'):
                        attr = attr.strip()
                        if attr.startswith('name='):
                            field_name = attr[5:].strip('"')
            if body and (filename or field_name == '_paths'):
                files.append((filename or field_name, body))
        return files

    def _serve_page(self):
        self._serve_static("index.html", "text/html; charset=utf-8")

    def _serve_static(self, relative_path, content_type=None):
        root = STATIC_DIR.resolve()
        requested = (root / relative_path).resolve()
        if root not in requested.parents or not requested.is_file():
            self.send_error(404)
            return
        content_type = content_type or mimetypes.guess_type(str(requested))[0] or "application/octet-stream"
        self._respond(200, requested.read_bytes(), content_type)

    def _respond(self, code, body, ctype='text/plain'):
        try:
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Connection', 'close')
            self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
            self.end_headers()
            self.wfile.write(body.encode() if isinstance(body, str) else body)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLEOFError):
            pass

    def log_message(self, fmt, *args):
        pass


def start_web_server(port=8080):
    global _web_server, _web_port
    if _web_server:
        return _web_server
    _web_port = port
    server = socketserver.ThreadingTCPServer(('', port), _WebHandler, bind_and_activate=False)
    server.allow_reuse_address = True
    server.daemon_threads = True  # don't let an open SSE connection wedge shutdown
    server.server_bind()
    server.server_activate()

    cert_dir = discstation_host.config_dir()
    cert = cert_dir / 'server.crt'
    key = cert_dir / 'server.key'
    if ssl is not None and cert.exists() and key.exists():
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(str(cert), str(key))
            server.socket = ctx.wrap_socket(server.socket, server_side=True)
            print(f"Web interface on https://0.0.0.0:{port}")
        except (ssl.SSLError, OSError) as e:
            print(f"TLS disabled ({e}); serving plain HTTP on {port}")
    else:
        print(f"Web interface on http://0.0.0.0:{port}"
              + ("" if ssl is not None else " (ssl module unavailable)"))

    _web_server = server
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    # Plain-HTTP listener for the mobile app (Expo Go can't use the self-signed
    # cert). Same handler, LAN only. Disable with DISCSTATION_HTTP_PORT=0.
    try:
        http_port = int(os.environ.get("DISCSTATION_HTTP_PORT", "8081"))
    except ValueError:
        http_port = 8081
    if http_port and http_port != port:
        try:
            plain = socketserver.ThreadingTCPServer(('', http_port), _WebHandler, bind_and_activate=False)
            plain.allow_reuse_address = True  # must be set before bind, or a restart hits TIME_WAIT
            plain.daemon_threads = True
            plain.server_bind()
            plain.server_activate()
            threading.Thread(target=plain.serve_forever, daemon=True).start()
            print(f"Plain HTTP (mobile app) on http://0.0.0.0:{http_port}")
        except OSError as e:
            print(f"Plain HTTP listener not started on {http_port}: {e}")

    return server


def local_ip():
    try:
        result = subprocess.run(['hostname', '-I'], capture_output=True, text=True, timeout=2)
        ips = result.stdout.strip().split()
        for ip in ips:
            if ip.count('.') == 3 and not ip.startswith('127.'):
                return ip
    except Exception:
        pass
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except Exception:
        return '127.0.0.1'
    finally:
        s.close()


def wait_for_web_url(ser):
    ip = local_ip()
    protocol = "https" if (discstation_host.config_dir() / 'server.crt').exists() else "http"
    url = f"{protocol}://{ip}:{_web_port}"
    safe_send(ser, f"IP:{url}")
    print(f"URL displayed: {url}")
    while True:
        line = read_serial_line(ser, timeout=0.5)
        if line and line.strip().upper() in ("CANCEL", "HOME"):
            safe_send(ser, "STANDBY:Insert disc")
            print("User cancelled URL input")
            return None
        try:
            url = _burn_url_queue.get(timeout=5)
            safe_send(ser, f"STATUS:Got URL, starting...")
            return url
        except Empty:
            safe_send(ser, "PING")
            check_serial_alive(ser)


MPV_SOCKET = (r"\\.\pipe\discstation-mpv" if os.name == "nt"
              else str(Path(tempfile.gettempdir()) / "discstation_mpv.sock"))
RIP_ROOT = discstation_burn.USER_HOME / "dvd_rips"
USER_AGENT = "DVDStation/0.1 (local appliance; phuju)"
DISC_POLL_SECONDS = 6

# Optional metadata libraries. If import/setup fails the code falls back to the
# hand-rolled requests-based lookups further down.
try:
    import libdiscid as _libdiscid
except Exception:
    _libdiscid = None

try:
    import musicbrainzngs as _mb
    _mb.set_useragent("DiscStation", "0.1", "https://github.com/phuju/dvd-station")
    _mb.set_rate_limit(1.0, 1)  # MB asks for <=1 req/s; replaces manual time.sleep(1)
except Exception:
    _mb = None

try:
    import discstation_meta  # TMDb video metadata (optional)
except Exception:
    discstation_meta = None


def _env_num(name, default, cast):
    try:
        return cast(os.environ.get(name, default))
    except (TypeError, ValueError):
        return cast(default)


# --- Disc-detection tuning -------------------------------------------------
# Probe timeouts are CEILINGS, not fixed costs: a healthy drive returns well
# under these. They are large because this appliance's USB optical bridge can
# need 5-15s on the first read after a disc loads. Every value is overridable
# via a DISCSTATION_* environment variable (set them in the systemd unit).
PROBE_TIMEOUT_UDEV = _env_num("DISCSTATION_PROBE_TIMEOUT_UDEV", 4, int)
PROBE_TIMEOUT_BLKID = _env_num("DISCSTATION_PROBE_TIMEOUT_BLKID", 8, int)
PROBE_TIMEOUT_LSDVD = _env_num("DISCSTATION_PROBE_TIMEOUT_LSDVD", 8, int)
PROBE_TIMEOUT_WODIM_TOC = _env_num("DISCSTATION_PROBE_TIMEOUT_WODIM_TOC", 12, int)
PROBE_TIMEOUT_MEDIAINFO = _env_num("DISCSTATION_PROBE_TIMEOUT_MEDIAINFO", 12, int)

# Post-insert settle wait + classification retry budget.
DISC_SETTLE_TIMEOUT = _env_num("DISCSTATION_DISC_SETTLE_TIMEOUT", 8, int)
DISC_SETTLE_POLL = _env_num("DISCSTATION_DISC_SETTLE_POLL", 1.0, float)
DISC_DETECT_RETRIES = _env_num("DISCSTATION_DISC_DETECT_RETRIES", 2, int)
DISC_DETECT_RETRY_DELAY = _env_num("DISCSTATION_DISC_DETECT_RETRY_DELAY", 2.0, float)
DISC_DETECT_BUDGET = _env_num("DISCSTATION_DISC_DETECT_BUDGET", 18, int)
DISC_DETECT_CACHE_TTL = _env_num("DISCSTATION_DISC_DETECT_CACHE_TTL", 5.0, float)

_NO_MEDIA_MARKERS = (
    "no medium", "no disk", "no disc", "cannot load media",
    "tray open", "medium not present",
)

DiscInfo = collections.namedtuple(
    "DiscInfo",
    "present kind capacity_bytes label web_type transient failed_probes",
)


def _disc_info(present=False, kind="none", capacity_bytes=0, label="",
               web_type="none", transient=False, failed_probes=()):
    return DiscInfo(bool(present), kind, int(capacity_bytes or 0), label or "",
                    web_type, bool(transient), tuple(failed_probes))


def _web_type_for(kind, capacity_bytes):
    simple = {
        "blank": "BLANK", "audio_cd": "AUDIO_CD", "data_cd": "DATA_CD",
        "vcd": "VCD", "svcd": "SVCD", "video_data": "VIDEO", "none": "none",
    }
    if kind in simple:
        return simple[kind]
    if kind in ("dvd_video", "data_disc"):
        if capacity_bytes and capacity_bytes > 6_000_000_000:
            return "DVD9"
        if capacity_bytes:
            return "DVD5"
        return "DVD-Video" if kind == "dvd_video" else "DATA_DISC"
    return "UNKNOWN"


def _udev_dvd_recordable(props):
    return any(props.get(k) == "1" for k in (
        "ID_CDROM_MEDIA_DVD_PLUS_R", "ID_CDROM_MEDIA_DVD_R",
        "ID_CDROM_MEDIA_DVD_PLUS_R_DL", "ID_CDROM_MEDIA_DVD_R_DL",
        "ID_CDROM_MEDIA_DVD_RW", "ID_CDROM_MEDIA_DVD_PLUS_RW",
    ))


def ensure_text(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="ignore")
    return str(value)


# --- Server-Sent Events: push status/progress to browsers the instant it changes
_sse_subs = set()          # of Queue
_sse_lock = threading.Lock()


def _status_snapshot():
    return {"status": _web_status or "READY", "progress": _web_progress, "active": _web_progress_active}


def _sse_publish(event):
    payload = json.dumps(event)
    with _sse_lock:
        for q in list(_sse_subs):
            try:
                q.put_nowait(payload)
            except Full:
                _sse_subs.discard(q)


def _set_web_progress(phase, percent=-1):
    global _web_status, _web_progress, _web_progress_active
    _web_status = phase
    _web_progress = max(-1, min(100, int(percent))) if percent is not None else -1
    _web_progress_active = True
    if _active_ser:
        discstation_burn.safe_send(_active_ser, f"STATUS:{phase}")
        if _web_progress >= 0:
            discstation_burn.safe_send(_active_ser, f"PROGRESS:{_web_progress}%")
    _sse_publish(_status_snapshot())


def _record_web_status(msg):
    global _web_status, _web_progress, _web_progress_active
    if msg.startswith("DISC:"):
        _sse_publish({"type": "disc-changed"})
        return
    if msg.startswith("STATUS:"):
        _web_status = msg[7:].strip() or "READY"
        _web_progress_active = True
    elif msg.startswith("PROGRESS:"):
        value = msg[9:].strip()
        _web_status = f"BURNING {value}"
        match = re.search(r"(\d+(?:\.\d+)?)", value)
        if match:
            _web_progress = min(100, max(0, int(float(match.group(1)))))
        _web_progress_active = True
    elif msg.startswith("DONE:"):
        _web_status = msg[5:].strip() or "DONE"
        _web_progress = 100
        _web_progress_active = False
    elif msg.startswith("ERROR:"):
        _web_status = msg[6:].strip() or "ERROR"
        _web_progress_active = False
    elif msg.startswith("CANCELLED:"):
        _web_status = msg[10:].strip() or "CANCELLED"
        _web_progress_active = False
    elif msg.startswith(("STANDBY:", "HOME:")):
        # idle again (tray open, insert disc, back to the menu) — clear any
        # lingering "Ejecting..." / progress state on the web UI.
        text = msg.split(":", 1)[1].strip()
        _web_status = "READY" if text in ("", "DiscStation", "Select mode", "Starting...") else text
        _web_progress = -1
        _web_progress_active = False
    else:
        return
    _sse_publish(_status_snapshot())


def send(ser, msg):
    discstation_burn.send(ser, msg)


def safe_send(ser, msg):
    discstation_burn.safe_send(ser, msg)


# Route every serial line the burn/rip pipeline emits into the web/SSE status.
discstation_burn.status_sink = _record_web_status


def run_as_desktop_user(cmd):
    if os.name != "posix" or pwd is None:
        return cmd
    sudo_user = os.environ.get("SUDO_USER")
    if os.geteuid() == 0 and sudo_user and sudo_user != "root":
        home = Path(discstation_burn.USER_HOME)
        uid = pwd.getpwnam(sudo_user).pw_uid
        return [
            "sudo", "-u", sudo_user,
            "env",
            "DISPLAY=" + os.environ.get("DISPLAY", ":0"),
            "XAUTHORITY=" + str(home / ".Xauthority"),
            "XDG_RUNTIME_DIR=/run/user/" + str(uid),
            *cmd,
        ]
    return cmd


def chown_to_sudo_user(path):
    sudo_user = os.environ.get("SUDO_USER")
    if os.name != "posix" or pwd is None or not hasattr(os, "geteuid"):
        return
    if os.geteuid() != 0 or not sudo_user or sudo_user == "root":
        return

    try:
        pw_record = pwd.getpwnam(sudo_user)
    except KeyError:
        return

    uid = pw_record.pw_uid
    gid = pw_record.pw_gid
    root_path = Path(path)

    for current_root, dirs, files in os.walk(root_path):
        try:
            os.chown(current_root, uid, gid)
        except OSError:
            pass
        for name in dirs + files:
            item = Path(current_root) / name
            try:
                os.chown(item, uid, gid)
            except OSError:
                pass


def _mpv_ipc(payload, timeout=None):
    """Send one JSON line to mpv's IPC endpoint. Windows = named pipe, POSIX =
    AF_UNIX socket. Returns the raw reply bytes (b"" if not read), or raises OSError."""
    if os.name == "nt":
        with open(MPV_SOCKET, "r+b", buffering=0) as pipe:
            pipe.write(payload)
            if timeout is None:
                return b""
            return pipe.read(4096) or b""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        if timeout is not None:
            sock.settimeout(timeout)
        sock.connect(MPV_SOCKET)
        sock.sendall(payload)
        return sock.recv(4096) if timeout is not None else b""


def mpv_command(command):
    try:
        _mpv_ipc(json.dumps({"command": command}).encode() + b"\n")
    except OSError:
        return False
    return True


def mpv_query(command):
    try:
        payload = json.dumps({"command": command, "request_id": 1}).encode() + b"\n"
        response = json.loads(_mpv_ipc(payload, timeout=0.5).decode(errors="ignore"))
        return response.get("data")
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def wait_for_socket(path, proc, timeout=8):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            _mpv_ipc(b'{"command":["get_property","idle-active"]}\n', timeout=0.25)
            return True
        except OSError:
            pass
        time.sleep(0.1)
    return False


def wait_for_button(ser):
    last_ping = time.time()
    while True:
        line = read_serial_line(ser, timeout=0.1)
        if line:
            return line
        now = time.time()
        if now - last_ping >= 5:
            last_ping = now
            safe_send(ser, "PING")
            check_serial_alive(ser)


_line_buf = b""


def read_serial_line(ser, timeout=0.1):
    global _line_buf
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _line_buf:
            _line_buf = _line_buf.lstrip(b'\r\n')
            if _line_buf:
                idx = _line_buf.find(b"\n")
                if idx >= 0:
                    line = _line_buf[:idx]
                    _line_buf = _line_buf[idx + 1:]
                    decoded = line.decode(errors="ignore").strip() or None
                    if decoded:
                        discstation_burn.note_serial_activity()
                    return decoded
        try:
            waiting = getattr(ser, "in_waiting", None)
            if waiting is not None:
                chunk = ser.read(min(4096, waiting)) if waiting else b""
            elif hasattr(ser, "fd"):
                chunk = os.read(ser.fd, 4096)
            else:
                chunk = b""
        except serial.SerialException:
            raise
        except OSError as e:
            raise serial.SerialException(f"serial read failed: {e}") from e
        except AttributeError:
            return None
        if chunk:
            chunk = _line_buf + chunk
            _line_buf = b""
            idx = chunk.find(b"\n")
            if idx >= 0:
                _line_buf = chunk[idx + 1:]
                chunk = chunk[:idx]
                decoded = chunk.decode(errors="ignore").strip() or None
                if decoded:
                    discstation_burn.note_serial_activity()
                return decoded
            _line_buf = chunk
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(remaining, 0.05))
    return None


def check_serial_alive(ser=None):
    """Raise serial.SerialException if the ESP32 link looks dead, so main()'s
    reconnect loop can re-scan for the (possibly renumbered) serial port.
    Call this inside any long poll loop that would otherwise spin forever on a
    stale handle (writes to a re-enumerated /dev/ttyUSBN fail silently)."""
    if discstation_burn.serial_write_failed():
        raise serial.SerialException("serial write failed (ESP32 link lost)")
    if discstation_burn.serial_activity_age() >= 35:
        raise serial.SerialException("ESP32 not responding")


def send_disc_info(ser, device, status_line=None):
    if status_line is None:
        status_line = disc_status_line(device)
    send(ser, f"DISC:{status_line}")
    title = disc_title(device)
    safe_send(ser, f"DISC_NAME:{title}")
    items = menu_items_for_disc(device)
    safe_send(ser, f"MENU_ITEMS:{','.join(items)}")

def show_home(ser):
    send(ser, "HOME:Select mode")


def refresh_main_menu(ser):
    """Restore the disc menu after a temporary picker changes MENU_ITEMS."""
    try:
        device = discstation_burn.disc_device()
        send_disc_info(ser, device)
    except Exception as e:
        print(f"Menu refresh error: {e}")
    show_home(ser)


def show_standby(ser):
    safe_send(ser, "STANDBY:DiscStation")


def eject_disc(ser, device):
    global _tray_open, _tray_open_since
    print(f"Ejecting disc from {device}")
    if discstation_host.system_name() != "linux":
        try:
            ok = discstation_host.eject_device(device)
        except Exception as e:
            print(f"Cross-platform eject error: {e}")
            ok = False
        if ok:
            _tray_open = True
            _tray_open_since = time.monotonic()
            safe_send(ser, "STANDBY:Tray open")
        else:
            safe_send(ser, "ERROR:Eject failed")
            safe_send(ser, "STANDBY:Insert disc")
        return ok
    subprocess.run(["sync"], timeout=5)

    subprocess.run(["sg_raw", device, "1e", "00", "00", "00", "00", "00"],
                   timeout=5, capture_output=True)

    ok = False
    for cmd in (["eject", device], ["sg_raw", device, "1b", "00", "00", "00", "02", "00"]):
        if ok:
            break
        try:
            r = subprocess.run(cmd, timeout=10, capture_output=True)
            ok = r.returncode == 0
            if ok:
                print(f"{cmd[0]} eject ok")
            else:
                err = (r.stderr or r.stdout or b"failed").decode(errors="ignore").strip()[:40]
                print(f"{cmd[0]} eject failed: {err}")
        except Exception as e:
            print(f"{cmd[0]} eject error: {e}")

    if ok:
        _tray_open = True
        _tray_open_since = time.monotonic()
        safe_send(ser, "WAITING:Press SELECT/to close tray")
        last_ping = time.time()
        deadline = time.time() + 60
        tray_was_cancelled = False
        last_status_check = 0
        # Let the eject settle before touching the drive again (the reclose guard).
        settle_until = time.time() + 3
        while time.time() < deadline:
            if time.time() - last_ping >= 5:
                last_ping = time.time()
                safe_send(ser, "PING")
            if time.time() >= settle_until and time.time() - last_status_check >= 1.5:
                last_status_check = time.time()
                if drive_status(device) in ("disc", "no_disc"):
                    print("Tray closed — continuing")
                    _tray_open = False
                    break
            line = read_serial_line(ser, timeout=0.1)
            if not line:
                continue
            if line == "PONG":
                continue
            if line == "CONFIRM":
                print("Closing tray...")
                safe_send(ser, "STATUS:Closing tray...")
                for close_cmd in (
                    ["eject", "-t", device],
                    ["sg_raw", device, "1b", "00", "00", "00", "03", "00"],
                ):
                    try:
                        r = subprocess.run(close_cmd, timeout=10, capture_output=True)
                        if r.returncode == 0:
                            _tray_open = False
                            break
                    except Exception:
                        pass
                time.sleep(2)
                break
            if line == "CANCEL":
                print("Tray left open")
                tray_was_cancelled = True
                break
        else:
            print("Tray close timed out")
            tray_was_cancelled = True
    else:
        tray_was_cancelled = False
        safe_send(ser, "ERROR:Eject failed")
    if tray_was_cancelled:
        safe_send(ser, "STANDBY:Tray open")
    else:
        safe_send(ser, "STANDBY:Insert disc")
    return ok


HISTORY_FILE = discstation_burn.WORK / "burn_history.jsonl"


def append_burn_history(entry):
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def run_probe(cmd, timeout=8, name=None):
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(cmd, 124, ensure_text(e.stdout), ensure_text(e.stderr))
    except (FileNotFoundError, OSError) as e:
        return subprocess.CompletedProcess(cmd, 127, "", str(e))


try:
    import pyudev as _pyudev
    _UDEV_CTX = _pyudev.Context()
except Exception:  # pyudev missing, or libudev not loadable
    _pyudev = None
    _UDEV_CTX = None


def _udev_props_via_pyudev(device):
    """Return the udev property dict for `device` via pyudev, or None if pyudev
    is unavailable / errored (caller then falls back to `udevadm info`)."""
    if _pyudev is None:
        return None
    try:
        dev = _pyudev.Devices.from_device_file(_UDEV_CTX, device)
        return dict(dev.properties)
    except Exception:
        return None


_CDROM_ID_BIN = None


def _cdrom_id_path():
    global _CDROM_ID_BIN
    if _CDROM_ID_BIN is None:
        _CDROM_ID_BIN = ""
        for cand in ("/usr/lib/udev/cdrom_id", "/lib/udev/cdrom_id"):
            if Path(cand).exists():
                _CDROM_ID_BIN = cand
                break
    return _CDROM_ID_BIN


def _refresh_udev(device):
    """Best-effort re-probe so ID_CDROM_MEDIA* reflects the disc that is in the
    drive *now*. USB ATAPI bridges frequently emit no media-change uevent, so
    `udevadm info` otherwise serves stale properties from the last change.
    Never raises; returns a dict of freshly read ID_CDROM*/ID_FS* keys."""
    if discstation_host.system_name() != "linux":
        return {}
    try:
        run_probe(
            ["udevadm", "trigger", "--settle", "--subsystem-match=block",
             "--name-match", Path(device).name],
            name="udevadm-trigger", timeout=5,
        )
    except Exception:
        pass
    extra = {}
    binpath = _cdrom_id_path()
    if binpath:
        r = run_probe([binpath, device], name="cdrom_id", timeout=PROBE_TIMEOUT_UDEV)
        if r.returncode == 0:
            for line in ensure_text(r.stdout).splitlines():
                line = line.strip()
                if "=" in line and (line.startswith("ID_CDROM") or line.startswith("ID_FS")):
                    key, value = line.split("=", 1)
                    extra[key] = value
    return extra


def udev_cdrom_properties(device, refresh=False):
    if discstation_host.system_name() != "linux":
        return discstation_host.media_properties(device)
    overlay = _refresh_udev(device) if refresh else {}
    properties = _udev_props_via_pyudev(device)
    if properties is None:
        # pyudev unavailable — fall back to parsing `udevadm info` output.
        result = run_probe(
            ["udevadm", "info", "--query=property", "--name", device],
            name="udevadm-info", timeout=PROBE_TIMEOUT_UDEV,
        )
        properties = {}
        if result.returncode == 0:
            for line in ensure_text(result.stdout).splitlines():
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                properties[key] = value
    properties.update(overlay)  # a fresh cdrom_id read wins over the cached udev db
    return properties


_tray_open = False
_tray_open_since = 0.0  # time.monotonic() of the last OLED-initiated eject


def _device_present(device):
    """Is `device` still a live drive node? On Linux/macOS that's a real
    filesystem path that can disappear (e.g. after an eject) - Path.exists()
    answers that correctly. On Windows `device` is a bare drive letter ("D:");
    Path("D:").exists() raises OSError (WinError 1) instead of returning False,
    and the drive letter is stable regardless of media state anyway (the real
    presence signal is ID_CDROM_MEDIA, checked downstream via media_properties())."""
    if not device:
        return False
    if discstation_host.system_name() == "windows":
        return True
    try:
        return Path(device).exists()
    except OSError:
        return False


def _tray_closed_with_disc(device):
    if not device:
        return False
    global _tray_open
    if not _device_present(device):
        return False
    properties = udev_cdrom_properties(device)
    if properties.get("ID_CDROM_MEDIA") == "1":
        _tray_open = False
        return True
    if properties.get("ID_CDROM_MEDIA_STATE") == "blank":
        _tray_open = False
        return True
    return False


def disc_present(device):
    if not device:
        return False
    if _tray_closed_with_disc(device):
        return True
    if _tray_open:
        return False
    if not _device_present(device):
        return False
    if discstation_host.system_name() != "linux":
        properties = udev_cdrom_properties(device)
        return properties.get("ID_CDROM_MEDIA") == "1"

    toc = run_probe(["wodim", "-toc", "dev=" + device], name="wodim-toc", timeout=PROBE_TIMEOUT_WODIM_TOC)
    toc_text = ensure_text(toc.stdout) + ensure_text(toc.stderr)
    no_media_markers = _NO_MEDIA_MARKERS
    if toc.returncode == 124:
        return is_blank_disc(device)
    if toc_text.strip() and any(marker in toc_text.lower() for marker in no_media_markers):
        return False

    dvd = run_probe(["lsdvd", device], name="lsdvd", timeout=PROBE_TIMEOUT_LSDVD)
    if dvd.returncode == 0:
        return True

    fs = run_probe(["blkid", "-o", "value", "-s", "TYPE", device], name="blkid", timeout=PROBE_TIMEOUT_BLKID)
    if fs.returncode == 0 and ensure_text(fs.stdout).strip():
        return True

    if "first:" in toc_text and "track:" in toc_text:
        return True

    if toc_text.strip() and not any(marker in toc_text.lower() for marker in no_media_markers):
        return True

    return is_blank_disc(device)


def is_blank_disc(device):
    if not device:
        return False
    if not _device_present(device):
        return False

    properties = udev_cdrom_properties(device, refresh=True)
    media = properties.get("ID_CDROM_MEDIA")
    if media == "0":
        return False
    if discstation_host.system_name() != "linux":
        return properties.get("ID_CDROM_MEDIA_STATE") == "blank"

    # dvd+rw-mediainfo talks to the drive directly and reliably reports blank
    # status even when udev's media flag is stale/missing on this USB bridge.
    info = run_probe(["dvd+rw-mediainfo", device], name="mediainfo", timeout=PROBE_TIMEOUT_MEDIAINFO)
    if info.returncode == 0:
        # dvd+rw-mediainfo pads its labels ("Disc status:           blank"), so
        # collapse runs of whitespace before matching.
        text = re.sub(r"\s+", " ", ensure_text(info.stdout).lower())
        if "disc status: blank" in text or "disc status: empty" in text:
            return True
        if "state of last session: empty" in text:
            return True
        if "disc status:" in text:
            return False

    if properties.get("ID_CDROM_MEDIA_STATE") == "blank":
        return True

    # The weaker "no filesystem / no TOC => blank" evidence below misfires on an
    # empty tray, so only trust it once udev confirms a disc is actually loaded.
    if media != "1":
        return False

    fs = run_probe(["blkid", "-o", "value", "-s", "TYPE", device], name="blkid", timeout=PROBE_TIMEOUT_BLKID)
    if fs.returncode == 0 and ensure_text(fs.stdout).strip():
        return False

    dvd = run_probe(["lsdvd", device], name="lsdvd", timeout=PROBE_TIMEOUT_LSDVD)
    if dvd.returncode == 0:
        return False

    toc = run_probe(["wodim", "-toc", "dev=" + device], name="wodim-toc", timeout=PROBE_TIMEOUT_WODIM_TOC)
    toc_text = ensure_text(toc.stdout) + ensure_text(toc.stderr)
    if "first:" in toc_text and "track:" in toc_text:
        return False

    return True


def is_rewritable_disc(device):
    if not device:
        return False
    """Return whether the inserted medium can be overwritten."""
    if not _device_present(device):
        return False

    properties = udev_cdrom_properties(device)
    if any(properties.get(key) == "1" for key in (
        "ID_CDROM_MEDIA_CD_RW",
        "ID_CDROM_MEDIA_DVD_RW",
        "ID_CDROM_MEDIA_DVD_RW_SEQ",
        "ID_CDROM_MEDIA_DVD_PLUS_RW",
    )):
        return True

    if discstation_host.system_name() == "linux":
        info = run_probe(["dvd+rw-mediainfo", device], timeout=3)
        text = ensure_text(info.stdout) + ensure_text(info.stderr)
        media_line = next(
            (line.lower() for line in text.splitlines() if "mounted media:" in line.lower()),
            "",
        )
        return "rw" in media_line
    # Non-Linux: the RW signal comes from the ID_CDROM_MEDIA_*_RW keys above,
    # which discstation_host.media_properties() tags from the drutil/diskutil
    # media type.
    return False


def can_burn_disc(device):
    return is_blank_disc(device) or is_rewritable_disc(device)


_DISC_LABELS = {
    "audio_cd": "Disc: Audio CD",
    "dvd_video": "Disc: DVD-Video",
    "vcd": "Disc: VCD",
    "svcd": "Disc: SVCD",
    "video_data": "Disc: Video data",
    "data_disc": "Disc: Data disc",
    "data_cd": "Disc: Data CD",
    "blank": "Disc: Blank",
    "unknown": "Disc: unknown",
}


def disc_status_line(device):
    try:
        info = detect_disc(device)
    except Exception as e:
        print(f"disc status error: {e}")
        return "Disc: reading..."
    if not info.present:
        return "Disc: none"
    if info.transient:
        return "Disc: reading..."
    return _DISC_LABELS.get(info.kind, "Disc: unknown")


def disc_kind(device):
    try:
        return detect_disc(device).kind
    except Exception as e:
        print(f"disc kind error: {e}")
        return "unknown"


# ---------------------------------------------------------------------------
# Shared disc-detection core. Both the ESP32/LCD path (disc_status_line /
# disc_kind) and the web path (_serve_disc_info) go through detect_disc(), so
# they always agree. A transient USB timeout yields a retry and then
# "reading..." rather than a sticky, wrong "unknown".
# ---------------------------------------------------------------------------

_detect_cache = {}
_detect_lock = threading.Lock()
_stuck_cycles = {}


_CDROM_DRIVE_STATUS = 0x5326  # CDS_NO_DISC=1  TRAY_OPEN=2  DRIVE_NOT_READY=3  DISC_OK=4


def drive_status(device):
    """Fast, reliable drive state via the CDROM_DRIVE_STATUS ioctl.

    Returns 'disc' | 'no_disc' | 'open' | 'loading' | 'unknown'. Single ioctl on
    an O_NONBLOCK fd — does not consult the (stale on this USB bridge) udev db and
    does not disturb the tray. Never raises."""
    if not device:
        return "unknown"
    if discstation_host.system_name() != "linux":
        return discstation_host.drive_status()
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return "unknown"
    try:
        st = fcntl.ioctl(fd, _CDROM_DRIVE_STATUS, 0)
    except OSError:
        return "unknown"
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    return {4: "disc", 1: "no_disc", 2: "open", 3: "loading"}.get(st, "unknown")


def _media_quick_state(device, props):
    """Fast, cheap read of whether a disc is loaded. No long probes."""
    if not device:
        return "empty"
    if _tray_open:
        return "empty"  # deliberate OLED eject — do not poke the drive
    st = drive_status(device)
    if st in ("open", "no_disc"):
        return "empty"
    if st == "disc":
        return "present"
    if st == "loading":
        return "unsure"
    # st == "unknown": fall through to the legacy probes below
    try:
        if not _device_present(device):
            return "empty"
    except OSError:
        return "empty"
    if props.get("ID_CDROM_MEDIA") == "1" or props.get("ID_CDROM_MEDIA_STATE") == "blank":
        return "present"
    r = run_probe(["wodim", "-toc", "dev=" + device], name="wodim-toc",
                  timeout=min(4, PROBE_TIMEOUT_WODIM_TOC))
    txt = (ensure_text(r.stdout) + ensure_text(r.stderr)).lower()
    if "first:" in txt and "track:" in txt:
        return "present"
    if txt.strip() and any(m in txt for m in _NO_MEDIA_MARKERS):
        return "empty"
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as e:
        return "empty" if e.errno in (errno.ENOMEDIUM, errno.ENXIO) else "unsure"
    try:
        os.read(fd, 2048)
        return "present"
    except OSError as e:
        # EIO happens on a perfectly good audio CD, so it is NOT proof of "empty".
        return "empty" if e.errno in (errno.ENOMEDIUM, errno.ENXIO) else "unsure"
    finally:
        os.close(fd)


def wait_for_disc_ready(device, timeout=None):
    """Block until the drive reports a stable media state after a tray close.
    Returns (status, elapsed) where status is 'empty' | 'ready' | 'timeout'."""
    if timeout is None:
        timeout = DISC_SETTLE_TIMEOUT
    if discstation_host.system_name() != "linux":
        return "ready", 0.0
    start = time.monotonic()
    while True:
        props = udev_cdrom_properties(device, refresh=True)
        state = _media_quick_state(device, props)
        elapsed = time.monotonic() - start
        if state == "empty":
            return "empty", elapsed
        if state == "present":
            return "ready", elapsed
        if elapsed >= timeout:
            return "timeout", elapsed
        time.sleep(DISC_SETTLE_POLL)


def _priv_mount_error(exc):
    s = str(exc).lower()
    return any(m in s for m in (
        "must be superuser", "permission denied", "only root",
        "operation not permitted", "are you root",
    ))


def _probe(name, cmd, timeout, deadline, failed):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        failed.append(f"{name}:timeout")
        return subprocess.CompletedProcess(cmd, 124, "", "")
    r = run_probe(cmd, name=name, timeout=max(1, int(min(timeout, remaining))))
    if r.returncode == 124:
        failed.append(f"{name}:timeout")
    elif r.returncode == 127:
        failed.append(f"{name}:missing")
    return r


def _cd_kind_from_toc(toc_text):
    """audio_cd vs data_cd from a `wodim -toc` dump (lowercased stdout+stderr).

    Decided by the per-track CONTROL field: bit 2 (0x04) set == data track.
    The old `"control: 2" in text` test only caught the "digital copy permitted"
    bit, so plain audio CDs (every track `control: 0`, including this appliance's
    own cdrdao burns) were misread as data_cd."""
    controls = [int(c) for c in re.findall(
        r"track:\s*\d+\b[^\n]*?control:\s*(\d+)", toc_text)]
    if not controls:
        return "audio_cd"  # readable track TOC, no filesystem -> almost always audio
    if all(c & 0x04 for c in controls):
        return "data_cd"
    return "audio_cd"  # >=1 audio track (incl. mixed-mode / CD-Extra)


def _classify_disc(device, props, failed, deadline):
    """One pass of the probe chain. Mirrors the historical disc_kind ordering
    (blkid -> lsdvd -> wodim -toc -> fs fallback -> blank) but records which
    probes timed out / were missing so the caller can retry."""
    if discstation_host.system_name() != "linux":
        if not props.get("ID_CDROM_MEDIA"):
            # media_properties() returned nothing -> no disc loaded (this is the
            # only "no media" signal on a platform like Windows where the drive
            # letter/device path exists whether or not media is present).
            return _disc_info(False, "none")
        if props.get("ID_CDROM_MEDIA_TYPE") == "audio":
            return _disc_info(True, "audio_cd", web_type="AUDIO_CD")
        if props.get("ID_FS_TYPE") in ("udf", "iso9660"):
            kind = None
            try:
                with mounted_disc(device) as mount_dir:
                    if (mount_dir / "VIDEO_TS").is_dir():
                        kind = "dvd_video"
                    else:
                        k, _ = disc_video_files(mount_dir)
                        kind = k
            except (OSError, RuntimeError) as e:
                print(f"Disc inspection failed: {e}")
            kind = kind or "data_disc"
            return _disc_info(True, kind, web_type=_web_type_for(kind, 0))
        if props.get("ID_CDROM_MEDIA_STATE") == "blank":
            try:
                cap = discstation_burn.disc_capacity_bytes(device) or 0
            except Exception:
                cap = 0
            return _disc_info(True, "blank", capacity_bytes=cap, web_type="BLANK")
        return _disc_info(True, "unknown", web_type="UNKNOWN", failed_probes=failed)

    # Fast path for a blank disc: cdrom_id reports this reliably in ~20ms, and
    # blkid/lsdvd/wodim all legitimately fail on blank media — running them here
    # is just an opportunity to time out on a slow drive.
    if props.get("ID_CDROM_MEDIA_STATE") == "blank":
        cap = 0
        if (deadline - time.monotonic()) > 8:
            try:
                cap = discstation_burn.disc_capacity_bytes(device) or 0
            except Exception:
                cap = 0
        return _disc_info(True, "blank", capacity_bytes=cap, web_type="BLANK")

    fs = _probe("blkid", ["blkid", "-o", "value", "-s", "TYPE", device],
                PROBE_TIMEOUT_BLKID, deadline, failed)
    fstype = ensure_text(fs.stdout).strip() if fs.returncode == 0 else ""

    kind = None
    if fstype:
        if time.monotonic() < deadline:
            try:
                with mounted_disc(device) as mount_dir:
                    k, _ = disc_video_files(mount_dir)
                    if k:
                        kind = k
            except (OSError, RuntimeError) as e:
                if not _priv_mount_error(e):
                    failed.append("mount:error")
        if kind is None:
            kind = "dvd_video" if fstype == "udf" else "data_disc"

    dvd = None
    if kind is None:
        dvd = _probe("lsdvd", ["lsdvd", device], PROBE_TIMEOUT_LSDVD, deadline, failed)
        if dvd.returncode == 0:
            kind = "dvd_video"

    toc_text = ""
    if kind is None:
        toc = _probe("wodim-toc", ["wodim", "-toc", "dev=" + device],
                     PROBE_TIMEOUT_WODIM_TOC, deadline, failed)
        toc_text = (ensure_text(toc.stdout) + ensure_text(toc.stderr)).lower()
        if "first:" in toc_text and "track:" in toc_text:
            kind = _cd_kind_from_toc(toc_text)

    toc_has_tracks = "first:" in toc_text and "track:" in toc_text
    media_present = (
        props.get("ID_CDROM_MEDIA") == "1"
        or bool(fstype)
        or (dvd is not None and dvd.returncode == 0)
        or toc_has_tracks
    )
    no_media = (
        bool(toc_text.strip())
        and any(m in toc_text for m in _NO_MEDIA_MARKERS)
        and not toc_has_tracks
    )

    if kind is None:
        if no_media and not media_present:
            return _disc_info(False, "none", failed_probes=failed)
        if is_blank_disc(device):
            kind = "blank"

    if kind is None:
        present = media_present or not no_media
        return _disc_info(present, "unknown", web_type="UNKNOWN",
                          transient=bool(failed), failed_probes=failed)

    # We have a definite kind — unrelated probe timeouts no longer make it transient.
    capacity = 0
    want_cap = kind in ("dvd_video", "data_disc", "blank") or _udev_dvd_recordable(props)
    if want_cap and (deadline - time.monotonic()) > 8:
        try:
            capacity = discstation_burn.disc_capacity_bytes(device) or 0
        except Exception as e:
            print(f"Disc capacity probe failed: {e}")
    if not capacity:
        bd = run_probe(["blockdev", "--getsize64", device], name="blockdev",
                       timeout=PROBE_TIMEOUT_UDEV)
        if bd.returncode == 0:
            try:
                capacity = int(ensure_text(bd.stdout).strip())
            except ValueError:
                capacity = 0

    label = props.get("ID_FS_LABEL", "")
    if kind == "dvd_video" and not label:
        if dvd is None:
            dvd = _probe("lsdvd", ["lsdvd", device], PROBE_TIMEOUT_LSDVD, deadline, failed)
        if dvd.returncode == 0:
            for line in ensure_text(dvd.stdout).splitlines():
                if line.startswith("Disc Title:"):
                    label = line.split(":", 1)[1].strip()
                    break

    return _disc_info(True, kind, capacity_bytes=capacity, label=label,
                      web_type=_web_type_for(kind, capacity),
                      failed_probes=failed)


def _maybe_reset_stuck_drive(device, failed_probes):
    if not any(p.endswith(":timeout") for p in failed_probes):
        _stuck_cycles[device] = 0
        return
    n = _stuck_cycles.get(device, 0) + 1
    _stuck_cycles[device] = n
    if n >= 2:
        print(f"Drive appears stuck ({n} cycles with probe timeouts), attempting USB reset...")
        try:
            discstation_burn.reset_drive(device)
        except Exception as e:
            print(f"USB reset failed: {e}")
        _stuck_cycles[device] = 0


def _detect_disc_locked(device, settle, budget):
    deadline = time.monotonic() + (DISC_DETECT_BUDGET if budget is None else budget)
    props = udev_cdrom_properties(device, refresh=True)

    if settle and discstation_host.system_name() == "linux":
        status, waited = wait_for_disc_ready(device)
        if status == "empty":
            info = _disc_info(False, "none")
            _detect_cache[device] = (time.monotonic(), info)
            return info
        props = udev_cdrom_properties(device, refresh=True)

    result = None
    for attempt in range(max(1, DISC_DETECT_RETRIES)):
        failed = []
        result = _classify_disc(device, props, failed, deadline)
        if result.kind != "unknown" and not result.transient:
            if result.failed_probes:
                print(f"disc classify: {result.kind} "
                      f"(partial probe failures: {list(result.failed_probes)})")
            _stuck_cycles[device] = 0
            _detect_cache[device] = (time.monotonic(), result)
            return result
        if (attempt < DISC_DETECT_RETRIES - 1
                and (deadline - time.monotonic()) > DISC_DETECT_RETRY_DELAY + 3):
            print(f"disc classify attempt {attempt + 1}: kind={result.kind} "
                  f"failed_probes={failed} — retrying")
            time.sleep(DISC_DETECT_RETRY_DELAY)
            props = udev_cdrom_properties(device, refresh=True)
        else:
            break

    if result is None:
        result = _disc_info(False, "none")
    if result.transient or result.failed_probes:
        print(f"disc classify: unresolved after {DISC_DETECT_RETRIES} attempts; "
              f"failed_probes={list(result.failed_probes)} — reporting as transient")
        _maybe_reset_stuck_drive(device, result.failed_probes)
        result = result._replace(kind="unknown", web_type="UNKNOWN", transient=True)
    elif result.kind == "unknown":
        print("disc classify: genuinely unknown (all probes ran, none matched)")
        _stuck_cycles[device] = 0
    _detect_cache[device] = (time.monotonic(), result)
    return result


def detect_disc(device, settle=True, budget=None, force=False):
    """Return a DiscInfo for the disc in `device`. Results are cached briefly so
    disc_status_line / disc_title / menu_items_for_disc in one refresh burst do a
    single probe. `settle=False` skips the post-insert wait (used by the web
    endpoint, which can just poll again)."""
    if not device:
        return _disc_info(False, "none")
    if _tray_open:
        # Tray was deliberately ejected from the OLED — report "no disc" without
        # touching the drive (cdrom_id / wodim / open() would re-close the tray).
        return _disc_info(False, "none")
    if not force:
        cached = _detect_cache.get(device)
        if cached and time.monotonic() - cached[0] < DISC_DETECT_CACHE_TTL:
            return cached[1]
    with _detect_lock:
        if not force:
            cached = _detect_cache.get(device)
            if cached and time.monotonic() - cached[0] < DISC_DETECT_CACHE_TTL:
                return cached[1]
        return _detect_disc_locked(device, settle, budget)


def disc_title(device):
    kind = disc_kind(device)
    if kind in ("data_disc", "data_cd", "video_data", "dvd_video"):
        properties = udev_cdrom_properties(device)
        label = properties.get("ID_FS_LABEL", "")
        if label:
            return label
        if kind != "dvd_video":
            return ""
    if kind == "dvd_video":
        dvd = run_probe(["lsdvd", device], timeout=5)
        if dvd.returncode == 0:
            for line in dvd.stdout.splitlines():
                if line.startswith("Disc Title:"):
                    t = line.split(":", 1)[1].strip()
                    if t:
                        return t
    elif kind == "audio_cd":
        try:
            toc = audio_cd_toc(device)
            if toc and toc.get("track_count"):
                n = toc["track_count"]
                leadout = toc.get("leadout", 0)
                tracks = toc.get("tracks", [])
                if tracks:
                    total_frames = leadout - tracks[0]
                    total_sec = int(total_frames / 75)
                else:
                    total_sec = 0
                fingerprint = f"{n}-{total_sec}"
                match = None
                if HISTORY_FILE.exists():
                    for line in open(HISTORY_FILE):
                        try:
                            entry = json.loads(line)
                            if entry.get("disc_type") == "Audio CD" and entry.get("fingerprint") == fingerprint:
                                match = entry
                                break
                        except Exception:
                            pass
                if match and match.get("title"):
                    return match["title"]
                return f"Audio CD ({n} tracks)"
        except Exception:
            pass
        return "Audio CD"
    elif kind == "vcd":
        return "VCD"
    elif kind == "svcd":
        return "SVCD"
    return ""


def audio_track_metadata(device):
    """Return track count and saved names for a DiscStation-burned CD."""
    try:
        toc = audio_cd_toc(device)
        track_count = int(toc.get("track_count", 0))
        tracks = toc.get("tracks", [])
        leadout = toc.get("leadout", 0)
        if not track_count or not tracks:
            return 0, [], []
        fingerprint = f"{track_count}-{int((leadout - tracks[0]) / 75)}"
        titles = []
        if HISTORY_FILE.exists():
            for line in reversed(HISTORY_FILE.read_text().splitlines()):
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if (entry.get("disc_type") == "Audio CD" and
                        entry.get("success") and entry.get("fingerprint") == fingerprint):
                    titles = entry.get("track_titles") or []
                    break
        starts = [int((position - tracks[0]) / 75) for position in tracks]
        return track_count, titles, starts
    except Exception:
        return 0, [], []


def menu_items_for_disc(device):
    kind = disc_kind(device)
    items = []
    if kind == "blank" or is_rewritable_disc(device):
        items = ["BURN", "BURN DATA", "BURN AUDIO"]
        had = discstation_burn.WORK.rglob("movie.mpg")
        if any(True for _ in had):
            items.append("BURN MPG")
    elif kind in ("dvd_video", "audio_cd", "vcd", "svcd", "video_data", "data_disc", "data_cd"):
        items = ["PLAY", "RIP"]
    else:
        items = ["PLAY", "RIP"]
    return items


class mounted_disc:
    def __init__(self, device):
        self.device = device
        self.tmp = None
        self.mount_path = None
        self.owned_mount = False

    def __enter__(self):
        if discstation_host.system_name() == "windows":
            # the optical disc is already mounted by the OS as its drive letter
            letter = str(self.device).rstrip("\\/").rstrip(":") + ":\\"
            self.mount_path = Path(letter)
            return self.mount_path
        if discstation_host.system_name() == "darwin":
            properties = discstation_host.media_properties(self.device)
            existing_mount = properties.get("ID_MOUNT_POINT")
            if existing_mount and Path(existing_mount).is_dir():
                self.mount_path = Path(existing_mount)
                return self.mount_path
            self.tmp = tempfile.TemporaryDirectory(prefix="discstation_disc_")
            command = [
                discstation_host.tool("hdiutil"), "attach", "-readonly", "-nobrowse",
                "-mountpoint", self.tmp.name, self.device,
            ]
        elif discstation_host.system_name() == "linux":
            self.tmp = tempfile.TemporaryDirectory(prefix="discstation_disc_")
            command = ["mount", "-o", "ro", self.device, self.tmp.name]
        else:
            self.tmp.cleanup()
            raise RuntimeError("Disc mounting backend is not configured for this operating system")
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            self.tmp.cleanup()
            raise RuntimeError((result.stderr or result.stdout or "Could not mount disc").strip())
        self.mount_path = Path(self.tmp.name)
        self.owned_mount = True
        return self.mount_path

    def __exit__(self, exc_type, exc, tb):
        if not self.owned_mount:
            return
        if discstation_host.system_name() == "darwin":
            subprocess.run(
                ["/usr/sbin/diskutil", "unmount", str(self.mount_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
        else:
            subprocess.run(["umount", str(self.mount_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.tmp.cleanup()


def disc_video_files(mount_dir):
    vcd_files = sorted((mount_dir / "MPEGAV").glob("*.DAT"))
    if vcd_files:
        return "vcd", vcd_files

    svcd_files = sorted((mount_dir / "MPEG2").glob("*.MPG"))
    if svcd_files:
        return "svcd", svcd_files

    video_exts = {".mpg", ".mpeg", ".mp4", ".mkv", ".avi", ".mov", ".webm", ".dat"}
    files = sorted(
        p for p in mount_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in video_exts
    )
    if files:
        return "video_data", files

    return None, []


def audio_cd_toc(device):
    if discstation_host.system_name() == "windows":
        rc, out, err = discstation_host._run_ps("audio-toc.ps1", device, timeout=25)
        info = {}
        for line in out.splitlines():
            if line.strip().startswith("{"):
                try:
                    info = json.loads(line)
                except ValueError:
                    pass
        tracks = info.get("tracks") or []
        if not tracks:
            raise RuntimeError(f"Could not read CD TOC: {(err or out)[:120]}")
        n = int(info["track_count"])
        return {
            "first_track": 1, "track_count": n, "leadout": int(info["leadout"]),
            "tracks": tracks,
            "toc": "+".join(map(str, [1, n, int(info["leadout"]), *tracks])),
        }
    if discstation_host.system_name() == "darwin":
        paranoia = None
        for name in ("cd-paranoia", "cdparanoia"):
            try:
                paranoia = discstation_burn.tool(name)
                break
            except FileNotFoundError:
                continue
        if not paranoia:
            raise RuntimeError("cd-paranoia not installed (brew install libcdio-paranoia)")
        result = subprocess.run(
            [paranoia, "-Q", "-d", rip_device(device)],
            capture_output=True, text=True, timeout=30,
        )
        text = ensure_text(result.stdout) + ensure_text(result.stderr)
        # "  1.    18288 [04:03.63]        0 [00:00.00]    no   no  2"
        begins, lengths = [], []
        for line in text.splitlines():
            match = re.match(r"\s*(\d+)\.\s+(\d+)\s+\[[\d:.]+\]\s+(\d+)\s+\[", line)
            if match:
                lengths.append(int(match.group(2)))
                begins.append(int(match.group(3)))
        if not begins:
            detail = next((l.strip() for l in reversed(text.splitlines()) if l.strip()), "cd-paranoia -Q failed")
            raise RuntimeError(f"Could not read macOS CD TOC: {detail[:100]}")
        tracks = [begin + 150 for begin in begins]           # LBA -> MB frame offset
        leadout = begins[-1] + lengths[-1] + 150
        return {
            "first_track": 1,
            "track_count": len(tracks),
            "leadout": leadout,
            "tracks": tracks,
            "toc": "+".join(map(str, [1, len(tracks), leadout, *tracks])),
        }
    if _libdiscid is not None:
        try:
            d = _libdiscid.read(device)
            tracks = list(d.track_offsets)
            if tracks:
                return {
                    "first_track": d.first_track,
                    "track_count": len(tracks),
                    "leadout": d.sectors,
                    "tracks": tracks,
                    "toc": d.toc,            # space-separated MB TOC string
                    "mb_discid": d.id,       # real MusicBrainz disc ID
                    "freedb_id": d.freedb_id,
                }
        except _libdiscid.DiscError as e:
            print(f"libdiscid read failed, falling back to wodim: {e}")

    toc = run_probe(["wodim", "-toc", "dev=" + device], timeout=8)
    text = ensure_text(toc.stdout) + ensure_text(toc.stderr)
    tracks = []
    leadout = None

    for line in text.splitlines():
        match = re.match(r"track:\s+(\d+)\s+lba:\s+(-?\d+)", line)
        if match:
            tracks.append(int(match.group(2)) + 150)

        match = re.match(r"track:lout\s+lba:\s+(-?\d+)", line)
        if match:
            leadout = int(match.group(1)) + 150

    if not tracks or leadout is None:
        raise RuntimeError("Could not read CD TOC")

    toc_string = "+".join(map(str, [1, len(tracks), leadout, *tracks]))
    return {
        "first_track": 1,
        "track_count": len(tracks),
        "leadout": leadout,
        "tracks": tracks,
        "toc": toc_string,
    }


def audio_cd_chapters(device):
    if discstation_host.system_name() in ("darwin", "windows"):
        toc = audio_cd_toc(device)
        tracks = toc["tracks"]
        first = tracks[0]
        leadout = toc["leadout"]
        chapters = []
        for index, start in enumerate(tracks):
            end = tracks[index + 1] if index + 1 < len(tracks) else leadout
            chapters.append({
                "start_time": (start - first) / 75,
                "end_time": (end - first) / 75,
                "tags": {"title": f"Track {index + 1:02d}"},
            })
        return chapters
    r = run_probe(
        [
            "ffprobe", "-v", "quiet", "-f", "libcdio", "-i", device,
            "-print_format", "json", "-show_chapters", "-show_format",
        ],
        timeout=15,
    )
    if r.returncode != 0:
        raise RuntimeError("Could not read audio CD")

    data = json.loads(r.stdout)
    return data.get("chapters", [])


def write_audio_tracks_file(out_dir, chapters):
    path = out_dir / "tracks.txt"
    with path.open("w") as f:
        for index, chapter in enumerate(chapters, start=1):
            start = float(chapter.get("start_time", 0))
            end = float(chapter.get("end_time", 0))
            title = chapter.get("tags", {}).get("title", f"track {index:02d}")
            f.write(f"{index:02d}\t{start:.3f}\t{end:.3f}\t{title}\n")
    return path


def safe_path_name(name):
    name = re.sub(r'[\\/:*?"<>|]+', "_", name.strip())
    name = re.sub(r"\s+", " ", name)
    return name[:120].strip(" ._") or "Unknown"


def unique_dir(path):
    if not path.exists():
        return path
    for index in range(2, 100):
        candidate = path.with_name(f"{path.name} ({index})")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{path.name} ({int(time.time())})")


_MB_RELEASE_INCLUDES = ["recordings", "artists", "artist-credits", "release-groups"]


def _mb_artist_phrase(credit):
    """Flatten a MusicBrainz artist-credit list into a display string."""
    if not credit:
        return ""
    out = []
    for part in credit:
        if isinstance(part, str):
            out.append(part)
        elif isinstance(part, dict):
            out.append((part.get("artist") or {}).get("name", "") or part.get("name", ""))
            out.append(part.get("joinphrase", ""))
    return "".join(out).strip()


def _release_meta(release, track_count, toc=None):
    """Extract our metadata dict from a MusicBrainz release, accepting both the
    musicbrainzngs shape (`medium-list`/`track-list`) and the raw ws/2 JSON
    shape (`media`/`tracks`)."""
    media = release.get("medium-list") or release.get("media") or []
    for medium in media:
        tracks = medium.get("track-list") or medium.get("tracks") or []
        if track_count and len(tracks) != track_count:
            continue

        album_artist = (
            release.get("artist-credit-phrase")
            or _mb_artist_phrase(release.get("artist-credit"))
            or "Unknown Artist"
        )
        date = release.get("date") or ""
        metadata = {
            "source": "musicbrainz",
            "release_id": release.get("id"),
            "release_group_id": (release.get("release-group") or {}).get("id"),
            "album": release.get("title") or "Unknown Album",
            "album_artist": album_artist,
            "date": date,
            "year": date[:4],
            "country": release.get("country") or "",
            "medium_position": medium.get("position", 1),
            "tracks": [],
            "toc": toc,
        }
        for index, track in enumerate(tracks, start=1):
            recording = track.get("recording") or {}
            artist = (
                track.get("artist-credit-phrase")
                or recording.get("artist-credit-phrase")
                or _mb_artist_phrase(track.get("artist-credit") or recording.get("artist-credit"))
                or album_artist
            )
            metadata["tracks"].append({
                "number": index,
                "title": track.get("title") or recording.get("title") or f"Track {index:02d}",
                "artist": artist,
                "recording_id": recording.get("id"),
                "release_track_id": track.get("id"),
            })
        return metadata

    return None


# Back-compat alias (older name).
metadata_from_musicbrainz_release = _release_meta


def musicbrainz_release_details(release_id):
    if _mb is not None:
        return _mb.get_release_by_id(
            release_id, includes=_MB_RELEASE_INCLUDES + ["media"],
        )["release"]
    response = requests.get(
        f"https://musicbrainz.org/ws/2/release/{release_id}",
        params={
            "inc": "recordings+artists+artist-credits+release-groups+media",
            "fmt": "json",
        },
        headers={"User-Agent": USER_AGENT},
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def musicbrainz_lookup(device, track_count):
    toc = audio_cd_toc(device)

    if _mb is not None and toc.get("mb_discid"):
        try:
            res = _mb.get_releases_by_discid(
                toc["mb_discid"], includes=["recordings", "artist-credits", "release-groups"],
                toc=toc.get("toc"), cdstubs=False,
            )
        except _mb.ResponseError as e:
            if getattr(getattr(e, "cause", None), "code", None) == 404:
                return None
            raise
        releases = res.get("disc", {}).get("release-list") or res.get("release-list") or []
        for release in releases:
            metadata = _release_meta(release, track_count, toc)
            if metadata:
                return metadata
        return None

    # --- fallback: raw ws/2 disc-id lookup ---
    response = requests.get(
        "https://musicbrainz.org/ws/2/discid/-",
        params={
            "toc": toc["toc"],
            "inc": "recordings+artists+artist-credits+release-groups",
            "fmt": "json", "cdstubs": "no", "media-format": "all",
        },
        headers={"User-Agent": USER_AGENT}, timeout=20,
    )
    response.raise_for_status()
    for release in response.json().get("releases", []):
        metadata = _release_meta(release, track_count, toc)
        if metadata:
            return metadata
    return None


def musicbrainz_lookup_by_album_hints(album_artist, album, track_count):
    if not album:
        return None

    if _mb is not None:
        fields = {"release": album}
        if album_artist:
            fields["artist"] = album_artist
        try:
            hits = _mb.search_releases(limit=8, **fields).get("release-list", [])
        except _mb.WebServiceError as e:
            print(f"MusicBrainz search failed: {e}")
            hits = []
        for hit in hits:
            release_id = hit.get("id")
            if not release_id:
                continue
            try:
                details = musicbrainz_release_details(release_id)
            except Exception:
                continue
            metadata = _release_meta(details, track_count)
            if metadata:
                metadata["source"] = "musicbrainz-search"
                return metadata
        return None

    # --- fallback: raw ws/2 search ---
    query_parts = [f'release:"{album}"']
    if album_artist:
        query_parts.append(f'artist:"{album_artist}"')
    response = requests.get(
        "https://musicbrainz.org/ws/2/release/",
        params={"query": " AND ".join(query_parts), "fmt": "json", "limit": 8},
        headers={"User-Agent": USER_AGENT}, timeout=20,
    )
    response.raise_for_status()
    for release in response.json().get("releases", []):
        release_id = release.get("id")
        if not release_id:
            continue
        try:
            details = musicbrainz_release_details(release_id)
        except Exception:
            continue
        metadata = _release_meta(details, track_count)
        if metadata:
            metadata["source"] = "musicbrainz-search"
            return metadata
        time.sleep(1)
    return None

def cddb_sum(value):
    return sum(int(ch) for ch in str(value))


def cddb_disc_id(toc):
    tracks = toc["tracks"]
    leadout = toc["leadout"]
    total_seconds = (leadout - tracks[0]) // 75
    checksum = sum(cddb_sum(offset // 75) for offset in tracks)
    disc_id = ((checksum % 255) << 24) | (total_seconds << 8) | len(tracks)
    return f"{disc_id:08x}", total_seconds


def cddb_get(command):
    response = requests.get(
        "http://gnudb.gnudb.org/~cddb/cddb.cgi",
        params={
            "cmd": command.replace("+", " "),
            "hello": "phuju localhost dvdstation 1.0",
            "proto": "6",
        },
        headers={"User-Agent": USER_AGENT},
        timeout=15,
    )
    response.raise_for_status()
    return response.text


def parse_cddb_kv(text):
    data = {}
    for line in text.splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key] = data.get(key, "") + value
    return data


def gnudb_lookup(device, track_count):
    toc = audio_cd_toc(device)
    if toc.get("freedb_id"):
        disc_id = toc["freedb_id"]
        total_seconds = (toc["leadout"] - toc["tracks"][0]) // 75
    else:
        disc_id, total_seconds = cddb_disc_id(toc)
    offsets = " ".join(str(offset) for offset in toc["tracks"])
    query = f"cddb query {disc_id} {track_count} {offsets} {total_seconds}"
    response = cddb_get(query)
    lines = [line.strip() for line in response.splitlines() if line.strip()]
    if not lines:
        return None

    first = lines[0].split(" ", 3)
    if first[0] not in ("200", "210", "211"):
        return None

    if first[0] == "200":
        category = first[1]
        read_id = first[2]
    else:
        parts = lines[1].split(" ", 2)
        if len(parts) < 2:
            return None
        category = parts[0]
        read_id = parts[1]

    read_response = cddb_get(f"cddb read {category} {read_id}")
    kv = parse_cddb_kv(read_response)
    dtitle = kv.get("DTITLE", "Unknown Artist / Unknown Album")

    if " / " in dtitle:
        album_artist, album = dtitle.split(" / ", 1)
    else:
        album_artist, album = "Unknown Artist", dtitle

    metadata = {
        "source": "gnudb",
        "release_id": None,
        "release_group_id": None,
        "album": album.strip() or "Unknown Album",
        "album_artist": album_artist.strip() or "Unknown Artist",
        "date": kv.get("DYEAR", ""),
        "year": kv.get("DYEAR", ""),
        "genre": kv.get("DGENRE", ""),
        "tracks": [],
        "toc": toc,
    }

    for index in range(track_count):
        title = kv.get(f"TTITLE{index}", f"Track {index + 1:02d}").strip()
        artist = metadata["album_artist"]
        if " / " in title:
            artist, title = title.split(" / ", 1)
        metadata["tracks"].append({
            "number": index + 1,
            "title": title or f"Track {index + 1:02d}",
            "artist": artist or metadata["album_artist"],
            "recording_id": None,
            "release_track_id": None,
        })

    return metadata


def musicbrainz_release_id_search(album_artist, album):
    if not album or album == "Unknown Album":
        return None

    if _mb is not None:
        fields = {"release": album}
        if album_artist and album_artist != "Unknown Artist":
            fields["artist"] = album_artist
        try:
            hits = _mb.search_releases(limit=1, **fields).get("release-list", [])
        except _mb.WebServiceError:
            hits = []
        return hits[0].get("id") if hits else None

    query_parts = [f'release:"{album}"']
    if album_artist and album_artist != "Unknown Artist":
        query_parts.append(f'artist:"{album_artist}"')
    response = requests.get(
        "https://musicbrainz.org/ws/2/release/",
        params={"query": " AND ".join(query_parts), "fmt": "json", "limit": 1},
        headers={"User-Agent": USER_AGENT},
        timeout=20,
    )
    response.raise_for_status()
    releases = response.json().get("releases", [])
    return releases[0].get("id") if releases else None


def audio_metadata_lookup(device, track_count, artist_hint=None, album_hint=None):
    metadata = None

    for attempt in range(2):
        try:
            metadata = musicbrainz_lookup(device, track_count)
            if metadata:
                break
        except Exception as e:
            if attempt == 0:
                print(f"MusicBrainz lookup failed, retrying... ({e})")
                time.sleep(1)

    if not metadata and album_hint:
        for attempt in range(2):
            try:
                metadata = musicbrainz_lookup_by_album_hints(artist_hint, album_hint, track_count)
                if metadata:
                    break
            except Exception as e:
                if attempt == 0:
                    print(f"MusicBrainz album search failed, retrying... ({e})")
                    time.sleep(1)

    if metadata and not metadata.get("release_id"):
        try:
            metadata["release_id"] = musicbrainz_release_id_search(
                metadata.get("album_artist", ""),
                metadata.get("album", ""),
            )
        except Exception as e:
            print(f"MusicBrainz release search failed: {e}")

    if metadata and metadata.get("release_id") and not metadata.get("release_group_id"):
        try:
            details = musicbrainz_release_details(metadata["release_id"])
            metadata["release_group_id"] = (details.get("release-group") or {}).get("id")
        except Exception as e:
            print(f"MusicBrainz release-group lookup failed: {e}")

    if not metadata:
        try:
            metadata = gnudb_lookup(device, track_count)
        except Exception as e:
            print(f"GnuDB lookup failed: {e}")

    return metadata


def write_album_info(out_dir, metadata):
    path = out_dir / "album_info.json"
    with path.open("w") as f:
        json.dump(metadata or {"metadata_found": False}, f, indent=2, ensure_ascii=False)
    return path


def _sniff_image_ext(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    return "jpg"


def download_cover_art(release_id, out_dir, release_group_id=None):
    if not release_id and not release_group_id:
        return None

    if _mb is not None:
        for fetch in (
            (lambda: _mb.get_image_front(release_id, size=500)) if release_id else None,
            (lambda: _mb.get_release_group_image_front(release_group_id, size=500)) if release_group_id else None,
        ):
            if fetch is None:
                continue
            try:
                data = fetch()
            except Exception:
                continue
            if data:
                path = out_dir / f"cover.{_sniff_image_ext(data)}"
                path.write_bytes(data)
                return path

    headers = {"User-Agent": USER_AGENT}
    candidates = []
    if release_id:
        candidates.extend(f"https://coverartarchive.org/release/{release_id}/{suffix}" for suffix in ("front-500", "front"))
    if release_group_id:
        candidates.extend(f"https://coverartarchive.org/release-group/{release_group_id}/{suffix}" for suffix in ("front-500", "front"))

    for url in candidates:
        try:
            response = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
        except requests.RequestException:
            continue

        if response.status_code == 200 and response.content:
            content_type = response.headers.get("Content-Type", "image/jpeg").split(";", 1)[0]
            ext = "png" if "png" in content_type else "jpg"
            path = out_dir / f"cover.{ext}"
            path.write_bytes(response.content)
            return path

    return None


def tag_flac(path, track_meta, album_meta, cover_path):
    audio = FLAC(path)
    total = len(album_meta.get("tracks", []))
    number = track_meta["number"]

    audio["TITLE"] = track_meta["title"]
    audio["ARTIST"] = track_meta["artist"]
    audio["ALBUM"] = album_meta["album"]
    audio["ALBUMARTIST"] = album_meta["album_artist"]
    audio["TRACKNUMBER"] = str(number)
    audio["TRACKTOTAL"] = str(total)

    if album_meta.get("date"):
        audio["DATE"] = album_meta["date"]
    if album_meta.get("release_id"):
        audio["MUSICBRAINZ_ALBUMID"] = album_meta["release_id"]
    if track_meta.get("recording_id"):
        audio["MUSICBRAINZ_TRACKID"] = track_meta["recording_id"]
    if track_meta.get("release_track_id"):
        audio["MUSICBRAINZ_RELEASETRACKID"] = track_meta["release_track_id"]

    if cover_path and cover_path.exists():
        picture = Picture()
        picture.type = 3
        picture.mime = "image/png" if cover_path.suffix.lower() == ".png" else "image/jpeg"
        picture.desc = "Cover"
        picture.data = cover_path.read_bytes()
        audio.clear_pictures()
        audio.add_picture(picture)

    audio.save()


def retag_audio_rip(rip_dir, artist_hint, album_hint):
    rip_dir = Path(rip_dir)
    flacs = sorted(rip_dir.glob("*.flac"))
    if not flacs:
        raise RuntimeError(f"No FLAC files found in {rip_dir}")
    if not album_hint:
        raise RuntimeError("Retag needs --album")

    metadata = musicbrainz_lookup_by_album_hints(artist_hint, album_hint, len(flacs))
    if not metadata:
        raise RuntimeError("Could not find album metadata")

    target_dir = unique_dir(RIP_ROOT / safe_path_name(f"{metadata['album_artist']} - {metadata['album']}"))
    if rip_dir != target_dir:
        rip_dir.rename(target_dir)
    else:
        target_dir = rip_dir

    write_album_info(target_dir, metadata)
    cover_path = download_cover_art(
        metadata.get("release_id"),
        target_dir,
        metadata.get("release_group_id"),
    )

    renamed = []
    for index, src in enumerate(sorted(target_dir.glob("*.flac")), start=1):
        if index > len(metadata["tracks"]):
            break

        track_meta = metadata["tracks"][index - 1]
        out_file = target_dir / f"{index:02d} - {safe_path_name(track_meta['title'])}.flac"
        if src != out_file:
            if out_file.exists():
                out_file.unlink()
            src.rename(out_file)

        tag_flac(out_file, track_meta, metadata, cover_path)
        renamed.append(out_file)

    chown_to_sudo_user(target_dir)
    return target_dir, cover_path, renamed


def latest_audio_rip_dir():
    candidates = sorted(
        path for path in RIP_ROOT.glob("audio_cd_*")
        if path.is_dir() and list(path.glob("*.flac"))
    )
    if not candidates:
        raise RuntimeError("No audio_cd_* rip folders found")
    return candidates[-1]


def parse_ffmpeg_time(line):
    marker = "time="
    if marker not in line:
        return None
    value = line.split(marker, 1)[1].split()[0]
    try:
        hours, minutes, seconds = value.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except ValueError:
        return None


from discstation_burn import CancelError


def _check_cancel(ser):
    try:
        line = read_serial_line(ser, timeout=0)
        return line in ("CANCEL", "PLAY_STOP") if line else False
    except OSError:
        return False


def iter_process_events(proc, idle_seconds=1.0, ser=None):
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
        if ser is not None:
            if _check_cancel(ser):
                discstation_burn.stop_process(proc)
                raise CancelError
            if time.time() - last_ping >= 5:
                discstation_burn.send(ser, "PING")
                last_ping = time.time()
        try:
            line = lines.get(timeout=idle_seconds)
        except Empty:
            yield None
            continue
        if line is finished:
            output_done = True
        else:
            yield line
    reader.join(timeout=1)


def rip_device(device):
    """The node a ripper should read. macOS libdvdread/HandBrake/cd-paranoia
    want the raw char node (/dev/rdiskN); Linux and others use `device` as-is."""
    if device and discstation_host.system_name() == "darwin":
        name = Path(device).name
        if name.startswith("disk"):
            return f"/dev/r{name}"
    return device


def device_size_bytes(device):
    if discstation_host.system_name() != "linux":
        return discstation_host.media_capacity_bytes(device) or 0
    result = run_probe(["blockdev", "--getsize64", device], timeout=3)
    try:
        return int(ensure_text(result.stdout).strip() or "0")
    except ValueError:
        return 0


def directory_size_bytes(path):
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            pass
    return total


def _stdin_is_tty():
    """sys.stdin is None under pythonw.exe (no console) - plain .isatty() would
    AttributeError. Also guards a closed/redirected stdin under systemd/launchd."""
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError, OSError):
        return False


def burn_flow(ser, url):
    if not url:
        if _stdin_is_tty():
            safe_send(ser, "STATUS:Enter URL or file path in terminal")
            print("=== Enter URL or file path below, then press Enter ===")
            try:
                url = sys.stdin.readline().strip()
            except (EOFError, KeyboardInterrupt, OSError):
                safe_send(ser, "CANCELLED:Cancelled")
                return
        else:
            url = wait_for_web_url(ser)
            if url is None:
                safe_send(ser, "CANCELLED:Cancelled")
                return
            if not url:
                safe_send(ser, "ERROR:Need URL or file path")
                return

    device = discstation_burn.disc_device()
    disc_bytes = discstation_burn.disc_capacity_bytes(device)
    if disc_bytes:
        print(f"Disc capacity: {disc_bytes / 1_000_000_000:.2f}GB")
    else:
        label_hint = "DVD5"
        if is_blank_disc(device):
            label_hint = "DVD5 (set DISC_DISC_BYTES=8500000000 for DL)"
        print(f"Disc capacity: unknown (assuming {label_hint})")

    discstation_burn.WORK.mkdir(parents=True, exist_ok=True)
    job_dir = discstation_burn.WORK / time.strftime("job_%Y%m%d_%H%M%S")
    job_dir.mkdir()

    send(ser, "STATUS:Preflight...")
    info = discstation_burn.get_video_info(url, ser)
    title = info["title"]
    duration = info["duration"]
    duration_line, fit_line, can_fit = discstation_burn.preflight_lines(duration, disc_bytes)
    disc_label = discstation_burn.sanitize_disc_label(title)

    print(f"Title: {title}")
    print(f"Duration: {discstation_burn.format_duration(duration)}")
    print(f"Preflight: {fit_line}")
    print(f"Disc label: {disc_label}")
    print(f"DVD drive: {device}")

    send(ser, f"TITLE:{title}")
    send(ser, f"META:{duration_line}")
    send(ser, f"FIT:{fit_line}")

    label_hint = "disc"
    if disc_bytes:
        if disc_bytes < 1_500_000_000:
            label_hint = "CD"
        elif disc_bytes > 6_000_000_000:
            label_hint = "DVD9"
        else:
            label_hint = "DVD5"
    if not can_fit:
        raise RuntimeError(f"Video too long for {label_hint}")

    dl_info = discstation_burn.detect_disc_type(device)
    if dl_info["is_dual_layer"]:
        sl_target = int(os.environ.get("DISC_TARGET_BYTES", "4300000000"))
        try:
            sl_plan = discstation_burn.bitrate_plan(duration, "AUTO", sl_target)
            if sl_plan:
                warn = f"DL disc for {label_hint} content"
                print(f"WARNING: {warn}")
                safe_send(ser, f"WARNING:{warn}")
                time.sleep(3)
                safe_send(ser, f"TITLE:{title}")
                safe_send(ser, f"META:{duration_line}")
                safe_send(ser, f"FIT:{fit_line}")
        except RuntimeError:
            pass

    selected_mode = "AUTO"
    burn_speed = None
    print("Waiting for burn START button...")
    while True:
        line = wait_for_button(ser)
        if line == "CANCEL" or line == "PLAY_STOP":
            safe_send(ser, "CANCELLED:Cancelled")
            print("Burn cancelled by user")
            return
        if line.startswith("MODE:"):
            selected_mode = discstation_burn.normalize_mode(line.split(":", 1)[1])
            print(f"Burn mode: {selected_mode}")
        elif line.startswith("SPEED:"):
            burn_speed = line.split(":", 1)[1].strip()
            print(f"Burn speed: {burn_speed}")
        elif line == "START" or line.startswith("START:"):
            if ":" in line:
                selected_mode = discstation_burn.normalize_mode(line.split(":", 1)[1])
            print(f"Starting burn flow in {selected_mode} mode")
            send(ser, f"STATUS:Starting {selected_mode}...")
            break

    start_time = time.time()
    disc_type_label = "DL" if dl_info["is_dual_layer"] else "SL"
    try:
        plan = discstation_burn.bitrate_plan(duration, selected_mode, disc_bytes)
        video = discstation_burn.download(ser, url, job_dir)
        mpg, dvd_aspect = discstation_burn.convert(ser, video, job_dir, selected_mode, disc_bytes)
        discstation_burn.check_encoded_size(ser, mpg, disc_bytes)
        srt_files = discstation_burn.find_subtitle_files(video)
        if not srt_files:
            srt_files = discstation_burn.extract_embedded_subtitles(video, job_dir)
        if srt_files:
            safe_send(ser, f"INFO:{len(srt_files)} subtitle(s)")
            mpg = discstation_burn.add_subtitles(ser, mpg, srt_files, job_dir)
        dvd_dir = discstation_burn.remux_and_author(
            ser, mpg, disc_label, disc_bytes, dvd_aspect
        )

        if plan["burn"]:
            discstation_burn.wait_for_burn_confirm(ser, dvd_dir, disc_bytes)
            discstation_burn.burn(ser, dvd_dir, disc_label, burn_speed, dl_info["is_dual_layer"])
            safe_send(ser, "DONE:Disc complete!")
            print("Burn complete.")
        else:
            safe_send(ser, "DONE:Test complete!")
            print(f"Test complete. DVD folder: {dvd_dir}")

        append_burn_history({
            "timestamp": datetime.datetime.now().isoformat(),
            "title": title,
            "disc_type": disc_type_label,
            "mode": selected_mode,
            "speed": burn_speed or "Auto",
            "success": True,
            "duration_s": round(time.time() - start_time),
        })
    except (KeyboardInterrupt, SystemExit):
        append_burn_history({
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
        append_burn_history({
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

    time.sleep(3)


def _mpg_label(job_dir):
    label = "DVD_VIDEO"
    dl = job_dir / "download"
    if dl.is_dir():
        for f in sorted(dl.iterdir()):
            if f.suffix.lower() in discstation_burn.VIDEO_EXTS:
                label = discstation_burn.sanitize_disc_label(f.stem)
                break
        else:
            for f in sorted(dl.iterdir()):
                label = discstation_burn.sanitize_disc_label(f.stem)
                break
    if label == "DVD_VIDEO" or not label:
        label = discstation_burn.sanitize_disc_label(job_dir.name)
    return label


def burn_mpg_flow(ser):
    jobs = sorted(discstation_burn.WORK.glob("job_*"), reverse=True)
    candidates = []
    for jd in jobs:
        mpg = jd / "movie.mpg"
        if mpg.exists():
            label = _mpg_label(jd)
            candidates.append((mpg, label))
    names = [c[1][:20] for c in candidates] + ["Enter path..."]
    safe_send(ser, f"MENU_ITEMS:{','.join(names)}")
    safe_send(ser, "HOME:Select MPG to burn")
    mpg = None
    disc_label = None
    while True:
        line = read_serial_line(ser, timeout=0.5)
        if not line:
            continue
        if line.startswith("SELECT:"):
            sel = line.split(":", 1)[1].strip()
            if sel == "Enter path...":
                path_str = wait_for_web_url(ser)
                if path_str is None:
                    refresh_main_menu(ser)
                    return
                path_str = path_str.strip()
                p = Path(path_str)
                if not p.exists():
                    safe_send(ser, "ERROR:Path not found")
                    time.sleep(2)
                    continue
                if p.is_dir():
                    mpg = p / "movie.mpg"
                    if not mpg.exists():
                        safe_send(ser, "ERROR:No movie.mpg in dir")
                        time.sleep(2)
                        continue
                elif p.suffix.lower() == ".mpg":
                    mpg = p
                else:
                    safe_send(ser, "ERROR:Not an .mpg file")
                    time.sleep(2)
                    continue
                disc_label = discstation_burn.sanitize_disc_label(mpg.stem)
                break
            else:
                idx = next((i for i, n in enumerate(candidates) if n[1][:20] == sel), None)
                if idx is not None:
                    mpg, disc_label = candidates[idx]
                    break
        elif line == "CANCEL":
            safe_send(ser, "CANCELLED:Cancelled")
            refresh_main_menu(ser)
            return
        time.sleep(0.05)

    device = discstation_burn.disc_device()
    dl_info = discstation_burn.detect_disc_type(device)
    disc_bytes = dl_info["capacity"]
    discstation_burn.remux_and_burn(ser, mpg, disc_label, disc_bytes, dl_info)


def _copy_to_job(ser, src, dst_dir):
    if src.is_dir():
        items = sorted(src.iterdir())
        n = len(items)
        for i, item in enumerate(items):
            if item.is_file():
                discstation_burn.copy_with_keepalive(ser, item, dst_dir / item.name,
                                              base_pct=int(i * 100 / n), pct_span=100 / n)
    else:
        discstation_burn.copy_with_keepalive(ser, src, dst_dir / src.name)


def burn_data_flow(ser):
    global _last_upload_dir, _last_upload_label

    if _last_upload_dir and Path(_last_upload_dir).exists():
        url = _last_upload_dir
        _last_upload_dir = None
    elif _stdin_is_tty():
        safe_send(ser, "STATUS:Enter URL or file path in terminal")
        print("=== Enter URL or file path below, then press Enter ===")
        try:
            url = sys.stdin.readline().strip()
        except (EOFError, KeyboardInterrupt, OSError):
            safe_send(ser, "CANCELLED:Cancelled")
            return
        if not url:
            safe_send(ser, "ERROR:Need URL or file path")
            return
    else:
        url = wait_for_web_url(ser)
        if url is None:
            safe_send(ser, "CANCELLED:Cancelled")
            return
        if not url:
            safe_send(ser, "ERROR:Need URL or file path")
            return

    device = discstation_burn.disc_device()
    dl_info = discstation_burn.detect_disc_type(device)
    disc_bytes = dl_info["capacity"]
    if disc_bytes:
        print(f"Disc capacity: {disc_bytes / 1_000_000_000:.2f}GB")
    else:
        print("Disc capacity: unknown (assuming DVD5)")

    local_path = Path(url)
    if local_path.exists():
        title = local_path.name if local_path.is_dir() else local_path.stem
    else:
        send(ser, "STATUS:Probing source...")
        info = discstation_burn.get_video_info(url, ser)
        title = info["title"]

    if _last_upload_label:
        disc_label = discstation_burn.sanitize_disc_label(_last_upload_label)
        _last_upload_label = None
    else:
        disc_label = discstation_burn.sanitize_disc_label(title)
    print(f"Source: {url}")
    print(f"Disc label: {disc_label}")
    print(f"DVD drive: {device}")

    send(ser, f"TITLE:{title}")

    burn_speed = None
    print("Waiting for burn START button...")
    while True:
        line = wait_for_button(ser)
        if line == "CANCEL" or line == "PLAY_STOP":
            safe_send(ser, "CANCELLED:Cancelled")
            print("Burn cancelled by user")
            return
        if line.startswith("SPEED:"):
            burn_speed = line.split(":", 1)[1].strip()
            print(f"Burn speed: {burn_speed}")
        elif line == "START" or line.startswith("START:"):
            print("Starting data burn...")
            send(ser, "STATUS:Starting data burn...")
            break

    if not can_burn_disc(device):
        raise RuntimeError("No writable disc in drive")

    discstation_burn.WORK.mkdir(parents=True, exist_ok=True)
    job_dir = discstation_burn.WORK / time.strftime("job_%Y%m%d_%H%M%S")
    job_dir.mkdir()
    download_dir = job_dir / "download"
    download_dir.mkdir()

    start_time = time.time()
    try:
        is_dir = local_path.is_dir() if local_path.exists() else False
        if is_dir:
            files_to_burn = [local_path]
        elif local_path.exists():
            safe_send(ser, "STATUS:Copying files...")
            _copy_to_job(ser, local_path, download_dir)
            files_to_burn = sorted(download_dir.iterdir())
        else:
            discstation_burn.download(ser, url, job_dir)
            files_to_burn = sorted(download_dir.iterdir())

        if not files_to_burn:
            raise RuntimeError("No files to burn")

        # A single .iso (loose, a local path, or an upload folder holding just
        # the .iso) is written verbatim as a bootable image, not repackaged.
        if len(files_to_burn) == 1 and files_to_burn[0].is_dir():
            inner = [p for p in files_to_burn[0].iterdir() if p.is_file()]
            if len(inner) == 1 and inner[0].suffix.lower() == ".iso":
                files_to_burn = inner

        total_bytes = sum(f.stat().st_size for f in files_to_burn if f.is_file())
        for d in files_to_burn:
            if d.is_dir():
                total_bytes += sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        label = "DVD5" if not dl_info["is_dual_layer"] else "DVD9"
        usable = discstation_burn.disc_output_limit_bytes(disc_bytes)
        if usable and total_bytes > usable:
            size_gb = total_bytes / 1e9
            cap_gb = usable / 1e9
            raise RuntimeError(
                f"Data too large for {label}: {size_gb:.1f}GB > {cap_gb:.1f}GB disc")

        is_iso = len(files_to_burn) == 1 and files_to_burn[0].suffix.lower() == '.iso'
        if is_iso:
            discstation_burn.burn_iso(ser, files_to_burn[0], burn_speed, dl_info["is_dual_layer"])
        else:
            discstation_burn.burn_data(ser, files_to_burn, disc_label, burn_speed, dl_info["is_dual_layer"])
        safe_send(ser, "DONE:Data disc complete!")
        print("Data burn complete.")

        append_burn_history({
            "timestamp": datetime.datetime.now().isoformat(),
            "title": title,
            "disc_type": "Data DVD",
            "mode": "DATA",
            "speed": burn_speed or "Auto",
            "success": True,
            "duration_s": round(time.time() - start_time),
        })
    except (KeyboardInterrupt, SystemExit):
        append_burn_history({
            "timestamp": datetime.datetime.now().isoformat(),
            "title": title,
            "disc_type": "Data DVD",
            "mode": "DATA",
            "speed": burn_speed or "Auto",
            "success": False,
            "error": "Cancelled",
            "duration_s": round(time.time() - start_time),
        })
        raise
    except CancelError:
        append_burn_history({
            "timestamp": datetime.datetime.now().isoformat(),
            "title": title,
            "disc_type": "Data DVD",
            "mode": "DATA",
            "speed": burn_speed or "Auto",
            "success": False,
            "error": "Cancelled",
            "duration_s": round(time.time() - start_time),
        })
        return
    except Exception as e:
        append_burn_history({
            "timestamp": datetime.datetime.now().isoformat(),
            "title": title,
            "disc_type": "Data DVD",
            "mode": "DATA",
            "speed": burn_speed or "Auto",
            "success": False,
            "error": str(e)[:100],
            "duration_s": round(time.time() - start_time),
        })
        raise

    time.sleep(3)


def burn_audio_flow(ser):
    if _stdin_is_tty():
        safe_send(ser, "STATUS:Enter path to audio files in terminal")
        print("=== Enter path to audio files/folder, then press Enter ===")
        try:
            url = sys.stdin.readline().strip()
        except (EOFError, KeyboardInterrupt, OSError):
            safe_send(ser, "CANCELLED:Cancelled")
            return
        if not url:
            safe_send(ser, "ERROR:Need path to audio files")
            return
    else:
        url = wait_for_web_url(ser)
        if url is None:
            safe_send(ser, "CANCELLED:Cancelled")
            return
        if not url:
            safe_send(ser, "ERROR:Need path to audio files")
            return

    src_path = Path(url)
    if not src_path.exists():
        safe_send(ser, "ERROR:Path not found")
        return

    audio_files = []
    audio_exts = {".wav", ".flac", ".mp3", ".aac", ".ogg", ".wma", ".m4a", ".opus"}
    if src_path.is_dir():
        for f in sorted(src_path.iterdir()):
            if f.suffix.lower() in audio_exts:
                audio_files.append(f)
    elif src_path.is_file():
        audio_files = [src_path]

    if not audio_files:
        safe_send(ser, "ERROR:No audio files found")
        return

    album_title = ""
    album_artist = ""
    track_titles = []
    total_dur = 0
    for f in audio_files:
        track_title = f.stem
        try:
            if f.suffix.lower() == ".flac":
                from mutagen.flac import FLAC
                a = FLAC(str(f))
                total_dur += a.info.length
                track_title = a.get("title", [f.stem])[0]
                if not album_title:
                    album_title = a.get("album", [""])[0]
                    album_artist = a.get("albumartist", [a.get("artist", [""])[0]])[0]
            elif f.suffix.lower() == ".mp3":
                from mutagen.mp3 import MP3
                a = MP3(str(f))
                total_dur += a.info.length
                track_title = str(a.get("TIT2", f.stem))
                if not album_title:
                    album_title = str(a.get("TALB", ""))
                    album_artist = str(a.get("TPE2", str(a.get("TPE1", ""))))
            else:
                total_dur += discstation_burn.probe_duration(str(f))
        except Exception:
            pass
        track_titles.append(track_title)
    fingerprint = f"{len(audio_files)}-{int(total_dur)}"

    source_label = src_path.name if src_path.is_dir() else src_path.stem
    disc_label = discstation_burn.audio_disc_title(source_label)
    mins = int(total_dur / 60)
    secs = int(total_dur % 60)
    fits = "OK" if total_dur <= 4740 else "TOO LONG"  # 79 min max for 700MB CD-R
    send(ser, f"TITLE:{disc_label}")
    send(ser, f"META:Dur {mins}m{secs}s")
    send(ser, f"FIT:CD-R {fits}")

    burn_speed = None
    print("Waiting for START button...")
    while True:
        line = wait_for_button(ser)
        if line == "CANCEL" or line == "PLAY_STOP":
            safe_send(ser, "CANCELLED:Cancelled")
            return
        if line.startswith("SPEED:"):
            burn_speed = line.split(":", 1)[1].strip()
        elif line == "START" or line.startswith("START:"):
            send(ser, "STATUS:Starting audio burn...")
            break

    device = discstation_burn.disc_device()
    if not can_burn_disc(device):
        raise RuntimeError("No writable disc in drive")
    if total_dur > 4740:
        raise RuntimeError(f"Too long for CD-R: {int(total_dur/60)}m{int(total_dur%60)}s > 79m")

    start_time = time.time()
    try:
        discstation_burn.burn_audio_cd(ser, audio_files, disc_label, burn_speed)
        safe_send(ser, "DONE:Audio CD complete!")
        append_burn_history({
            "timestamp": datetime.datetime.now().isoformat(),
            "title": disc_label,
            "fingerprint": fingerprint,
            "track_titles": track_titles,
            "disc_type": "Audio CD",
            "mode": "AUDIO",
            "speed": burn_speed or "Auto",
            "success": True,
            "duration_s": round(time.time() - start_time),
        })
    except (KeyboardInterrupt, SystemExit):
        raise
    except CancelError:
        append_burn_history({
            "timestamp": datetime.datetime.now().isoformat(),
            "title": disc_label,
            "fingerprint": fingerprint,
            "track_titles": track_titles,
            "disc_type": "Audio CD",
            "mode": "AUDIO",
            "speed": burn_speed or "Auto",
            "success": False,
            "error": "Cancelled",
            "duration_s": round(time.time() - start_time),
        })
        return
    except Exception as e:
        append_burn_history({
            "timestamp": datetime.datetime.now().isoformat(),
            "title": disc_label,
            "fingerprint": fingerprint,
            "track_titles": track_titles,
            "disc_type": "Audio CD",
            "mode": "AUDIO",
            "speed": burn_speed or "Auto",
            "success": False,
            "error": str(e)[:100],
            "duration_s": round(time.time() - start_time),
        })
        raise

    time.sleep(3)


def _iter_proc_lines(proc, ser):
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
            discstation_burn.send(ser, "PING")
            last_ping = time.time()
        if _check_cancel(ser):
            discstation_burn.stop_process(proc)
            return
        try:
            line = lines.get(timeout=0.5)
        except Empty:
            continue
        if line is finished:
            output_done = True
        else:
            yield line
    reader.join(timeout=1)


def _run_mpv(ser, cmd, label, kind=None, track_titles=None, track_starts=None):
    try:
        os.unlink(MPV_SOCKET)
    except OSError:
        pass

    env = os.environ.copy()
    if discstation_host.system_name() == "linux":
        if "DISPLAY" not in env:
            env["DISPLAY"] = ":0"
        try:
            uid = os.getuid()
            home_xauth = Path.home() / ".Xauthority"
            env.setdefault("XAUTHORITY", str(home_xauth) if home_xauth.exists() else f"/run/user/{uid}/.Xauthority")
            env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
        except Exception:
            pass

    proc = subprocess.Popen(run_as_desktop_user(cmd), env=env)

    try:
        if not wait_for_socket(MPV_SOCKET, proc):
            discstation_burn.stop_process(proc)
            raise RuntimeError("Could not start mpv")

        time.sleep(1)
        if proc.poll() is not None:
            raise RuntimeError("mpv could not open disc")

        paused = False
        current_volume = None
        current_track = None
        last_track_poll = 0
        track_titles = track_titles or []
        track_starts = track_starts or []
        send(ser, "PLAY_MODE:AUDIO_CD" if kind == "audio_cd" else "PLAY_MODE:DEFAULT")
        send(ser, "PLAY:PLAYING")
        print(f"{label}. Short press toggles pause; long press stops.")

        last_ping = time.time()
        while proc.poll() is None:
            if time.time() - last_ping >= 5:
                last_ping = time.time()
                safe_send(ser, "PING")

            if kind == "audio_cd" and time.time() - last_track_poll >= 1:
                last_track_poll = time.time()
                track = mpv_query(["get_property", "chapter"])
                if not isinstance(track, (int, float)) and track_starts:
                    position = mpv_query(["get_property", "time-pos"])
                    if isinstance(position, (int, float)):
                        track = max((i for i, start in enumerate(track_starts) if start <= position), default=0)
                if isinstance(track, (int, float)):
                    track = int(track)
                    if track != current_track:
                        current_track = track
                        title = track_titles[track] if 0 <= track < len(track_titles) else ""
                        status = f"TRACK {track + 1:02d}"
                        if title:
                            status += f" // {title}"
                        send(ser, f"PLAY_STATUS:{status}")

            if ser.in_waiting:
                line = ser.readline().decode(errors="ignore").strip()
                discstation_burn.note_serial_activity()

                if line == "PLAY_BUTTON":
                    paused = not paused
                    mpv_command(["set_property", "pause", paused])
                    mpv_command(["set_property", "speed", 1.0])
                    send(ser, "PLAY_STATUS:PAUSED" if paused else "PLAY_STATUS:PLAYING")

                elif line == "PLAY_STOP":
                    send(ser, "STATUS:Stopping play")
                    discstation_burn.stop_process(proc)
                    break

                elif line == "FF:BIG":
                    if kind == "audio_cd":
                        mpv_command(["add", "chapter", 1])
                        send(ser, "PLAY_STATUS:Next track")
                    else:
                        mpv_command(["seek", 120])
                        mpv_command(["set_property", "pause", False])
                        paused = False
                        send(ser, "PLAY_STATUS:FF 120s")

                elif line.startswith("FF:"):
                    try:
                        seek_sec = int(line.split(":", 1)[1])
                    except ValueError:
                        continue
                    if kind == "audio_cd":
                        mpv_command(["add", "chapter", 1])
                        send(ser, "PLAY_STATUS:Next track")
                    else:
                        mpv_command(["seek", seek_sec])
                        mpv_command(["set_property", "pause", False])
                        paused = False
                        send(ser, f"PLAY_STATUS:FF {seek_sec}s")

                elif line == "REW:BIG":
                    if kind == "audio_cd":
                        mpv_command(["add", "chapter", -1])
                        send(ser, "PLAY_STATUS:Prev track")
                    else:
                        mpv_command(["seek", -120])
                        mpv_command(["set_property", "pause", False])
                        paused = False
                        send(ser, "PLAY_STATUS:REW 120s")

                elif line.startswith("REW:"):
                    try:
                        seek_sec = int(line.split(":", 1)[1])
                    except ValueError:
                        continue
                    if kind == "audio_cd":
                        mpv_command(["add", "chapter", -1])
                        send(ser, "PLAY_STATUS:Prev track")
                    else:
                        mpv_command(["seek", -seek_sec])
                        mpv_command(["set_property", "pause", False])
                        paused = False
                        send(ser, f"PLAY_STATUS:REW {seek_sec}s")

                elif line.startswith("POT:"):
                    try:
                        volume = int(line.split(":", 1)[1])
                    except ValueError:
                        continue
                    if current_volume is None or abs(volume - current_volume) >= 3:
                        current_volume = volume
                        try:
                            mpv_command(["set_property", "volume", volume])
                        except OSError:
                            break

            time.sleep(0.05)
    finally:
        if proc.poll() is None:
            discstation_burn.stop_process(proc)
        try:
            os.unlink(MPV_SOCKET)
        except OSError:
            pass


def play_flow(ser):
    device = discstation_burn.disc_device()
    kind = disc_kind(device)
    print(f"Disc type: {kind}")

    try:
        mpv = discstation_burn.tool("mpv")
    except FileNotFoundError:
        raise RuntimeError("mpv not found")

    def play_vob_fallback():
        # No DVD-menu engine available (libdvdnav missing, or on Windows
        # where the plain mpv build never has it) - play the main title's
        # VOBs directly off the mounted volume instead (no menus).
        with mounted_disc(device) as mount_dir:
            video_ts = mount_dir / "VIDEO_TS"
            files = sorted(
                path for path in video_ts.glob("VTS_01_*.VOB")
                if re.search(r"_\d+\.VOB$", path.name, re.IGNORECASE)
                and not path.name.upper().endswith("_0.VOB")
            )
            if not files:
                raise RuntimeError("No playable DVD title found")
            cmd = [
                mpv,
                "--input-ipc-server=" + MPV_SOCKET,
                "--force-window=yes",
                "--idle=no",
                *[str(path) for path in files],
            ]
            _run_mpv(ser, cmd, "Playing DVD", kind)

    if kind == "dvd_video":
        if discstation_host.system_name() == "darwin":
            try:
                cmd = [
                    mpv,
                    "--input-ipc-server=" + MPV_SOCKET,
                    "--force-window=yes",
                    "--idle=no",
                    "--dvd-device=" + rip_device(device),
                    "dvdnav://",
                ]
                _run_mpv(ser, cmd, "Playing DVD", kind)
            except RuntimeError:
                # libdvdnav couldn't open the disc.
                play_vob_fallback()
        else:
            # Windows' only reliably-fetchable mpv build (a plain .zip, no
            # 7z/rar tooling needed) has no libdvdnav - --dvd-device isn't
            # even a recognized option in it, so don't waste a doomed
            # attempt; go straight to the VOB fallback.
            play_vob_fallback()

    elif kind == "audio_cd":
        _, track_titles, track_starts = audio_track_metadata(device)
        audio_device = discstation_host.audio_output_device()
        cmd = [
            mpv,
            "--input-ipc-server=" + MPV_SOCKET,
            "--force-window=no",
            "--idle=no",
            "--cdrom-device=" + rip_device(device),
            "--cdda-cdtext=yes",
            "cdda://",
        ]
        if audio_device:
            cmd.insert(1, "--audio-device=" + audio_device)
            print(f"Audio CD output: {audio_device}")
        _run_mpv(ser, cmd, "Playing audio CD", kind, track_titles, track_starts)

    elif kind in ("vcd", "svcd", "video_data"):
        with mounted_disc(device) as mount_dir:
            _, files = disc_video_files(mount_dir)
            if not files:
                raise RuntimeError("No playable video files")
            cmd = [
                mpv,
                "--input-ipc-server=" + MPV_SOCKET,
                "--force-window=yes",
                "--idle=no",
                *[str(path) for path in files],
            ]
            _run_mpv(ser, cmd, f"Playing {kind.upper()}", kind)

    else:
        raise RuntimeError(f"Unsupported disc: {kind}")

    safe_send(ser, "DONE:Playback stopped")
    time.sleep(3)


def _finalize_video_rip(ser, out_dir, device, kind):
    """After a successful video rip: look the title up on TMDb, rename the
    output folder to "Title (Year)", and drop poster.jpg / movie.nfo. No-op if
    discstation_meta is unavailable or no key is configured. Returns the final
    directory."""
    out_dir = Path(out_dir)
    if discstation_meta is None or not discstation_meta.available():
        return out_dir
    try:
        guess = disc_title(device) or ""
    except Exception:
        guess = ""
    if not guess:
        return out_dir
    meta = discstation_meta.lookup(guess)
    if not meta:
        print(f"TMDb: no match for {guess!r}")
        return out_dir

    target = out_dir
    new_name = discstation_meta.folder_name(meta)
    if new_name and Path(new_name).name != out_dir.name:
        candidate = unique_dir(RIP_ROOT / new_name)
        try:
            out_dir.rename(candidate)
            target = candidate
        except OSError as e:
            print(f"TMDb: could not rename rip dir: {e}")

    discstation_meta.save_assets(target, meta)
    info_path = target / "disc_info.json"
    try:
        data = json.loads(info_path.read_text()) if info_path.exists() else {"kind": kind}
    except Exception:
        data = {"kind": kind}
    data["tmdb"] = meta
    try:
        info_path.write_text(json.dumps(data, indent=2))
    except OSError:
        pass
    safe_send(ser, f"INFO:{meta.get('title', '')} ({meta.get('year', '')})".strip())
    print(f"TMDb: {meta.get('title')} ({meta.get('year')}) -> {target}")
    return target


def _handbrake_json_blocks(text):
    """HandBrakeCLI --json prints one or more 'Marker: {json}' blocks.
    Return {marker: parsed_obj}."""
    blocks = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = re.match(r"^([A-Za-z][A-Za-z ]*): \{$", lines[i])
        if not m:
            i += 1
            continue
        buf = ["{"]
        i += 1
        while i < len(lines):
            buf.append(lines[i])
            if lines[i] == "}":
                break
            i += 1
        try:
            blocks[m.group(1)] = json.loads("\n".join(buf))
        except ValueError:
            pass
        i += 1
    return blocks


def handbrake_scan(device):
    """Return {'main_feature': int|None, 'titles': [{index,duration_s,chapters}]}
    or None. Uses HandBrakeCLI, which does real main-feature detection."""
    try:
        handbrake_cli = discstation_burn.tool("HandBrakeCLI")
    except FileNotFoundError:
        return None
    try:
        r = subprocess.run(
            [handbrake_cli, "--json", "--scan", "-i", device, "-t", "0"],
            capture_output=True, text=True, timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"HandBrake scan failed: {e}")
        return None
    ts = _handbrake_json_blocks(ensure_text(r.stdout) + ensure_text(r.stderr)).get("JSON Title Set")
    if not ts or not ts.get("TitleList"):
        return None
    titles = []
    for t in ts["TitleList"]:
        dur = t.get("Duration") or {}
        secs = dur.get("Hours", 0) * 3600 + dur.get("Minutes", 0) * 60 + dur.get("Seconds", 0)
        titles.append({
            "index": t.get("Index"),
            "duration_s": secs,
            "chapters": len(t.get("ChapterList") or []),
        })
    main = ts.get("MainFeature")
    if main is None and titles:
        main = max(titles, key=lambda x: x["duration_s"])["index"]
    return {"main_feature": main, "titles": titles}


def handbrake_rip_main_feature(ser, device, out_dir, title_index):
    """Transcode one DVD title to MKV (H.264) with HandBrakeCLI, streaming its
    JSON progress to the ESP32."""
    dest = out_dir / "main_feature.mkv"
    send(ser, "STATUS:Ripping main feature")
    send(ser, f"INFO:HandBrake title {title_index}")
    send(ser, "PROGRESS:0%")
    proc = subprocess.Popen(
        [discstation_burn.tool("HandBrakeCLI"), "--json", "-i", device, "-o", str(dest),
         "-t", str(title_index), "-e", "x264", "-q", "20",
         "--all-audio", "--all-subtitles"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    last_pct = -1
    try:
        for event in iter_process_events(proc, ser=ser):
            if event is None:
                continue
            m = re.search(r'"Progress":\s*([0-9.]+)', event)
            if m:
                pct = int(float(m.group(1)) * 100)
                if pct > last_pct:
                    last_pct = pct
                    send(ser, f"PROGRESS:{min(pct, 99)}%")
    except CancelError:
        discstation_burn.stop_process(proc)
        safe_send(ser, "CANCELLED:Rip cancelled")
        raise
    if proc.wait() != 0 or not dest.exists():
        raise RuntimeError("HandBrake rip failed")
    return dest


def rip_flow(ser, artist_hint=None, album_hint=None):
    device = discstation_burn.disc_device()
    kind = disc_kind(device)

    if kind == "audio_cd":
        rip_audio_cd(ser, device, artist_hint, album_hint)
        return

    if kind in ("vcd", "svcd", "video_data"):
        rip_video_disc(ser, device, kind)
        return

    if kind != "dvd_video":
        raise RuntimeError(f"Unsupported disc: {kind}")

    # libdvdread / HandBrake / dvdbackup want the raw node on macOS and the
    # auto-mounted UDF/ISO volume released first (no-ops on Linux).
    device = rip_device(device)
    discstation_host.unmount_device(device)

    out_dir = RIP_ROOT / time.strftime("rip_%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    if discstation_host.system_name() == "windows":
        # Neither dvdbackup nor HandBrakeCLI's libdvdread reliably reads this
        # drive on Windows (the latter hangs rather than erroring) - skip
        # both and just mirror the drive with robocopy, a plain recursive
        # file copy. Same real byte-count progress technique as the
        # dvdbackup path below (accurate, not an estimate - directory size
        # vs. known disc size).
        send(ser, "STATUS:Ripping disc...")
        send(ser, "INFO:Full VIDEO_TS copy")
        send(ser, "PROGRESS:0%")
        source = str(device).rstrip("\\/").rstrip(":") + ":\\"
        print(f"Ripping {source} to {out_dir}")
        proc = subprocess.Popen(
            ["robocopy", source, str(out_dir), "/E", "/R:1", "/W:1"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        disc_bytes = device_size_bytes(device)
        last_pct = -1
        try:
            for event in iter_process_events(proc, ser=ser):
                if event is None and disc_bytes > 0:
                    pct = min(int(directory_size_bytes(out_dir) / disc_bytes * 100), 99)
                    if pct > last_pct:
                        last_pct = pct
                        send(ser, f"PROGRESS:{pct}%")
        except CancelError:
            safe_send(ser, "CANCELLED:Rip cancelled")
            print("Rip cancelled by user")
            return
        except (KeyboardInterrupt, SystemExit):
            discstation_burn.stop_process(proc)
            safe_send(ser, "CANCELLED:Rip stopped")
            raise
        rc = proc.wait()
        if rc >= 8:  # robocopy: 0-7 are success variants, only 8+ is a real failure
            raise RuntimeError(f"robocopy failed (exit {rc})")
        safe_send(ser, "PROGRESS:100%")
        safe_send(ser, "DONE:Rip complete!")
        print(f"Rip complete: {out_dir}")
        out_dir = _finalize_video_rip(ser, out_dir, device, "dvd_video")
        chown_to_sudo_user(out_dir)
        time.sleep(3)
        return

    scan = handbrake_scan(device)
    if scan:
        main = scan["main_feature"]
        mins = next((t["duration_s"] // 60 for t in scan["titles"] if t["index"] == main), 0)
        send(ser, f"INFO:{len(scan['titles'])} titles, main #{main} ~{mins}m")
        print(f"HandBrake scan: {len(scan['titles'])} titles; main feature #{main} (~{mins}m)")

    # Opt-in: transcode just the main feature to MKV instead of a full mirror.
    if os.environ.get("DISCSTATION_DVD_RIP_MODE", "").lower() == "mkv" and scan and scan["main_feature"]:
        handbrake_rip_main_feature(ser, device, out_dir, scan["main_feature"])
        (out_dir / "disc_info.json").write_text(
            json.dumps({"kind": "dvd_video", "mode": "handbrake-main-feature",
                        "title": scan["main_feature"], "files": ["main_feature.mkv"]}, indent=2),
        )
        safe_send(ser, "PROGRESS:100%")
        safe_send(ser, "DONE:Rip complete!")
        print(f"Rip complete: {out_dir}")
        out_dir = _finalize_video_rip(ser, out_dir, device, "dvd_video")
        chown_to_sudo_user(out_dir)
        time.sleep(3)
        return

    if not shutil.which("dvdbackup"):
        raise RuntimeError("dvdbackup not found")

    send(ser, "STATUS:Ripping disc...")
    send(ser, "INFO:Full VIDEO_TS copy")
    send(ser, "PROGRESS:0%")
    print(f"Ripping {device} to {out_dir}")

    proc = subprocess.Popen(
        ["dvdbackup", "-M", "-p", "-i", device, "-o", str(out_dir)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    disc_bytes = device_size_bytes(device)
    last_pct = -1

    try:
        for event in iter_process_events(proc, ser=ser):
            if event is None:
                if disc_bytes > 0:
                    pct = min(int(directory_size_bytes(out_dir) / disc_bytes * 100), 99)
                    if pct > last_pct:
                        last_pct = pct
                        send(ser, f"PROGRESS:{pct}%")
                continue

            line = event.strip()
            print(line)
            match = re.search(r"(\d+(?:\.\d+)?)\s*%", line)
            if match:
                last_pct = int(float(match.group(1)))
                send(ser, f"PROGRESS:{match.group(1)}%")
            elif "Copying" in line:
                send(ser, "PROGRESS:" + line[:20])
    except CancelError:
        safe_send(ser, "CANCELLED:Rip cancelled")
        print("Rip cancelled by user")
        return
    except (KeyboardInterrupt, SystemExit):
        discstation_burn.stop_process(proc)
        safe_send(ser, "CANCELLED:Rip stopped")
        raise

    if proc.wait() != 0:
        if not disc_present(device):
            raise RuntimeError("Disc was removed during rip")
        raise RuntimeError("Rip failed")

    safe_send(ser, "PROGRESS:100%")
    safe_send(ser, "DONE:Rip complete!")
    print(f"Rip complete: {out_dir}")
    out_dir = _finalize_video_rip(ser, out_dir, device, "dvd_video")
    chown_to_sudo_user(out_dir)
    time.sleep(3)


def remux_or_copy_video(src, dest):
    result = subprocess.run(
        ["ffmpeg", "-y", "-nostdin", "-i", str(src), "-c", "copy", str(dest)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        shutil.copy2(src, dest.with_suffix(src.suffix.lower()))


def rip_video_disc(ser, device, kind):
    out_dir = RIP_ROOT / f"{kind}_{time.strftime('%Y%m%d_%H%M%S')}"
    out_dir.mkdir(parents=True, exist_ok=True)

    send(ser, f"STATUS:Ripping {kind.upper()}")
    print(f"Ripping {kind} from {device} to {out_dir}")

    with mounted_disc(device) as mount_dir:
        _, files = disc_video_files(mount_dir)
        if not files:
            raise RuntimeError("No video files found")

        for index, src in enumerate(files, start=1):
            send(ser, f"PROGRESS:File {index}/{len(files)}")
            dest = out_dir / f"{index:02d} - {safe_path_name(src.stem)}.mpg"
            print(f"Ripping {src.name} -> {dest.name}")
            remux_or_copy_video(src, dest)

    (out_dir / "disc_info.json").write_text(
        json.dumps({"kind": kind, "files": [path.name for path in files]}, indent=2),
    )
    safe_send(ser, "PROGRESS:100%")
    safe_send(ser, "DONE:Rip complete!")
    print(f"Video rip complete: {out_dir}")
    out_dir = _finalize_video_rip(ser, out_dir, device, kind)
    chown_to_sudo_user(out_dir)
    time.sleep(3)


def _rip_audio_cd_macos(ser, device, chapters, metadata, cover_path, out_dir):
    if not chapters:
        raise RuntimeError("No audio CD tracks found")
    wav_dir = out_dir / ".wav"
    wav_dir.mkdir(parents=True, exist_ok=True)
    paranoia = None
    for name in ("cd-paranoia", "cdparanoia"):
        try:
            paranoia = discstation_burn.tool(name)
            break
        except FileNotFoundError:
            continue
    if not paranoia:
        raise RuntimeError("cd-paranoia not installed (brew install libcdio-paranoia)")
    # -B batch mode writes track01.cdda.wav, track02.cdda.wav, ... in cwd.
    command = [paranoia, "-B", "-d", rip_device(device), "1-"]
    send(ser, "STATUS:RIPPING AUDIO CD")
    send(ser, "PROGRESS:0%")
    proc = subprocess.Popen(
        command,
        cwd=str(wav_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output = []
    try:
        for line in _iter_proc_lines(proc, ser):
            output.append(line)
            count = len(list(wav_dir.glob("*.wav")))
            if count:
                send(ser, f"PROGRESS:{min(int(count / len(chapters) * 60), 60)}%")
    except (KeyboardInterrupt, SystemExit):
        discstation_burn.stop_process(proc)
        raise
    proc.wait()
    if proc.returncode != 0:
        detail = next((line.strip() for line in reversed(output) if line.strip()), "cd-paranoia failed")
        raise RuntimeError(f"Audio CD rip failed: {detail[:80]}")

    wav_files = sorted(wav_dir.glob("*.wav"))
    if len(wav_files) < len(chapters):
        raise RuntimeError(f"Only ripped {len(wav_files)}/{len(chapters)} tracks")
    for index, wav in enumerate(wav_files[:len(chapters)], start=1):
        chapter = chapters[index - 1]
        if metadata and index <= len(metadata["tracks"]):
            track_meta = metadata["tracks"][index - 1]
            title = track_meta["title"]
        else:
            title = chapter.get("tags", {}).get("title", f"track {index:02d}")
            track_meta = {
                "number": index,
                "title": title,
                "artist": metadata["album_artist"] if metadata else "Unknown Artist",
                "recording_id": None,
                "release_track_id": None,
            }
        out_file = out_dir / f"{index:02d} - {safe_path_name(title)}.flac"
        subprocess.run(
            [discstation_burn.tool("ffmpeg"), "-y", "-i", str(wav), "-c:a", "flac", str(out_file)],
            capture_output=True,
            check=True,
        )
        if metadata:
            tag_flac(out_file, track_meta, metadata, cover_path)
        wav.unlink(missing_ok=True)
        send(ser, f"PROGRESS:{60 + int(index / len(chapters) * 40)}%")
    shutil.rmtree(str(wav_dir), ignore_errors=True)
    safe_send(ser, "PROGRESS:100%")
    safe_send(ser, "DONE:Rip complete!")
    chown_to_sudo_user(out_dir)
    time.sleep(3)


def rip_audio_cd(ser, device, artist_hint=None, album_hint=None):
    chapters = audio_cd_chapters(device)
    metadata = None
    cover_path = None

    send(ser, "STATUS:Looking up CD...")
    metadata = audio_metadata_lookup(device, len(chapters), artist_hint, album_hint)

    if metadata:
        album_folder = safe_path_name(f"{metadata['album_artist']} - {metadata['album']}")
        out_dir = unique_dir(RIP_ROOT / album_folder)
    else:
        out_dir = RIP_ROOT / time.strftime("audio_cd_%Y%m%d_%H%M%S")

    out_dir.mkdir(parents=True, exist_ok=True)
    tracks_path = write_audio_tracks_file(out_dir, chapters)
    write_album_info(out_dir, metadata)

    if metadata:
        send(ser, "STATUS:Downloading art")
        cover_path = download_cover_art(
            metadata.get("release_id"),
            out_dir,
            metadata.get("release_group_id"),
        )

    send(ser, "STATUS:Ripping audio CD")
    if metadata:
        send(ser, f"INFO:{metadata['album'][:20]}")
    else:
        send(ser, f"INFO:{len(chapters)} tracks")
    print(f"Ripping audio CD from {device} to {out_dir}")
    print(f"Track list: {tracks_path}")
    if metadata:
        print(f"Album: {metadata['album_artist']} - {metadata['album']}")
        print(f"Metadata: {metadata.get('source', 'unknown')}")
        print(f"Cover: {cover_path or 'not found'}")
    else:
        print("No MusicBrainz match; using generic track names.")

    if discstation_host.system_name() == "darwin":
        _rip_audio_cd_macos(ser, device, chapters, metadata, cover_path, out_dir)
        return

    if not chapters:
        raise RuntimeError("No audio CD tracks found")

    total_tracks = len(chapters)
    disc_duration = max((float(ch.get("end_time", 0)) for ch in chapters), default=0)
    rip_duration = max(disc_duration - 1.0, 1.0) if disc_duration > 0 else 0
    split_times = [
        float(chapter.get("end_time", 0))
        for chapter in chapters[:-1]
        if float(chapter.get("end_time", 0)) < rip_duration
    ]
    segment_template = out_dir / "track_%03d.flac"

    send(ser, "STATUS:Ripping audio CD")
    send(ser, "PROGRESS:0%")
    print("Ripping audio CD in one pass and splitting by track markers.")

    proc = subprocess.Popen(
        [
            "ffmpeg", "-y", "-nostdin",
            "-f", "libcdio", "-i", device,
            *(["-t", f"{rip_duration:.3f}"] if rip_duration else []),
            "-map", "0:a:0",
            "-c:a", "flac",
            "-f", "segment",
            "-segment_format", "flac",
            "-segment_times", ",".join(f"{value:.3f}" for value in split_times),
            "-reset_timestamps", "1",
            str(segment_template),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        for line in _iter_proc_lines(proc, ser):
            print(line, end="")
            secs = parse_ffmpeg_time(line)
            if secs is not None and rip_duration > 0:
                pct = min(int(secs / rip_duration * 100), 99)
                send(ser, f"PROGRESS:{pct}%")
    except (KeyboardInterrupt, SystemExit):
        discstation_burn.stop_process(proc)
        safe_send(ser, "CANCELLED:Rip stopped")
        raise

    proc.wait()
    if proc.returncode == -15:
        print("Rip cancelled by user")
        safe_send(ser, "CANCELLED:Rip cancelled")
        return
    elif proc.returncode != 0:
        if not disc_present(device):
            raise RuntimeError("Disc was removed during rip")
        raise RuntimeError("Audio CD rip failed")

    segment_files = sorted(out_dir.glob("track_*.flac"))
    if len(segment_files) < total_tracks:
        raise RuntimeError(f"Only ripped {len(segment_files)}/{total_tracks} tracks")

    for index, segment in enumerate(segment_files[:total_tracks], start=1):
        chapter = chapters[index - 1]
        if metadata and index <= len(metadata["tracks"]):
            track_meta = metadata["tracks"][index - 1]
            title = track_meta["title"]
        else:
            title = chapter.get("tags", {}).get("title", f"track {index:02d}")
            track_meta = {
                "number": index,
                "title": title,
                "artist": metadata["album_artist"] if metadata else "Unknown Artist",
                "recording_id": None,
                "release_track_id": None,
            }

        out_file = out_dir / f"{index:02d} - {safe_path_name(title)}.flac"
        if out_file.exists():
            out_file.unlink()
        segment.rename(out_file)

        if metadata:
            tag_flac(out_file, track_meta, metadata, cover_path)

        send(ser, f"PROGRESS:Tagged {index}/{total_tracks}")

    safe_send(ser, "PROGRESS:100%")
    safe_send(ser, "DONE:Rip complete!")
    print(f"Audio rip complete: {out_dir}")
    chown_to_sudo_user(out_dir)
    time.sleep(3)


def station_loop(ser, url, artist_hint=None, album_hint=None):
    global _last_burn_result, _last_burn_result_time, _tray_open, _tray_open_since, _operation_active
    discstation_burn.cleanup_old_jobs()
    try:
        device = discstation_burn.disc_device()
    except Exception:
        device = None  # empty drive (e.g. macOS after an eject) — keep the loop alive

    for _ in range(50):
        line = read_serial_line(ser, timeout=0.2)
        if not line:
            break
        print(f"ESP32: {line}")
        if "DISCSTATION_READY" in line:
            break

    safe_send(ser, "STANDBY:Starting...")

    last_disc_line = None
    last_disc_poll = 0
    standby = False
    print("DiscStation menu ready.")

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(disc_status_line, device)
        next_disc_line = None
        probe_deadline = time.time() + 25
        while time.time() < probe_deadline:
            if fut.done():
                try:
                    next_disc_line = fut.result()
                except Exception as e:
                    next_disc_line = "Disc: reading..."
                    print(f"Disc probe error: {e}")
                break
            safe_send(ser, "PING")
            time.sleep(4)
        if next_disc_line is None:
            next_disc_line = "Disc: reading..."
            print("Disc probe timed out")
    if next_disc_line != last_disc_line:
        prev_disc_line = last_disc_line
        last_disc_line = next_disc_line
        send_disc_info(ser, device, next_disc_line)
        print(next_disc_line)
        has_disc = "none" not in next_disc_line.lower() and "checking" not in next_disc_line.lower()
        if has_disc or "blank" in next_disc_line.lower():
            show_home(ser)
        else:
            show_standby(ser)
            standby = True
    last_disc_poll = time.time()
    discstation_burn.note_serial_activity()
    last_ping = time.time()
    _disc_poll_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    atexit.register(_disc_poll_pool.shutdown, wait=False)
    _disc_poll_future = None
    _disc_poll_start = 0

    def _poll_disc():
        try:
            current_device = discstation_burn.disc_device()
        except Exception:
            return "Disc: none", None  # no drive/media visible right now
        return disc_status_line(current_device), current_device

    def apply_disc_line(new_line):
        nonlocal last_disc_line, prev_disc_line, standby
        if not new_line or new_line == last_disc_line:
            return
        prev_disc_line = last_disc_line
        last_disc_line = new_line
        send_disc_info(ser, device, new_line)
        print(new_line)
        low = new_line.lower()
        transient = any(w in low for w in ("none", "checking", "reading", "tray open"))
        if not transient or "blank" in low:
            prev_empty = prev_disc_line is None or (prev_disc_line and "none" in prev_disc_line.lower())
            if standby or prev_empty:
                show_home(ser)
                standby = False
        elif not standby:
            show_standby(ser)
            standby = True

    last_status_check = 0.0
    last_status = None

    while True:
        now = time.time()

        # --- fast drive-state check (~1.5s), independent of the 5s ping ---------
        # CDROM_DRIVE_STATUS is a cheap ioctl that reports tray/media state
        # reliably on this USB bridge (udev's ID_CDROM_MEDIA is stale here).
        if now - last_status_check >= 1.5:
            last_status_check = now
            in_eject_guard = _tray_open and (time.monotonic() - _tray_open_since) < 8
            if not in_eject_guard:
                st = drive_status(device)
                if st == "open":
                    _tray_open = True
                    _disc_poll_future = None
                    apply_disc_line("Disc: Tray open")
                elif st == "no_disc":
                    _tray_open = False
                    _disc_poll_future = None
                    apply_disc_line("Disc: none")
                elif st == "loading":
                    _tray_open = False
                    if (last_disc_line or "").lower().find("none") >= 0 or last_disc_line is None:
                        apply_disc_line("Disc: reading...")
                elif st == "disc":
                    _tray_open = False
                    transient_words = ["none", "checking", "reading", "tray open"]
                    if discstation_host.system_name() == "darwin":
                        # macOS slot drives take a few seconds to mount; "unknown"
                        # is a not-ready read, not a settled answer — keep re-polling.
                        transient_words.append("unknown")
                    have_line = last_disc_line and not any(
                        w in last_disc_line.lower() for w in transient_words)
                    if not have_line and _disc_poll_future is None:
                        _disc_poll_start = now
                        last_disc_poll = now
                        _detect_cache.pop(device, None)  # force a fresh classify
                        _disc_poll_future = _disc_poll_pool.submit(_poll_disc)
                if st != "unknown":
                    last_status = st

        if now - last_ping >= 5:
            last_ping = now
            safe_send(ser, "PING")
            check_serial_alive(ser)

            # Slow full classify as a backstop (type changes, stuck "reading...").
            if (not _tray_open and _disc_poll_future is None
                    and now - last_disc_poll >= DISC_POLL_SECONDS):
                last_disc_poll = now
                _disc_poll_start = now
                _disc_poll_future = _disc_poll_pool.submit(_poll_disc)

        if _disc_poll_future is not None:
            next_disc_line = None
            if _disc_poll_future.done():
                try:
                    next_disc_line, polled_device = _disc_poll_future.result()
                    if polled_device:
                        device = polled_device
                except Exception as e:
                    next_disc_line = "Disc: reading..."
                    print(f"Disc poll error: {e}")
                _disc_poll_future = None
            elif now - _disc_poll_start > 25:
                _disc_poll_future = None
                next_disc_line = "Disc: reading..."
                print("Disc poll timed out (async)")
            if not _tray_open:
                apply_disc_line(next_disc_line)

        line = read_serial_line(ser, timeout=0.1)
        if not line:
            continue

        if line == "PONG":
            continue

        if line.startswith("MENU:"):
            print(f"Menu: {line.split(':', 1)[1]}")
            continue

        if line == "EJECT":
            try:
                device = discstation_burn.disc_device()
            except FileNotFoundError:
                if discstation_host.system_name() == "darwin":
                    device = None
                else:
                    raise
            safe_send(ser, "STATUS:Ejecting...")
            try:
                eject_disc(ser, device)
            except Exception as e:
                print(f"Eject handler error: {e}")
                safe_send(ser, "ERROR:Eject failed")
                time.sleep(2)
                safe_send(ser, "STANDBY:Error")
            # eject_disc talks to the OLED directly and may leave the tray in any
            # state — force station_loop to re-detect from scratch next tick.
            last_disc_line = None
            last_status = None
            last_status_check = 0.0
            _detect_cache.pop(device, None)
            continue

        if not line.startswith("SELECT:"):
            if line.startswith("WiFi") or line.startswith("IP:") or "ip:" in line.lower():
                print(f"ESP32: {line}")
            continue

        mode = line.split(":", 1)[1].strip().upper()
        print(f"Selected: {mode}")

        # The user picked a mode — they want to act on a disc, so the drive is
        # fair game again even if it was ejected from the OLED earlier.
        _tray_open = False
        _operation_active = True  # stop /disc-info probing the drive during the flow
        try:
            if mode == "BURN":
                burn_flow(ser, url)
                _last_burn_result = "Burn complete"
            elif mode == "PLAY":
                play_flow(ser)
            elif mode == "RIP":
                rip_flow(ser, artist_hint, album_hint)
                _last_burn_result = "Rip complete"
            elif mode == "BURN MPG":
                burn_mpg_flow(ser)
                _last_burn_result = "Burn complete"
            elif mode == "BURN DATA":
                burn_data_flow(ser)
                _last_burn_result = "Burn complete"
            elif mode == "BURN AUDIO":
                burn_audio_flow(ser)
                _last_burn_result = "Burn complete"
            else:
                print(f"Ignoring stale menu selection: {mode}")
                refresh_main_menu(ser)
                continue

        except KeyboardInterrupt:
            raise
        except Exception as e:
            safe_send(ser, f"ERROR:{str(e)[:50]}")
            _last_burn_result = f"ERROR: {e}"
            print(f"Error in {mode}: {e}")
            time.sleep(4)
        finally:
            _operation_active = False

        _last_burn_result_time = time.time()
        refresh_main_menu(ser)


PIDFILE = os.path.join(tempfile.gettempdir(), "discstation.pid")


def check_pidfile():
    try:
        if os.path.exists(PIDFILE):
            with open(PIDFILE) as f:
                old_pid = int(f.read().strip())
            try:
                os.kill(old_pid, 0)
                if sys.platform == "linux":
                    with open(f"/proc/{old_pid}/cmdline") as f:
                        alive = "discstation" in f.read()
                elif os.name == "nt":
                    tl = subprocess.run(["tasklist", "/FI", f"PID eq {old_pid}", "/FO", "CSV", "/NH"],
                                        capture_output=True, text=True)
                    alive = "python" in tl.stdout.lower()
                else:
                    ps = subprocess.run(["ps", "-p", str(old_pid), "-o", "command="],
                                        capture_output=True, text=True)
                    alive = "discstation" in ps.stdout
                if alive:
                    print(f"Already running (PID {old_pid}), exiting")
                    sys.exit(0)
            except (OSError, IOError, SystemError):
                # os.kill(pid, 0) on Windows raises SystemError (not OSError)
                # for some stale/reused PIDs ("WinError 87: The parameter is
                # incorrect") instead of just saying the process is gone.
                pass
    except (ValueError, OSError):
        pass
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))


def parse_args():
    parser = argparse.ArgumentParser(description="Physical DVD station controller")
    parser.add_argument("--artist", help="Audio CD album artist hint for metadata fallback")
    parser.add_argument("--album", help="Audio CD album title hint for metadata fallback")
    parser.add_argument(
        "--retag-latest-audio",
        action="store_true",
        help="Retag the newest generic audio_cd_* rip using --artist/--album, then exit",
    )
    parser.add_argument("--port", type=int, default=8080, help="Web interface port")
    parser.add_argument("url", nargs="?", help="YouTube URL or file path for burn mode")
    return parser.parse_args()


def main():
    global _active_ser, _line_buf
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    check_pidfile()

    args = parse_args()
    ser = None
    exit_code = 0

    try:
        if args.retag_latest_audio:
            rip_dir = latest_audio_rip_dir()
            new_dir, cover_path, renamed = retag_audio_rip(rip_dir, args.artist, args.album)
            print(f"Retagged: {new_dir}")
            print(f"Cover: {cover_path or 'not found'}")
            for path in renamed:
                print(path.name)
            return

        start_web_server(args.port)

        while True:
            try:
                _line_buf = b""
                port = discstation_host.serial_port()
                if not port:
                    raise serial.SerialException("No ESP32 serial port found")
                print(f"Using ESP32 serial port: {port}")
                ser = serial.Serial(port, discstation_burn.BAUD, timeout=1, write_timeout=1)
                if discstation_host.system_name() == "linux":
                    ser.setDTR(False)
                    time.sleep(0.1)
                    ser.setDTR(True)
                time.sleep(2)
                discstation_burn.reset_serial_state()
                _active_ser = ser
                station_loop(ser, args.url, args.artist, args.album)
            except (serial.SerialException, OSError, termios.error) as e:
                print(f"Disconnected ({e}), reconnecting in 3s...")
                time.sleep(3)
            except KeyboardInterrupt:
                raise
            finally:
                _active_ser = None
                if ser:
                    try:
                        ser.close()
                    except Exception:
                        pass
                    ser = None
    except KeyboardInterrupt:
        print("\nStopped.")
        exit_code = 130
    except Exception as e:
        print(f"Error: {e}")
        exit_code = 1

    if exit_code:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
