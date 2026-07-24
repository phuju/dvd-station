#!/usr/bin/env python3
import argparse
import concurrent.futures
import json
import os
import signal
import pwd
import re
import select
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import datetime
from pathlib import Path
from queue import Queue, Empty
import http.server
import socketserver
import termios
import threading
import urllib.parse

import requests
import serial
from mutagen.flac import FLAC, Picture

import dvd_burn

# Web interface for URL input and file upload
_burn_url_queue = Queue()
_web_port = 8080
_web_server = None
_last_burn_result = None
_last_burn_result_time = 0
_last_upload_dir = None


class _WebHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/':
            self._serve_page()
        elif self.path == '/status':
            result = _last_burn_result
            self._respond(200, result or 'Idle')
        elif self.path == '/sw.js':
            self._serve_sw()
        elif self.path == '/manifest.json':
            self._serve_manifest()
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == '/':
            content_type = self.headers.get('Content-Type', '')
            if 'multipart/form-data' in content_type:
                self._handle_upload()
            else:
                self._handle_url()
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
        files = self._parse_multipart()
        if not files:
            self._respond(400, 'No files uploaded')
            return
        upload_dir = Path(dvd_burn.WORK) / f"upload_{time.strftime('%Y%m%d_%H%M%S')}"
        upload_dir.mkdir(parents=True, exist_ok=True)
        total = 0
        for filename, data in files:
            safe_name = Path(filename).name
            (upload_dir / safe_name).write_bytes(data)
            total += len(data)
        _last_upload_dir = str(upload_dir)
        size_str = f"{total / 1e6:.1f}MB" if total > 1e6 else f"{total / 1e3:.0f}KB"
        self._respond(200, f'{len(files)} file(s) uploaded ({size_str}). Select BURN DATA on remote.')

    def _serve_sw(self):
        sw = '''self.addEventListener('install', e => {
  self.skipWaiting();
  caches.open('dvd-v2').then(c => c.addAll(['/']));
});
self.addEventListener('activate', e => e.waitUntil(clients.claim()));
self.addEventListener('fetch', e => {
  e.respondWith(
    caches.match(e.request).then(r => r || fetch(e.request))
  );
});'''
        self._respond(200, sw, 'application/javascript')

    def _serve_manifest(self):
        icon_svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><circle cx="256" cy="256" r="230" fill="#5c9cf5" stroke="#0a0a0a" stroke-width="20"/><circle cx="256" cy="256" r="80" fill="#0a0a0a"/><rect x="176" y="246" width="160" height="20" rx="10" fill="#5c9cf5" opacity="0.7"/></svg>'
        manifest = {
            "name": "DVD Station",
            "short_name": "DVD Station",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#0a0a0a",
            "theme_color": "#0a0a0a",
            "description": "Physical disc burning station",
            "icons": [{
                "src": "data:image/svg+xml," + urllib.parse.quote(icon_svg),
                "sizes": "512x512",
                "type": "image/svg+xml",
                "purpose": "any maskable"
            }]
        }
        self._respond(200, json.dumps(manifest), 'application/json')

    def _parse_multipart(self):
        content_type = self.headers.get('Content-Type', '')
        boundary = None
        for part in content_type.split(';'):
            part = part.strip()
            if part.lower().startswith('boundary='):
                boundary = part[9:].strip('"')
        if not boundary:
            return []
        raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
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
            if filename and body:
                files.append((filename, body))
        return files

    def _serve_page(self):
        result_html = ""
        r = _last_burn_result
        t = _last_burn_result_time
        if r and t and time.time() - t < 60:
            is_err = "ERROR" in r or "error" in r or "fail" in r.lower()
            color = "#4f4" if not is_err else "#f44"
            result_html = f'<div class="status ok" style="background:{"#1a3a1a" if not is_err else "#3a1a1a"};color:{color}">{r}</div>'
        upload_ready = _last_upload_dir and Path(_last_upload_dir).exists()
        upload_info = '<p style="color:#5c9cf5;text-align:center">Files ready! Select BURN DATA on the remote.</p>' if upload_ready else ''
        html = f'''<!DOCTYPE html>
<html><head>
<title>DVD Station</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#0a0a0a">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<link rel="manifest" href="/manifest.json">
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0a0a0a;color:#ddd;max-width:500px;margin:0 auto;padding:2em 1em}}
  h1{{color:#5c9cf5;text-align:center;margin-bottom:1em;font-size:1.5em}}
  .status{{text-align:center;padding:.7em;margin-bottom:1em;border-radius:8px;font-weight:600;font-size:.95em}}
  .tabs{{display:flex;gap:0;margin-bottom:1.2em}}
  .tab{{flex:1;text-align:center;padding:.8em;cursor:pointer;background:#141414;border:1px solid #2a2a2a;color:#888;font-weight:600;font-size:.9em;transition:all .15s}}
  .tab.active{{background:#1e1e1e;color:#5c9cf5;border-bottom:2px solid #5c9cf5}}
  .tab:first-child{{border-radius:8px 0 0 8px}}
  .tab:last-child{{border-radius:0 8px 8px 0}}
  .panel{{display:none}}
  .panel.active{{display:block}}
  .hint{{color:#777;font-size:.85em;margin-bottom:.8em;text-align:center}}
  input[type=text]{{width:100%;padding:.9em;font-size:1em;background:#141414;border:1px solid #2a2a2a;color:#ddd;border-radius:8px;margin-bottom:.8em;outline:none}}
  input[type=text]:focus{{border-color:#5c9cf5}}
  button{{width:100%;padding:.9em;font-size:1em;border-radius:8px;cursor:pointer;border:none;font-weight:700;background:#5c9cf5;color:#0a0a0a;transition:background .15s}}
  button:hover{{background:#7cb5ff}}
  button:disabled{{opacity:.4;cursor:default}}
  .dropzone{{border:2px dashed #333;text-align:center;color:#888;padding:2em 1em;margin-bottom:.8em;border-radius:8px;background:#141414;transition:all .2s;cursor:pointer}}
  .dropzone:hover,.dropzone.drag{{border-color:#5c9cf5;color:#5c9cf5}}
  .dropzone .icon{{font-size:2em;margin-bottom:.3em}}
  .file-item{{padding:.5em .7em;margin:.2em 0;background:#141414;border-radius:6px;display:flex;justify-content:space-between;align-items:center;font-size:.9em}}
  .file-item .name{{color:#ccc;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:60%}}
  .file-item .size{{color:#666;font-size:.85em;flex-shrink:0;margin-left:.5em}}
  .file-item .rm{{color:#f44;cursor:pointer;margin-left:.5em;font-weight:700;font-size:1.1em;flex-shrink:0}}
  #file-list{{max-height:200px;overflow-y:auto;margin-bottom:.8em}}
</style></head>
<body>
<h1>&#x1F4BF; DVD Station</h1>
{result_html}
{upload_info}
<div id="install-spot"></div>
<div class="tabs">
  <div class="tab active" onclick="switchTab('url')">URL / Path</div>
  <div class="tab" onclick="switchTab('upload')">Upload Files</div>
</div>
<div id="panel-url" class="panel active">
  <div class="hint">YouTube URL or local file path &#x2192; video DVD</div>
  <form method="POST">
    <input type="text" name="url" placeholder="https://youtube.com/... or /path/to/file.mp4" autofocus>
    <button type="submit">Burn Video DVD</button>
  </form>
</div>
<div id="panel-upload" class="panel">
  <div class="hint">Files burned as-is &#x2192; data DVD (no quality loss)</div>
  <div class="dropzone" id="dropzone" onclick="document.getElementById('filein').click()">
    <div class="icon">&#x2B06;</div>
    <div>Tap or drop files</div>
  </div>
  <input type="file" id="filein" multiple style="display:none" onchange="addFiles(this.files)">
  <div id="file-list"></div>
  <button id="upload-btn" onclick="uploadFiles()" disabled>Upload &amp; Burn to Data DVD</button>
</div>
<script>
  let files=[];
  function switchTab(id){{
    document.querySelectorAll('.tab').forEach((t,i)=>{{t.classList.toggle('active',i===(id==='url'?0:1))}});
    document.getElementById('panel-url').classList.toggle('active',id==='url');
    document.getElementById('panel-upload').classList.toggle('active',id==='upload');
  }}
  function addFiles(fl){{for(let f of fl)files.push(f);render();}}
  function removeFile(i){{files.splice(i,1);document.getElementById('filein').value='';render();}}
  function render(){{
    let h='';files.forEach((f,i)=>{{
      let s=f.size>1e9?(f.size/1e9).toFixed(1)+'GB':f.size>1e6?(f.size/1e6).toFixed(1)+'MB':(f.size/1e3).toFixed(0)+'KB';
      h+=`<div class="file-item"><span class="name">${{f.name}}</span><span class="size">${{s}}</span><span class="rm" onclick="removeFile(${{i}})">&times;</span></div>`;
    }});
    document.getElementById('file-list').innerHTML=h;
    document.getElementById('upload-btn').disabled=files.length===0;
  }}
  async function uploadFiles(){{
    if(!files.length)return;
    let btn=document.getElementById('upload-btn');btn.disabled=true;btn.textContent='Uploading...';
    let fd=new FormData();files.forEach(f=>fd.append('files',f));
    try{{let r=await fetch('/',{{method:'POST',body:fd}});btn.textContent=await r.text();}}
    catch(e){{btn.textContent='Upload failed'}}
    if(btn.textContent.includes('Select BURN DATA')){{files=[];render();btn.disabled=true}}
    else{{btn.disabled=false;btn.textContent='Upload & Burn to Data DVD'}}
  }}
  let dz=document.getElementById('dropzone');
  dz.addEventListener('dragover',e=>{{e.preventDefault();dz.classList.add('drag')}});
  dz.addEventListener('dragleave',()=>dz.classList.remove('drag'));
  dz.addEventListener('drop',e=>{{e.preventDefault();dz.classList.remove('drag');addFiles(e.dataTransfer.files)}});
</script>
<script>if('serviceWorker' in navigator)navigator.serviceWorker.register('/sw.js')</script>
<script>
let deferredPrompt;
window.addEventListener('beforeinstallprompt', e => {{
  e.preventDefault();
  deferredPrompt = e;
  var b = document.createElement('button');
  b.textContent = '\U0001f4e5 Install App';
  b.style.cssText = 'width:100%;padding:.9em;font-size:1em;border-radius:8px;border:none;font-weight:700;background:#5c9cf5;color:#0a0a0a;margin-top:.6em;cursor:pointer;';
  b.onclick = async () => {{ deferredPrompt.prompt(); var r = await deferredPrompt.userChoice; b.textContent = r.outcome === 'accepted' ? 'Installed!' : '\U0001f4e5 Install App'; if(r.outcome==='accepted')b.style.opacity='.4'; deferredPrompt = null; }};
  document.getElementById('install-spot').appendChild(b);
}});
</script>
</body></html>'''
        self._respond(200, html, 'text/html')

    def _respond(self, code, body, ctype='text/plain'):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Connection', 'close')
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
        self.end_headers()
        self.wfile.write(body.encode() if isinstance(body, str) else body)

    def log_message(self, fmt, *args):
        pass


