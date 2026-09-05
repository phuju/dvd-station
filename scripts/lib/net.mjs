import { networkInterfaces } from 'node:os';

/** First non-internal IPv4 LAN address, so other devices on the network know
 *  what to open — mirrors discstation.py's local_ip(). Falls back to
 *  'localhost' if nothing's found (e.g. no network connection). */
export function lanIp() {
  const nets = networkInterfaces();
  for (const name of Object.keys(nets)) {
    for (const net of nets[name] || []) {
      if (net.family === 'IPv4' && !net.internal) return net.address;
    }
  }
  return 'localhost';
}
