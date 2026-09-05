import React, { useState } from 'react';
import { Modal, Pressable, StyleSheet, Text, View } from 'react-native';

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

const MODE_BUTTONS: [string, string][] = [
  ['BURN', 'SELECT:BURN'],
  ['BURN DATA', 'SELECT:BURN DATA'],
  ['BURN AUDIO', 'SELECT:BURN AUDIO'],
  ['RIP', 'SELECT:RIP'],
  ['PLAY', 'SELECT:PLAY'],
  ['CANCEL / HOME', 'CANCEL'],
];

function discStatusText(disc: api.DiscInfo | null): string {
  if (!disc) return 'DISC: UNKNOWN';
  if (disc.busy) return 'DISC: BUSY (see status above)';
  if (!disc.disc_present) return 'DISC: NONE';
  const kind = (disc.type || disc.kind || 'unknown').toUpperCase();
  const label = disc.label ? ` "${disc.label}"` : '';
  const size = disc.capacity_gb ? ` // ${disc.capacity_gb}GB` : '';
  return `DISC: ${kind}${label}${size}`;
}

export default function RemoteModal({ visible, onClose, c, m, prog, disc }: Props) {
  const s = makeStyles(c, m);
  const [volume, setVolume] = useState(70);
  const hardware = prog.appliance === 'hardware';
  const trayOpen = !!prog.tray_open;

  const send = (cmd: string) => {
    if (hardware) return;
    api.postButton(cmd).catch(() => {
      /* next poll tick reflects reality, same as the web remote */
    });
  };

  const stepVolume = (delta: number) => {
    const next = Math.max(0, Math.min(100, volume + delta));
    setVolume(next);
    send(`POT:${next}`);
  };

  return (
    <Modal visible={visible} transparent animationType="fade" onRequestClose={onClose}>
      <View style={s.modalWrap}>
        <View style={s.modalCard}>
          <Text style={s.kicker}>ON-SCREEN REMOTE</Text>
          <Text style={s.h2Small}>CONTROL SURFACE</Text>
          <Text style={s.fieldNote}>
            {hardware
              ? 'A physical remote is attached — on-screen controls are disabled.'
              : 'No physical remote detected — control DiscStation from here.'}
          </Text>
          <View style={s.discStatus}>
            <Text style={s.discStatusText}>{discStatusText(disc)}</Text>
          </View>

          <View style={s.grid}>
            {MODE_BUTTONS.map(([label, cmd]) => (
              <Pressable
                key={cmd}
                style={[s.gridBtn, hardware && s.btnDisabled]}
                onPress={() => send(cmd)}
                disabled={hardware}
              >
                <Text style={s.gridBtnText}>{label}</Text>
              </Pressable>
            ))}
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
            <Text style={s.linkBtnText}>CLOSE</Text>
          </Pressable>
        </View>
      </View>
    </Modal>
  );
}

function makeStyles(c: Palette, m: Metrics) {
  const { ms, sp } = m;
  return StyleSheet.create({
    modalWrap: {
      flex: 1,
      backgroundColor: 'rgba(0,0,0,0.55)',
      justifyContent: 'center',
      alignItems: 'center',
      paddingHorizontal: m.gutter,
    },
    modalCard: {
      backgroundColor: c.paper,
      borderWidth: 1,
      borderColor: c.ink,
      padding: sp(20),
      width: '100%',
      maxWidth: 460,
    },
    kicker: { color: c.ink, fontFamily: MONO, fontSize: ms(10), fontWeight: '700', letterSpacing: 1.6 },
    h2Small: {
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