def start_web_server(port=8080):
    global _web_server, _web_port
    if _web_server:
        return _web_server
    _web_port = port
    server = socketserver.ThreadingTCPServer(('', port), _WebHandler, bind_and_activate=False)
    server.allow_reuse_address = True
    server.server_bind()
    server.server_activate()

    cert = Path.home() / '.local' / 'share' / 'dvd-station' / 'server.crt'
    key = Path.home() / '.local' / 'share' / 'dvd-station' / 'server.key'
    if cert.exists() and key.exists():
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert), str(key))
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        print(f"Web interface on https://0.0.0.0:{port}")
    else:
        print(f"Web interface on http://0.0.0.0:{port}")

    _web_server = server
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
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
    protocol = "https" if (Path.home() / '.local' / 'share' / 'dvd-station' / 'server.crt').exists() else "http"
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


MPV_SOCKET = "/tmp/dvd_station_mpv.sock"
RIP_ROOT = dvd_burn.USER_HOME / "dvd_rips"
USER_AGENT = "DVDStation/0.1 (local appliance; phuju)"
DISC_POLL_SECONDS = 6


def ensure_text(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="ignore")
    return str(value)


def send(ser, msg):
    dvd_burn.send(ser, msg)


def safe_send(ser, msg):
    dvd_burn.safe_send(ser, msg)


