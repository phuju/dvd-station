import React, { useState } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import { MONO, Palette } from './theme';
import { Metrics } from './responsive';
import * as api from './api';

type Props = {
  visible: boolean;
  onClose: () => void;
  c: Palette;
  m: Metrics;
  prog: api.Progress;
  disc: api.DiscInfo | null;
};

// Server-computed via menu_items_for_disc() (discstation.py) and sent as
// disc.menu_items - CANCEL/HOME is a control, not a mode, and stays
// unconditional (same split as the web remote's remote-grid).
const MODE_BUTTONS: [string, string][] = [
  ['BURN', 'SELECT:BURN'],
  ['BURN DATA', 'SELECT:BURN DATA'],
  ['BURN AUDIO', 'SELECT:BURN AUDIO'],
  ['RIP', 'SELECT:RIP'],
  ['PLAY', 'SELECT:PLAY'],
];

function discStatusText(disc: api.DiscInfo | null): string {
  if (!disc) return 'DISC: UNKNOWN';
  if (disc.busy) return 'DISC: BUSY';
  if (!disc.disc_present) return 'DISC: NONE';
  const kind = (disc.type || disc.kind || 'unknown').toUpperCase();
  const label = disc.label ? ` "${disc.label}"` : '';
  const size = disc.capacity_gb ? ` // ${disc.capacity_gb}GB` : '';
  return `DISC: ${kind}${label}${size}`;
}

// Inline section, not a modal - it used to be a full-screen Modal overlay,
// which blocked the burn/upload UI underneath it entirely. Combined with
// auto-opening in software mode (the only control surface with no ESP32
// attached), that meant the burn tab was unreachable: closing the modal
// just got overridden again on the next status poll. Rendered inline like
// the web remote's panel instead - it can stay open alongside the rest of
// the page without blocking anything.
export default function RemotePanel({ visible, onClose, c, m, prog, disc }: Props) {
  const s = makeStyles(c, m);
  const [volume, setVolume] = useState(70);
  const hardware = prog.appliance === 'hardware';
  const trayOpen = !!prog.tray_open;

  if (!visible) return null;

  const send = (cmd: string) => {
    if (hardware) return Promise.resolve();
    return api.postButton(cmd).catch(() => {
      /* next poll tick reflects reality, same as the web remote */
    });
  };

  // On real hardware START is a long-press of the encoder on the burn-ready
  // review screen, not its own button - picking a mode here is meant to be
  // the whole action, so chase SELECT straight through to START instead of
  // leaving nothing left to press (mirrors app.js's web remote). Must await
  // the SELECT: request before sending START - firing both at once races
  // over the network, and if START lands first it gets wiped the moment
  // SELECT: is processed (station_loop clears any stale buffered input on
  // a fresh mode selection), leaving the burn stuck at "waiting for start."
  const sendMode = async (cmd: string) => {
    await send(cmd);
    if (cmd.startsWith('SELECT:BURN')) await send('START');
  };

  const allowedModes = disc?.menu_items;
  const visibleModeButtons = allowedModes
    ? MODE_BUTTONS.filter(([, cmd]) => allowedModes.includes(cmd.slice('SELECT:'.length)))
    : MODE_BUTTONS;

  const stepVolume = (delta: number) => {
    const next = Math.max(0, Math.min(100, volume + delta));
    setVolume(next);
    send(`POT:${next}`);
  };

  return (
    <View style={[s.panel, s.rule]}>
      <View style={s.panelHead}>
        <View style={{ flexShrink: 1 }}>
          <Text style={s.kicker}>ON-SCREEN REMOTE</Text>
          <Text style={s.h2}>CONTROL SURFACE</Text>
        </View>
        <Text style={s.panelIndex}>DISCSTN-02</Text>
      </View>
      <Text style={s.fieldNote}>
        {hardware
          ? 'A physical remote is attached — on-screen controls are disabled.'
          : 'No physical remote detected — control DiscStation from here.'}
      </Text>
      <View style={s.discStatus}>
        <Text style={s.discStatusText}>{discStatusText(disc)}</Text>
      </View>

      <View style={s.grid}>
        {visibleModeButtons.map(([label, cmd]) => (
          <Pressable
            key={cmd}
            style={[s.gridBtn, hardware && s.btnDisabled]}
            onPress={() => sendMode(cmd)}
            disabled={hardware}
          >
            <Text style={s.gridBtnText}>{label}</Text>
          </Pressable>
        ))}
        <Pressable
          style={[s.gridBtn, hardware && s.btnDisabled]}
          onPress={() => send('CANCEL')}
          disabled={hardware}
        >
          <Text style={s.gridBtnText}>CANCEL / HOME</Text>
        </Pressable>
        <Pressable
          style={[s.gridBtn, hardware && s.btnDisabled]}
          onPress={() => send(trayOpen ? 'CONFIRM' : 'EJECT')}
          disabled={hardware}
        >
          <Text style={s.gridBtnText}>{trayOpen ? 'CLOSE TRAY' : 'EJECT'}</Text>
        </Pressable>
      </View>

      {!!prog.playing && (
        <View style={s.transport}>
          <View style={s.grid}>
            <Pressable style={s.gridBtn} onPress={() => send('REW:BIG')}>
              <Text style={s.gridBtnText}>⏮ PREV</Text>
            </Pressable>
            <Pressable style={s.gridBtn} onPress={() => send('PLAY_BUTTON')}>
              <Text style={s.gridBtnText}>⏯ PLAY/PAUSE</Text>
            </Pressable>
            <Pressable style={s.gridBtn} onPress={() => send('FF:BIG')}>
              <Text style={s.gridBtnText}>⏭ NEXT</Text>
            </Pressable>
            <Pressable style={s.gridBtn} onPress={() => send('PLAY_STOP')}>
              <Text style={s.gridBtnText}>⏹ STOP</Text>
            </Pressable>
          </View>
          <Text style={s.fieldLabel}>VOLUME: {volume}</Text>
          <View style={s.grid}>
            <Pressable style={s.gridBtn} onPress={() => stepVolume(-10)}>
              <Text style={s.gridBtnText}>VOL -</Text>
            </Pressable>
            <Pressable style={s.gridBtn} onPress={() => stepVolume(10)}>
              <Text style={s.gridBtnText}>VOL +</Text>
            </Pressable>
          </View>
        </View>
      )}

      <Pressable style={s.linkBtn} onPress={onClose}>
        <Text style={s.linkBtnText}>COLLAPSE</Text>
      </Pressable>
    </View>
  );
}

