#!/usr/bin/env node
// Runs automatically after `npm install -g discstation` (fresh install or an
// update). Closes the class of bug where `npm install -g` updates the
// package but an already-running background service (systemd/launchd/a
// Windows Scheduled Task) keeps serving the old code because nobody
// remembered to separately re-run `discstation-setup` afterward.
//
// Deliberately lightweight: only copies files + restarts the service if one
// is *already* installed. Never touches brew/apt/winget or does a first
// install - that stays an explicit, visible `discstation-setup` run, since
// it needs heavier system-package work a silent postinstall shouldn't do.
import { existsSync, cpSync } from 'node:fs';
import { homedir, platform } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');

// Never auto-run inside a dev checkout of this repo itself.
if (existsSync(join(ROOT, '.git'))) process.exit(0);
// Only for a real global install - a project merely depending on this
// package shouldn't have a background service silently touched.
if (!process.env.npm_config_global) process.exit(0);

function configDir() {
  if (process.env.DISCSTATION_CONFIG_DIR) return process.env.DISCSTATION_CONFIG_DIR;
  const p = platform();
  if (p === 'win32') return join(process.env.APPDATA || homedir(), 'DiscStation');
  if (p === 'darwin') return join(homedir(), 'Library', 'Application Support', 'DiscStation');
  return join(homedir(), '.local', 'share', 'discstation');
}

const appDir = process.env.DISCSTATION_APP_DIR || join(configDir(), 'app');

function alreadyInstalled() {
  const p = platform();
  if (p === 'darwin') {
    return existsSync(join(homedir(), 'Library', 'LaunchAgents', 'com.discstation.agent.plist'));
  }
  if (p === 'win32') {
    const r = spawnSync('schtasks', ['/Query', '/TN', 'DiscStation'], { stdio: 'ignore' });
    return r.status === 0;
  }
  return existsSync(join(homedir(), '.config', 'systemd', 'user', 'discstation.service'));
}

function restartService() {
  const p = platform();
  try {
    if (p === 'darwin') {
      spawnSync('launchctl', ['kickstart', '-k', `gui/${process.getuid()}/com.discstation.agent`], { stdio: 'ignore' });
    } else if (p === 'win32') {
      spawnSync(
        'powershell.exe',
        [
          '-NoProfile',
          '-Command',
          'Stop-ScheduledTask -TaskName DiscStation -ErrorAction SilentlyContinue; Start-ScheduledTask -TaskName DiscStation',
        ],
        { stdio: 'ignore' }
      );
    } else {
      spawnSync('systemctl', ['--user', 'restart', 'discstation.service'], { stdio: 'ignore' });
    }
  } catch {
    /* best-effort - a failed restart here shouldn't fail the npm install */
  }
}

if (!existsSync(appDir) || !alreadyInstalled()) {
  console.log('\ndiscstation: first install detected - run `discstation-setup` to finish setting up the host.\n');
  process.exit(0);
}

try {
  cpSync(join(ROOT, 'src'), appDir, { recursive: true, force: true });
  restartService();
  console.log('\ndiscstation: redeployed the updated host and restarted the service.\n');
} catch (e) {
  console.log(`\ndiscstation: could not auto-redeploy (${e.message}). Run \`discstation-setup\` manually.\n`);
}