def run_as_desktop_user(cmd):
    sudo_user = os.environ.get("SUDO_USER")
    if os.geteuid() == 0 and sudo_user and sudo_user != "root":
        home = Path(dvd_burn.USER_HOME)
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


def mpv_command(command):
    try:
        payload = json.dumps({"command": command}).encode() + b"\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(MPV_SOCKET)
            sock.sendall(payload)
    except OSError:
        pass


def wait_for_socket(path, proc, timeout=8):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        if Path(path).exists():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    sock.settimeout(0.25)
                    sock.connect(path)
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


_line_buf = b""


def read_serial_line(ser, timeout=0.1):
    global _line_buf
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _line_buf:
            idx = _line_buf.find(b"\n")
            if idx >= 0:
                line = _line_buf[:idx]
                _line_buf = _line_buf[idx + 1:]
                return line.decode(errors="ignore").strip() or None
        try:
            chunk = os.read(ser.fd, 4096)
        except OSError:
            return None
        if chunk:
            chunk = _line_buf + chunk
            _line_buf = b""
            idx = chunk.find(b"\n")
            if idx >= 0:
                _line_buf = chunk[idx + 1:]
                chunk = chunk[:idx]
                return chunk.decode(errors="ignore").strip() or None
            _line_buf = chunk
        time.sleep(min(deadline - time.monotonic(), 0.05))
    return None


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


def show_standby(ser):
    safe_send(ser, "STANDBY:DVD Station")


def eject_disc(ser, device):
    global _tray_open
    print(f"Ejecting disc from {device}")
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
        safe_send(ser, "WAITING:Press SELECT/to close tray")
        last_ping = time.time()
        deadline = time.time() + 60
        tray_was_cancelled = False
        last_udev_check = 0
        while time.time() < deadline:
            if time.time() - last_ping >= 5:
                last_ping = time.time()
                safe_send(ser, "PING")
            if time.time() - last_udev_check >= 2:
                last_udev_check = time.time()
                if _tray_closed_with_disc(device):
                    print("Disc detected — tray closed manually, continuing")
                    time.sleep(1)
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
        safe_send(ser, "ERROR:Eject failed")
    if tray_was_cancelled:
        safe_send(ser, "STANDBY:Tray open")
    else:
        safe_send(ser, "STANDBY:Insert disc")
    return ok


HISTORY_FILE = dvd_burn.WORK / "burn_history.jsonl"


def append_burn_history(entry):
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def run_probe(cmd, timeout=8):
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(cmd, 124, ensure_text(e.stdout), ensure_text(e.stderr))


def udev_cdrom_properties(device):
    result = run_probe(["udevadm", "info", "--query=property", "--name", device], timeout=2)
    if result.returncode != 0:
        return {}

    properties = {}
    for line in ensure_text(result.stdout).splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        properties[key] = value
    return properties


_tray_open = False


