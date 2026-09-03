#!/usr/bin/env node
// `discstation` — open the local web UI in the default browser.
// The UI is a PWA: once open, use the browser's "Install DiscStation" (or the
// in-page INSTALL APP button on Chromium) to get a standalone app window.

import { spawn } from 'node:child_process';
import { get } from 'node:http';

const port = process.env.DISCSTATION_HTTP_PORT || '8081';
const url = process.argv[2] || `http://localhost:${port}/`;

function open(target) {
  const [cmd, args] =
    process.platform === 'darwin' ? ['open', [target]]
    : process.platform === 'win32' ? ['cmd', ['/c', 'start', '', target]]
    : ['xdg-open', [target]];
  const child = spawn(cmd, args, { stdio: 'ignore', detached: true });
  child.on('error', () => {
    console.log(`Open this in your browser:\n  ${target}`);
  });
  child.unref();
}

// Best-effort liveness check so a stopped host gives a clear hint.
const probe = get(url, { timeout: 1500 }, (res) => {
  res.resume();
  open(url);
});
probe.on('timeout', () => probe.destroy());
probe.on('error', () => {
  console.log(
    `DiscStation host isn't answering on ${url}\n` +
    `Start it with "discstation-setup" (first run) or check the service:\n` +
    `  linux:  systemctl --user status discstation.service\n` +
    `  macOS:  launchctl print gui/$(id -u)/com.discstation.agent`,
  );
  open(url);
});