function makeStyles(c: Palette, m: Metrics) {
  const { ms, sp } = m;
  return StyleSheet.create({
    panel: { paddingVertical: sp(20) },
    rule: { borderBottomWidth: 1, borderBottomColor: c.softLine },
    panelHead: {
      flexDirection: 'row',
      justifyContent: 'space-between',
      alignItems: 'flex-start',
      marginBottom: sp(10),
    },
    panelIndex: { color: c.muted, fontFamily: MONO, fontSize: ms(10), letterSpacing: 1 },
    kicker: { color: c.ink, fontFamily: MONO, fontSize: ms(10), fontWeight: '700', letterSpacing: 1.6 },
    h2: {
      color: c.ink,
      fontFamily: MONO,
      fontSize: ms(20),
      fontWeight: '900',
      marginTop: sp(6),
      marginBottom: sp(6),
    },
    fieldNote: {
      color: c.muted,
      fontFamily: MONO,
      fontSize: ms(11),
      lineHeight: ms(11) * 1.6,
      marginBottom: sp(10),
    },
    fieldLabel: {
      color: c.ink,
      fontFamily: MONO,
      fontSize: ms(10),
      fontWeight: '700',
      letterSpacing: 1.2,
      marginTop: sp(16),
      marginBottom: sp(7),
    },
    discStatus: {
      borderWidth: 1,
      borderColor: c.ink,
      backgroundColor: c.surface,
      paddingVertical: sp(10),
      paddingHorizontal: sp(12),
      marginBottom: sp(14),
    },
    discStatusText: { color: c.ink, fontFamily: MONO, fontSize: ms(10), fontWeight: '700', letterSpacing: 0.6 },
    grid: { flexDirection: 'row', flexWrap: 'wrap', gap: sp(8) },
    gridBtn: {
      flexBasis: '48%',
      flexGrow: 1,
      borderWidth: 1,
      borderColor: c.ink,
      paddingVertical: sp(13),
      paddingHorizontal: sp(8),
      alignItems: 'center',
    },
    gridBtnText: { color: c.ink, fontFamily: MONO, fontSize: ms(10), fontWeight: '700', letterSpacing: 1 },
    btnDisabled: { opacity: 0.4 },
    transport: { marginTop: sp(18), paddingTop: sp(16), borderTopWidth: 1, borderTopColor: c.softLine },
    linkBtn: { alignItems: 'center', paddingVertical: sp(14), marginTop: sp(10) },
    linkBtnText: { color: c.muted, fontFamily: MONO, fontSize: ms(10), letterSpacing: 1 },
  });
}