def _tray_closed_with_disc(device):
    global _tray_open
    if not Path(device).exists():
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
    if _tray_closed_with_disc(device):
        return True
    if _tray_open:
        return False
    if not Path(device).exists():
        return False

    toc = run_probe(["wodim", "-toc", "dev=" + device], timeout=5)
    toc_text = ensure_text(toc.stdout) + ensure_text(toc.stderr)
    no_media_markers = (
        "no medium",
        "no disk",
        "no disc",
        "cannot load media",
        "tray open",
        "medium not present",
    )
    if toc.returncode == 124:
        return is_blank_disc(device)
    if toc_text.strip() and any(marker in toc_text.lower() for marker in no_media_markers):
        return False

    dvd = run_probe(["lsdvd", device], timeout=3)
    if dvd.returncode == 0:
        return True

    fs = run_probe(["blkid", "-o", "value", "-s", "TYPE", device], timeout=3)
    if fs.returncode == 0 and ensure_text(fs.stdout).strip():
        return True

    if "first:" in toc_text and "track:" in toc_text:
        return True

    if toc_text.strip() and not any(marker in toc_text.lower() for marker in no_media_markers):
        return True

    return is_blank_disc(device)


def is_blank_disc(device):
    if not Path(device).exists():
        return False

    properties = udev_cdrom_properties(device)
    if properties.get("ID_CDROM_MEDIA") != "1":
        return False

    if properties.get("ID_CDROM_MEDIA_STATE") == "blank":
        return True

    info = run_probe(["dvd+rw-mediainfo", device], timeout=3)
    if info.returncode == 0:
        text = ensure_text(info.stdout).lower()
        if "disc status: blank" in text or "disc status: empty" in text:
            return True
        if "state of last session: empty" in text:
            return True

    fs = run_probe(["blkid", "-o", "value", "-s", "TYPE", device], timeout=3)
    if fs.returncode == 0 and ensure_text(fs.stdout).strip():
        return False

    dvd = run_probe(["lsdvd", device], timeout=3)
    if dvd.returncode == 0:
        return False

    toc = run_probe(["wodim", "-toc", "dev=" + device], timeout=5)
    toc_text = ensure_text(toc.stdout) + ensure_text(toc.stderr)
    if "first:" in toc_text and "track:" in toc_text:
        return False

    return True


