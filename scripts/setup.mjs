#!/usr/bin/env node
// One entry point for installing the DiscStation host on any OS.
// It just dispatches to the platform installer that already exists in the repo
// (install.sh / install-macos.sh / install-windows.ps1); extra CLI args and the
// DISCSTATION_* / DISC_* env vars are forwarded through.

import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const args = process.argv.slice(2);

if (args.includes('-h') || args.includes('--help')) {
  process.stdout.write(
    `discstation-setup — install the DiscStation host

Runs the installer for the current OS:
  linux    install.sh          full: apt deps, venv, systemd --user service, self-signed cert
  darwin   install-macos.sh    full: Homebrew deps, venv, launchd agent, cert (audio-CD burn is best-effort)
  windows  install-windows.ps1 files + venv only; web/serial control, no burn backend

Prereqs: Node 16+, Python 3 (python3 / py on PATH), and either bash (linux/macOS)
or PowerShell (Windows). Homebrew is auto-installed on macOS if missing; the
Linux script uses sudo for apt + group membership.

Env vars (forwarded): DISCSTATION_APP_DIR, DISCSTATION_VENV_DIR,
DISCSTATION_CONFIG_DIR, DISCSTATION_HTTP_PORT, DISC_DEVICE, DISC_PORT.

Any extra arguments are passed straight to the platform script.
`
  );
  process.exit(0);
}

function run(cmd, cmdArgs) {
  const r = spawnSync(cmd, cmdArgs, { stdio: 'inherit', cwd: ROOT });
  if (r.error) {
    console.error(`\n${cmd}: ${r.error.message}`);
    process.exit(1);
  }
  process.exit(r.status ?? 1);
}

function needPython(bin) {
  const r = spawnSync(bin, ['--version'], { stdio: 'ignore' });
  return r.status === 0;
}

switch (process.platform) {
  case 'linux':
  case 'darwin': {
    const script = process.platform === 'linux' ? 'install.sh' : 'install-macos.sh';
    if (!needPython('python3')) {
      console.error('Python 3 not found on PATH. Install it first, then re-run.');
      process.exit(1);
    }
    run('bash', [join(ROOT, script), ...args]);
    break;
  }
  case 'win32': {
    if (!needPython('py') && !needPython('python')) {
      console.error('Python 3 not found. Install from https://www.python.org/downloads/windows/');
      process.exit(1);
    }
    const ps1 = join(ROOT, 'install-windows.ps1');
    if (!existsSync(ps1)) {
      console.error('install-windows.ps1 missing from the package.');
      process.exit(1);
    }
    run('powershell.exe', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ps1, ...args]);
    break;
  }
  default:
    console.error(`Unsupported platform: ${process.platform}. Run one of the install.* scripts by hand.`);
    process.exit(1);
}
