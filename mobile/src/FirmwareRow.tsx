import { useState } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import { MONO, Palette } from './theme';
import { Metrics } from './responsive';
import * as api from './api';

type Props = { c: Palette; m: Metrics; prog: api.Progress };

// Over-the-air update row for a hardware remote. Its own component (not part of
// RemotePanel) because RemotePanel is collapsed whenever a hardware remote is
// attached - and that is exactly when there is a remote to update.
export default function FirmwareRow({ c, m, prog }: Props) {
  const [error, setError] = useState('');
  const state = prog.remote_update;
  const line =
    state === 'available'
      ? `Remote firmware ${prog.remote_fw} — ${prog.remote_fw_latest} is available.`
      : state === 'unknown'
        ? "This remote's firmware can't update itself yet — flash it once with a USB cable, then future updates are one click."
        : '';
  const text = [line, prog.remote_update_msg ? `Update failed (${prog.remote_update_msg}).` : '', error]
    .filter(Boolean)
    .join(' ');
  if (!text) return null;

  const updating = prog.active && /^UPDATING REMOTE/.test(prog.status || '');
  const start = async () => {
    setError('');
    try {
      await api.startRemoteUpdate();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const { ms, sp } = m;
  return (
    <View style={{ paddingVertical: sp(16), borderBottomWidth: 1, borderBottomColor: c.softLine }}>
      <Text style={{ color: c.muted, fontFamily: MONO, fontSize: ms(11), lineHeight: ms(11) * 1.6 }}>{text}</Text>
      {state === 'available' && (
        <Pressable
          style={[styles.btn, { borderColor: c.ink, opacity: updating ? 0.4 : 1, marginTop: sp(10) }]}
          onPress={start}
          disabled={updating}
        >
          <Text style={{ color: c.ink, fontFamily: MONO, fontSize: ms(10), fontWeight: '700', letterSpacing: 1 }}>
            UPDATE REMOTE
          </Text>
        </Pressable>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  btn: { borderWidth: 1, paddingVertical: 13, alignItems: 'center' },
});