def disc_status_line(device):
    if not disc_present(device):
        return "Disc: none"

    labels = {
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
    try:
        return labels.get(disc_kind(device), "Disc: unknown")
    except Exception:
        return "Disc: reading..."


def disc_kind(device):
    properties = udev_cdrom_properties(device)
    if properties.get("ID_CDROM_MEDIA_STATE") == "blank":
        return "blank"

    fs = run_probe(["blkid", "-o", "value", "-s", "TYPE", device], timeout=3)
    if fs.returncode == 0 and ensure_text(fs.stdout).strip():
        fstype = ensure_text(fs.stdout).strip()
        if fstype in ("udf", "iso9660"):
            try:
                with mounted_disc(device) as mount_dir:
                    kind, _ = disc_video_files(mount_dir)
                    if kind:
                        return kind
            except RuntimeError:
                pass
            if fstype == "udf":
                return "dvd_video"
            return "data_disc"

    dvd = run_probe(["lsdvd", device], timeout=2)
    if dvd.returncode == 0:
        return "dvd_video"
    stuck = dvd.returncode == 124

    toc = run_probe(["wodim", "-toc", "dev=" + device], timeout=4)
    toc_text = ensure_text(toc.stdout) + ensure_text(toc.stderr)
    if not stuck:
        stuck = toc.returncode == 124
    if "first:" in toc_text and "track:" in toc_text:
        if "control: 2" in toc_text or "mode: -1" in toc_text:
            return "audio_cd"
        return "data_cd"

    if ensure_text(fs.stdout).strip():
        try:
            with mounted_disc(device) as mount_dir:
                kind, _ = disc_video_files(mount_dir)
                if kind:
                    return kind
        except RuntimeError:
            pass
        return "data_disc"

    if is_blank_disc(device):
        return "blank"

    if stuck:
        _stuck_count = getattr(disc_kind, "_stuck_count", 0) + 1
        disc_kind._stuck_count = _stuck_count
        if _stuck_count >= 2:
            print("Drive appears stuck (2 probes), attempting USB reset...")
            dvd_burn.reset_drive(device)
            disc_kind._stuck_count = 0
    else:
        disc_kind._stuck_count = 0

    return "unknown"


def disc_title(device):
    kind = disc_kind(device)
    if kind == "dvd_video":
        dvd = run_probe(["lsdvd", device], timeout=5)
        if dvd.returncode == 0:
            for line in dvd.stdout.splitlines():
                if line.startswith("Disc Title:"):
                    t = line.split(":", 1)[1].strip()
                    if t:
                        return t
    elif kind == "audio_cd":
        return "Audio CD"
    elif kind == "vcd":
        return "VCD"
    elif kind == "svcd":
        return "SVCD"
    return ""


def menu_items_for_disc(device):
    kind = disc_kind(device)
    items = []
    if kind == "blank":
        items = ["BURN", "BURN DATA"]
        had = dvd_burn.WORK.rglob("movie.mpg")
        if any(True for _ in had):
            items.append("BURN MPG")
    elif kind in ("dvd_video", "audio_cd", "vcd", "svcd", "video_data", "data_disc", "data_cd"):
        items = ["PLAY", "RIP"]
    else:
        items = ["BURN", "BURN DATA", "PLAY", "RIP"]
    return items


class mounted_disc:
    def __init__(self, device):
        self.device = device
        self.tmp = None

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dvd_station_disc_")
        result = subprocess.run(
            ["mount", "-o", "ro", self.device, self.tmp.name],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            self.tmp.cleanup()
            raise RuntimeError((result.stderr or result.stdout or "Could not mount disc").strip())
        return Path(self.tmp.name)

    def __exit__(self, exc_type, exc, tb):
        subprocess.run(["umount", self.tmp.name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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


def artist_credit_name(credit):
    if not credit:
        return "Unknown Artist"
    return "".join(part.get("name", "") + part.get("joinphrase", "") for part in credit).strip() or "Unknown Artist"


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


def metadata_from_musicbrainz_release(release, track_count, toc=None):
    for medium in release.get("media", []):
        tracks = medium.get("tracks", [])
        if len(tracks) != track_count:
            continue

        album_artist = artist_credit_name(release.get("artist-credit"))
        album = release.get("title") or "Unknown Album"
        date = release.get("date") or ""
        metadata = {
            "source": "musicbrainz",
            "release_id": release.get("id"),
            "release_group_id": (release.get("release-group") or {}).get("id"),
            "album": album,
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
            metadata["tracks"].append({
                "number": index,
                "title": track.get("title") or recording.get("title") or f"Track {index:02d}",
                "artist": artist_credit_name(track.get("artist-credit") or recording.get("artist-credit") or release.get("artist-credit")),
                "recording_id": recording.get("id"),
                "release_track_id": track.get("id"),
            })

        return metadata

    return None


def musicbrainz_release_details(release_id):
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
    params = {
        "toc": toc["toc"],
        "inc": "recordings+artists+artist-credits+release-groups",
        "fmt": "json",
        "cdstubs": "no",
        "media-format": "all",
    }
    headers = {"User-Agent": USER_AGENT}
    response = requests.get(
        "https://musicbrainz.org/ws/2/discid/-",
        params=params,
        headers=headers,
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()

    for release in data.get("releases", []):
        metadata = metadata_from_musicbrainz_release(release, track_count, toc)
        if metadata:
            return metadata

    return None


def musicbrainz_lookup_by_album_hints(album_artist, album, track_count):
    if not album:
        return None

    query_parts = [f'release:"{album}"']
    if album_artist:
        query_parts.append(f'artist:"{album_artist}"')

    response = requests.get(
        "https://musicbrainz.org/ws/2/release/",
        params={
            "query": " AND ".join(query_parts),
            "fmt": "json",
            "limit": 8,
        },
        headers={"User-Agent": USER_AGENT},
        timeout=20,
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

        metadata = metadata_from_musicbrainz_release(details, track_count)
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

    return metadata


def write_album_info(out_dir, metadata):
    path = out_dir / "album_info.json"
    with path.open("w") as f:
        json.dump(metadata or {"metadata_found": False}, f, indent=2, ensure_ascii=False)
    return path


def download_cover_art(release_id, out_dir, release_group_id=None):
    if not release_id and not release_group_id:
        return None

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


class CancelError(Exception):
    pass


def iter_process_events(proc, idle_seconds=1.0, ser=None):
    buffer = ""
    fd = proc.stdout.fileno()
    last_ping = time.time()

    while proc.poll() is None:
        fds = [fd]
        if ser is not None:
            fds.append(ser.fileno())
        ready, _, _ = select.select(fds, [], [], idle_seconds)
        if not ready:
            if ser is not None:
                if _check_cancel(ser):
                    dvd_burn.stop_process(proc)
                    raise CancelError
                if time.time() - last_ping >= 5:
                    dvd_burn.send(ser, "PING")
                    last_ping = time.time()
            yield None
            continue

        chunk = os.read(fd, 4096).decode(errors="ignore")
        if not chunk:
            break

        for char in chunk:
            if char in "\r\n":
                if buffer:
                    yield buffer
                    buffer = ""
            else:
                buffer += char

    while True:
        chunk = os.read(fd, 4096).decode(errors="ignore")
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


def device_size_bytes(device):
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


def burn_flow(ser, url):
    if not url:
        if sys.stdin.isatty():
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

    device = dvd_burn.dvd_device()
    disc_bytes = dvd_burn.disc_capacity_bytes(device)
    if disc_bytes:
        print(f"Disc capacity: {disc_bytes / 1_000_000_000:.2f}GB")
    else:
        label_hint = "DVD5"
        if is_blank_disc(device):
            label_hint = "DVD5 (set DVD_DISC_BYTES=8500000000 for DL)"
        print(f"Disc capacity: unknown (assuming {label_hint})")

    dvd_burn.WORK.mkdir(parents=True, exist_ok=True)
    job_dir = dvd_burn.WORK / time.strftime("job_%Y%m%d_%H%M%S")
    job_dir.mkdir()

    send(ser, "STATUS:Preflight...")
    info = dvd_burn.get_video_info(url)
    title = info["title"]
    duration = info["duration"]
    duration_line, fit_line, can_fit = dvd_burn.preflight_lines(duration, disc_bytes)
    disc_label = dvd_burn.sanitize_disc_label(title)

    print(f"Title: {title}")
    print(f"Duration: {dvd_burn.format_duration(duration)}")
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

    dl_info = dvd_burn.detect_disc_type(device)
    if dl_info["is_dual_layer"]:
        sl_target = int(os.environ.get("DVD_TARGET_BYTES", "4300000000"))
        try:
            sl_plan = dvd_burn.bitrate_plan(duration, "AUTO", sl_target)
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
            selected_mode = dvd_burn.normalize_mode(line.split(":", 1)[1])
            print(f"Burn mode: {selected_mode}")
        elif line.startswith("SPEED:"):
            burn_speed = line.split(":", 1)[1].strip()
            print(f"Burn speed: {burn_speed}")
        elif line == "START" or line.startswith("START:"):
            if ":" in line:
                selected_mode = dvd_burn.normalize_mode(line.split(":", 1)[1])
            print(f"Starting burn flow in {selected_mode} mode")
            send(ser, f"STATUS:Starting {selected_mode}...")
            break

    start_time = time.time()
    disc_type_label = "DL" if dl_info["is_dual_layer"] else "SL"
    try:
        plan = dvd_burn.bitrate_plan(duration, selected_mode, disc_bytes)
        video = dvd_burn.download(ser, url, job_dir)
        mpg, dvd_aspect = dvd_burn.convert(ser, video, job_dir, selected_mode, disc_bytes)
        srt_files = dvd_burn.find_subtitle_files(video)
        if not srt_files:
            srt_files = dvd_burn.extract_embedded_subtitles(video, job_dir)
        if srt_files:
            safe_send(ser, f"INFO:{len(srt_files)} subtitle(s)")
            mpg = dvd_burn.add_subtitles(ser, mpg, srt_files, job_dir)
        dvd_dir = dvd_burn.author(ser, mpg, job_dir, dvd_aspect)
        dvd_burn.check_dvd_size(ser, dvd_dir, disc_bytes)

        if plan["burn"]:
            dvd_burn.wait_for_burn_confirm(ser, dvd_dir, disc_bytes)
            dvd_burn.burn(ser, dvd_dir, disc_label, burn_speed, dl_info["is_dual_layer"])
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
            if f.suffix.lower() in dvd_burn.VIDEO_EXTS:
                label = dvd_burn.sanitize_disc_label(f.stem)
                break
        else:
            for f in sorted(dl.iterdir()):
                label = dvd_burn.sanitize_disc_label(f.stem)
                break
    if label == "DVD_VIDEO" or not label:
        label = dvd_burn.sanitize_disc_label(job_dir.name)
    return label


def burn_mpg_flow(ser):
    jobs = sorted(dvd_burn.WORK.glob("job_*"), reverse=True)
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
                disc_label = dvd_burn.sanitize_disc_label(mpg.stem)
                break
            else:
                idx = next((i for i, n in enumerate(candidates) if n[1][:20] == sel), None)
                if idx is not None:
                    mpg, disc_label = candidates[idx]
                    break
        elif line == "CANCEL":
            safe_send(ser, "CANCELLED:Cancelled")
            return
        time.sleep(0.05)

    device = dvd_burn.dvd_device()
    dl_info = dvd_burn.detect_disc_type(device)
    disc_bytes = dl_info["capacity"]
    dvd_burn.remux_and_burn(ser, mpg, disc_label, disc_bytes, dl_info)


def _copy_to_job(ser, src, dst_dir):
    if src.is_dir():
        items = sorted(src.iterdir())
        n = len(items)
        for i, item in enumerate(items):
            if item.is_file():
                dvd_burn.copy_with_keepalive(ser, item, dst_dir / item.name,
                                              base_pct=int(i * 100 / n), pct_span=100 / n)
    else:
        dvd_burn.copy_with_keepalive(ser, src, dst_dir / src.name)


def burn_data_flow(ser):
    global _last_upload_dir

    if _last_upload_dir and Path(_last_upload_dir).exists():
        url = _last_upload_dir
        _last_upload_dir = None
    elif sys.stdin.isatty():
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

    device = dvd_burn.dvd_device()
    dl_info = dvd_burn.detect_disc_type(device)
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
        info = dvd_burn.get_video_info(url)
        title = info["title"]

    disc_label = dvd_burn.sanitize_disc_label(title)
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

    if not is_blank_disc(device):
        raise RuntimeError("No blank disc in drive")

    dvd_burn.WORK.mkdir(parents=True, exist_ok=True)
    job_dir = dvd_burn.WORK / time.strftime("job_%Y%m%d_%H%M%S")
    job_dir.mkdir()
    download_dir = job_dir / "download"
    download_dir.mkdir()

    start_time = time.time()
    try:
        if local_path.exists():
            safe_send(ser, "STATUS:Copying files...")
            _copy_to_job(ser, local_path, download_dir)
        else:
            dvd_burn.download(ser, url, job_dir)

        files_to_burn = sorted(download_dir.iterdir())
        if not files_to_burn:
            raise RuntimeError("No files to burn")

        total_bytes = sum(f.stat().st_size for f in files_to_burn if f.is_file())
        label = "DVD5" if not dl_info["is_dual_layer"] else "DVD9"
        overhead = 0.97
        usable = (disc_bytes or 0) * overhead
        if usable and total_bytes > usable:
            size_gb = total_bytes / 1e9
            cap_gb = usable / 1e9
            raise RuntimeError(
                f"Data too large for {label}: {size_gb:.1f}GB > {cap_gb:.1f}GB disc")

        dvd_burn.burn_data(ser, files_to_burn, disc_label, burn_speed, dl_info["is_dual_layer"])
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
    except RuntimeError as e:
        msg = str(e)
        if msg == "Cancelled":
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
        raise
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


def _check_cancel(ser):
    try:
        line = read_serial_line(ser, timeout=0)
        return line in ("CANCEL", "PLAY_STOP") if line else False
    except OSError:
        return False


def _iter_proc_lines(proc, ser):
    proc_fd = proc.stdout.fileno()
    ser_fd = ser.fileno()
    buffer = ""
    last_ping = time.time()

    while proc.poll() is None:
        if time.time() - last_ping >= 5:
            dvd_burn.send(ser, "PING")
            last_ping = time.time()
        ready, _, _ = select.select([proc_fd, ser_fd], [], [], 0.5)

        if ser_fd in ready and _check_cancel(ser):
            dvd_burn.stop_process(proc)
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


def _run_mpv(ser, cmd, label, kind=None):
    try:
        os.unlink(MPV_SOCKET)
    except FileNotFoundError:
        pass

    env = os.environ.copy()
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
            dvd_burn.stop_process(proc)
            raise RuntimeError("Could not start mpv")

        time.sleep(1)
        if proc.poll() is not None:
            raise RuntimeError("mpv could not open disc")

        paused = False
        current_volume = None
        send(ser, "PLAY:PLAYING")
        print(f"{label}. Short press toggles pause; long press stops.")

        last_ping = time.time()
        while proc.poll() is None:
            if time.time() - last_ping >= 5:
                last_ping = time.time()
                safe_send(ser, "PING")

            if ser.in_waiting:
                line = ser.readline().decode(errors="ignore").strip()

                if line == "PLAY_BUTTON":
                    paused = not paused
                    mpv_command(["set_property", "pause", paused])
                    mpv_command(["set_property", "speed", 1.0])
                    send(ser, "PLAY_STATUS:PAUSED" if paused else "PLAY_STATUS:PLAYING")

                elif line == "PLAY_STOP":
                    send(ser, "STATUS:Stopping play")
                    dvd_burn.stop_process(proc)
                    break

                elif line == "FF:BIG":
                    if kind == "audio_cd":
                        mpv_command(["playlist-next"])
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
                        mpv_command(["playlist-next"])
                        send(ser, "PLAY_STATUS:Next track")
                    else:
                        mpv_command(["seek", seek_sec])
                        mpv_command(["set_property", "pause", False])
                        paused = False
                        send(ser, f"PLAY_STATUS:FF {seek_sec}s")

                elif line == "REW:BIG":
                    if kind == "audio_cd":
                        mpv_command(["playlist-prev"])
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
                        mpv_command(["playlist-prev"])
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
            dvd_burn.stop_process(proc)
        try:
            os.unlink(MPV_SOCKET)
        except FileNotFoundError:
            pass


def play_flow(ser):
    device = dvd_burn.dvd_device()
    if not shutil.which("mpv"):
        raise RuntimeError("mpv not found")

    kind = disc_kind(device)
    print(f"Disc type: {kind}")

    if kind == "dvd_video":
        cmd = [
            "mpv",
            "--input-ipc-server=" + MPV_SOCKET,
            "--force-window=yes",
            "--idle=no",
            device,
        ]
        _run_mpv(ser, cmd, "Playing DVD", kind)

    elif kind == "audio_cd":
        cmd = [
            "mpv",
            "--input-ipc-server=" + MPV_SOCKET,
            "--force-window=no",
            "--idle=no",
            "--cdrom-device=" + device,
            "cdda://",
        ]
        _run_mpv(ser, cmd, "Playing audio CD", kind)

    elif kind in ("vcd", "svcd", "video_data"):
        with mounted_disc(device) as mount_dir:
            _, files = disc_video_files(mount_dir)
            if not files:
                raise RuntimeError("No playable video files")
            cmd = [
                "mpv",
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


def rip_flow(ser, artist_hint=None, album_hint=None):
    device = dvd_burn.dvd_device()
    kind = disc_kind(device)

    if kind == "audio_cd":
        rip_audio_cd(ser, device, artist_hint, album_hint)
        return

    if kind in ("vcd", "svcd", "video_data"):
        rip_video_disc(ser, device, kind)
        return

    if kind != "dvd_video":
        raise RuntimeError(f"Unsupported disc: {kind}")

    if not shutil.which("dvdbackup"):
        raise RuntimeError("dvdbackup not found")

    out_dir = RIP_ROOT / time.strftime("rip_%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

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
        dvd_burn.stop_process(proc)
        safe_send(ser, "CANCELLED:Rip stopped")
        raise

    if proc.wait() != 0:
        if not disc_present(device):
            raise RuntimeError("Disc was removed during rip")
        raise RuntimeError("Rip failed")

    safe_send(ser, "PROGRESS:100%")
    safe_send(ser, "DONE:Rip complete!")
    print(f"Rip complete: {out_dir}")
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
        dvd_burn.stop_process(proc)
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
    dvd_burn.cleanup_old_jobs()
    device = dvd_burn.dvd_device()

    for _ in range(50):
        line = read_serial_line(ser, timeout=0.2)
        if not line:
            break
        print(f"ESP32: {line}")
        if "DVD_STATION_READY" in line:
            break

    safe_send(ser, "STANDBY:Starting...")

    last_disc_line = None
    last_disc_poll = 0
    standby = False
    print("DVD Station menu ready.")

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(disc_status_line, device)
        next_disc_line = None
        probe_deadline = time.time() + 15
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
    last_ping = time.time()
    last_pong = time.time()
    _disc_poll_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    _disc_poll_future = None
    _disc_poll_start = 0

    def _poll_disc():
        return disc_status_line(device), None

    while True:
        now = time.time()

        if now - last_ping >= 5:
            last_ping = now
            safe_send(ser, "PING")
            if now - last_pong >= 35:
                raise serial.SerialException("ESP32 not responding")

            if _disc_poll_future is None and now - last_disc_poll >= DISC_POLL_SECONDS:
                last_disc_poll = now
                _disc_poll_start = now
                _disc_poll_future = _disc_poll_pool.submit(_poll_disc)

            if _disc_poll_future is not None:
                next_disc_line = None
                if _disc_poll_future.done():
                    try:
                        next_disc_line, _ = _disc_poll_future.result()
                    except Exception as e:
                        next_disc_line = "Disc: reading..."
                        print(f"Disc poll error: {e}")
                    _disc_poll_future = None
                elif now - _disc_poll_start > 15:
                    _disc_poll_future = None
                    next_disc_line = "Disc: reading..."
                    print("Disc poll timed out (async)")

                if next_disc_line is not None and next_disc_line != last_disc_line:
                    prev_disc_line = last_disc_line
                    last_disc_line = next_disc_line
                    send_disc_info(ser, device, next_disc_line)
                    print(next_disc_line)

                    is_blank = "blank" in next_disc_line.lower()
                    has_disc = "none" not in next_disc_line.lower() and "checking" not in next_disc_line.lower()

                    if is_blank or has_disc:
                        prev_empty = prev_disc_line is None or (prev_disc_line and "none" in prev_disc_line.lower())
                        if standby or prev_empty:
                            show_home(ser)
                            standby = False
                    elif not has_disc and not standby:
                        show_standby(ser)
                        standby = True

        line = read_serial_line(ser, timeout=0.1)
        if not line:
            continue

        if line == "PONG":
            last_pong = now
            continue

        if line.startswith("MENU:"):
            print(f"Menu: {line.split(':', 1)[1]}")
            continue

        if line == "EJECT":
            device = dvd_burn.dvd_device()
            safe_send(ser, "STATUS:Ejecting...")
            try:
                eject_disc(ser, device)
            except Exception as e:
                print(f"Eject handler error: {e}")
                safe_send(ser, "ERROR:Eject failed")
                time.sleep(2)
                safe_send(ser, "STANDBY:Error")
            continue

        if not line.startswith("SELECT:"):
            continue

        mode = line.split(":", 1)[1].strip().upper()
        print(f"Selected: {mode}")

        global _last_burn_result, _last_burn_result_time
        try:
            if mode == "BURN":
                last_pong = time.time()
                burn_flow(ser, url)
                _last_burn_result = "Burn complete"
            elif mode == "PLAY":
                play_flow(ser)
            elif mode == "RIP":
                rip_flow(ser, artist_hint, album_hint)
                _last_burn_result = "Rip complete"
            elif mode == "BURN MPG":
                last_pong = time.time()
                burn_mpg_flow(ser)
                _last_burn_result = "Burn complete"
            elif mode == "BURN DATA":
                last_pong = time.time()
                burn_data_flow(ser)
                _last_burn_result = "Burn complete"
            else:
                safe_send(ser, "ERROR:Bad mode")
                time.sleep(2)

        except KeyboardInterrupt:
            raise
        except Exception as e:
            safe_send(ser, f"ERROR:{str(e)[:50]}")
            _last_burn_result = f"ERROR: {e}"
            print(f"Error in {mode}: {e}")
            time.sleep(4)

        _last_burn_result_time = time.time()
        show_home(ser)


PIDFILE = "/tmp/dvd-station.pid"


def check_pidfile():
    try:
        if os.path.exists(PIDFILE):
            with open(PIDFILE) as f:
                old_pid = int(f.read().strip())
            try:
                os.kill(old_pid, 0)
                with open(f"/proc/{old_pid}/cmdline") as f:
                    if "dvd_station" in f.read():
                        print(f"Already running (PID {old_pid}), exiting")
                        sys.exit(0)
            except (OSError, IOError):
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
                ser = serial.Serial(dvd_burn.PORT, dvd_burn.BAUD, timeout=1, write_timeout=1)
                ser.setDTR(False)
                time.sleep(0.1)
                ser.setDTR(True)
                time.sleep(2)
                station_loop(ser, args.url, args.artist, args.album)
            except (serial.SerialException, OSError, termios.error) as e:
                print(f"Serial disconnected ({e}), reconnecting in 3s...")
                time.sleep(3)
            except KeyboardInterrupt:
                raise
            finally:
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
