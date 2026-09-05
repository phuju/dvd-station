// Client for the DiscStation Python host HTTP API (discstation.py `_WebHandler`).
// The host serves plain HTTP on :8081 (added for this app) alongside the
// self-signed HTTPS UI on :8080. No auth, no /api prefix, mostly text/plain.

let base = '';

/** Accepts "192.168.1.50", "192.168.1.50:8081", or a full "http://host:port". */
export function setHost(input: string): void {
  let h = (input || '').trim();
  h = h.replace(/^https?:\/\//i, '').replace(/\/+$/, '');
  if (!h) {
    base = '';
    return;
  }
  if (!/:\d+$/.test(h)) h += ':8081';
  base = 'http://' + h;
}

export function getBase(): string {
  return base;
}

async function req(path: string, init: RequestInit = {}, timeoutMs = 6000): Promise<Response> {
  if (!base) throw new Error('No host configured');
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    return await fetch(base + path, { ...init, signal: ctrl.signal });
  } finally {
    clearTimeout(timer);
  }
}

export type Progress = {
  status: string;
  progress: number;
  active: boolean;
  appliance?: 'hardware' | 'software';
  playing?: boolean;
  tray_open?: boolean;
};
export type DiscInfo = {
  disc_present: boolean;
  capacity_bytes: number;
  capacity_gb: number;
  type: string; // "none" | "reading" | "AUDIO_CD" | "DVD5" | ...
  kind?: string;
  label?: string;
  busy?: boolean;
  appliance?: 'hardware' | 'software';
};

export async function getProgress(): Promise<Progress> {
  const r = await req('/progress');
  if (!r.ok) throw new Error('progress ' + r.status);
  return r.json(); // shape is controlled by discstation.py
}

export async function getDiscInfo(): Promise<DiscInfo> {
  const r = await req('/disc-info');
  if (!r.ok) throw new Error('disc-info ' + r.status);
  return r.json();
}

export async function ping(): Promise<string> {
  const r = await req('/status', {}, 4000);
  return (await r.text()).trim();
}

export async function submitUrl(url: string): Promise<{ ok: boolean; text: string }> {
  const r = await req('/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: 'url=' + encodeURIComponent(url),
  });
  return { ok: r.ok, text: (await r.text()).trim() };
}

export async function setLabel(label: string): Promise<void> {
  await req('/set-label', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: 'label=' + encodeURIComponent(label),
  });
}

/** Same text protocol the ESP32 remote sends (SELECT:BURN DATA, PLAY_BUTTON,
 *  CANCEL, EJECT, CONFIRM, POT:<0-100>, ...) - fed into the host's virtual
 *  serial queue, same endpoint the web on-screen remote uses. */
export async function postButton(cmd: string): Promise<void> {
  await req('/remote/button', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: 'cmd=' + encodeURIComponent(cmd),
  });
}

export type PickedFile = { uri: string; name: string; size?: number; mimeType?: string };

/** Multipart upload mirroring app.js: repeated `files` parts + a `_paths` JSON
 *  field `[{n,p}]` index-aligned to the files. Uses XHR for upload progress. */
export function uploadFiles(
  files: PickedFile[],
  onProgress?: (pct: number) => void
): Promise<{ ok: boolean; text: string }> {
  return new Promise((resolve, reject) => {
    if (!base) {
      reject(new Error('No host configured'));
      return;
    }
    const form = new FormData();
    const paths = files.map((f) => ({ n: f.name, p: f.name })); // flat — no folder trees on mobile
    files.forEach((f) => {
      form.append('files', {
        uri: f.uri,
        name: f.name,
        type: f.mimeType || 'application/octet-stream',
      } as any);
    });
    form.append('_paths', JSON.stringify(paths));

    const xhr = new XMLHttpRequest();
    xhr.open('POST', base + '/');
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(Math.round((e.loaded / e.total) * 100));
    };
    xhr.onload = () => resolve({ ok: xhr.status >= 200 && xhr.status < 300, text: xhr.responseText });
    xhr.onerror = () => reject(new Error('network'));
    xhr.ontimeout = () => reject(new Error('timeout'));
    xhr.timeout = 10 * 60 * 1000;
    xhr.send(form);
  });
}

/** SI (decimal) units — matches app.js formatSize(). */
export function formatSize(bytes: number): string {
  if (bytes >= 1e9) return (bytes / 1e9).toFixed(1) + ' GB';
  if (bytes >= 1e6) return (bytes / 1e6).toFixed(1) + ' MB';
  if (bytes >= 1e3) return Math.round(bytes / 1e3) + ' KB';
  return bytes + ' B';
}
