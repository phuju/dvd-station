import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  ActivityIndicator,
  AppState,
  Modal,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  useColorScheme,
  useWindowDimensions,
  View,
} from 'react-native';
import { StatusBar } from 'expo-status-bar';
import {
  EdgeInsets,
  initialWindowMetrics,
  SafeAreaProvider,
  useSafeAreaInsets,
} from 'react-native-safe-area-context';
import * as DocumentPicker from 'expo-document-picker';

import { DARK, DISPLAY, LIGHT, MONO, Palette } from './src/theme';
import { makeMetrics, Metrics } from './src/responsive';
import * as api from './src/api';
import { get, set } from './src/storage';

const HOST_KEY = 'discstation.host';
const THEME_KEY = 'discstation.theme';

type Tab = 'url' | 'upload';
type Conn = 'checking' | 'online' | 'offline';

const CAPABILITIES: [string, string][] = [
  ['01', 'VIDEO DVD'],
  ['02', 'DATA DVD'],
  ['03', 'AUDIO CD'],
  ['04', 'RIP / PLAY'],
];

export default function App() {
  return (
    <SafeAreaProvider initialMetrics={initialWindowMetrics}>
      <Screen />
    </SafeAreaProvider>
  );
}

function Screen() {
  const system = useColorScheme();
  const { width, height } = useWindowDimensions();
  const insets = useSafeAreaInsets();

  const [override, setOverride] = useState<'light' | 'dark' | null>(null);
  const dark = override ? override === 'dark' : system === 'dark';
  const c = dark ? DARK : LIGHT;

  const m = useMemo(() => makeMetrics(width, height), [width, height]);
  const s = useMemo(() => makeStyles(c, m, insets), [c, m, insets.top, insets.bottom]);

  const [host, setHost] = useState('');
  const [hostInput, setHostInput] = useState('');
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testMsg, setTestMsg] = useState('');

  const [tab, setTab] = useState<Tab>('url');
  const [urlText, setUrlText] = useState('');
  const [picked, setPicked] = useState<api.PickedFile[]>([]);
  const [label, setLabel] = useState('');
  const [busy, setBusy] = useState(false);
  const [formMsg, setFormMsg] = useState<{ text: string; ok: boolean } | null>(null);

  const [conn, setConn] = useState<Conn>('checking');
  const [prog, setProg] = useState<api.Progress>({ status: 'READY', progress: -1, active: false });
  const [disc, setDisc] = useState<api.DiscInfo | null>(null);

  // ---- boot: load persisted host + theme -------------------------------------
  useEffect(() => {
    (async () => {
      const [h, t] = await Promise.all([get(HOST_KEY), get(THEME_KEY)]);
      setOverride(t === 'light' || t === 'dark' ? t : null);
      if (h) {
        setHost(h);
        setHostInput(h);
        api.setHost(h);
      } else {
        setSettingsOpen(true);
      }
    })();
  }, []);

  // ---- polling -------------------------------------------------------------
  useEffect(() => {
    if (!host) return;
    let alive = true;
    const poll = async () => {
      if (AppState.currentState !== 'active') return;
      try {
        const p = await api.getProgress();
        if (!alive) return;
        setProg(p);
        setConn('online');
      } catch {
        if (!alive) return;
        setConn('offline');
        setProg({ status: 'OFFLINE', progress: -1, active: false });
      }
      try {
        const d = await api.getDiscInfo(); // server-cached, cheap
        if (alive) setDisc(d);
      } catch {
        /* keep last */
      }
    };
    poll();
    const id = setInterval(poll, 2000);
    const sub = AppState.addEventListener('change', (st) => {
      if (st === 'active') poll();
    });
    return () => {
      alive = false;
      clearInterval(id);
      sub.remove();
    };
  }, [host]);

  // ---- actions -----------------------------------------------------------
  const testConnection = useCallback(async () => {
    api.setHost(hostInput.trim());
    setTesting(true);
    setTestMsg('');
    try {
      const status = await api.ping();
      setTestMsg('LINK OK — ' + (status || 'READY').toUpperCase());
    } catch {
      setTestMsg('NO RESPONSE');
    } finally {
      setTesting(false);
    }
  }, [hostInput]);

  const toggleTheme = useCallback(() => {
    const next = dark ? 'light' : 'dark';
    setOverride(next);
    set(THEME_KEY, next);
  }, [dark]);

  const doSubmitUrl = useCallback(async () => {
    const v = urlText.trim();
    if (!v || busy) return;
    setBusy(true);
    setFormMsg(null);
    try {
      const r = await api.submitUrl(v);
      setFormMsg({ text: r.text || (r.ok ? 'Queued.' : 'Failed.'), ok: r.ok });
      if (r.ok) setUrlText('');
    } catch (e: any) {
      setFormMsg({ text: 'Request failed: ' + (e?.message || 'error'), ok: false });
    } finally {
      setBusy(false);
    }
  }, [urlText, busy]);

  const addFiles = useCallback(async () => {
    const res = await DocumentPicker.getDocumentAsync({
      multiple: true,
      copyToCacheDirectory: true,
    });
    if (res.canceled || !res.assets) return;
    setPicked((prev) => [
      ...prev,
      ...res.assets!.map((a) => ({
        uri: a.uri,
        name: a.name,
        size: a.size ?? undefined,
        mimeType: a.mimeType ?? undefined,
      })),
    ]);
  }, []);

  const removeFile = useCallback((i: number) => {
    setPicked((prev) => prev.filter((_, idx) => idx !== i));
  }, []);

  const doUpload = useCallback(async () => {
    if (!picked.length || busy) return;
    setBusy(true);
    setFormMsg(null);
    setProg({ status: 'UPLOADING', progress: 0, active: true });
    try {
      const lbl = label.trim();
      if (lbl) await api.setLabel(lbl);
      const r = await api.uploadFiles(picked, (pct) =>
        setProg({ status: 'UPLOADING', progress: pct, active: true })
      );
      setFormMsg({ text: r.text || (r.ok ? 'Uploaded.' : 'Failed.'), ok: r.ok });
      if (r.ok) {
        setPicked([]);
        setProg({ status: 'UPLOAD READY', progress: 100, active: false });
      }
    } catch (e: any) {
      setFormMsg({ text: 'Upload failed: ' + (e?.message || 'error'), ok: false });
    } finally {
      setBusy(false);
    }
  }, [picked, label, busy]);

  // ---- derived ---------------------------------------------------------------
  const selectedBytes = picked.reduce((n, f) => n + (f.size || 0), 0);
  const discBytes = disc?.capacity_bytes || 0;
  const discType = disc?.type || 'none';
  const meterPct = discBytes ? Math.min((selectedBytes / discBytes) * 100, 100) : 0;
  const showMeter = discBytes > 0 && picked.length > 0;

  const connLabel =
    conn === 'online' ? 'LINK: LIVE' : conn === 'offline' ? 'LINK: OFFLINE' : 'LINK: CHECKING';
  const connColor = conn === 'online' ? c.online : conn === 'offline' ? c.offline : c.muted;

  return (
    <View style={s.root}>
      <StatusBar style={dark ? 'light' : 'dark'} />
      <ScrollView
        style={s.scroll}
        contentContainerStyle={s.shell}
        keyboardShouldPersistTaps="handled"
      >
        {/* ---- topbar ---- */}
        <View style={[s.topbar, s.rule]}>
          <View style={s.brand}>
            <View style={s.brandMark}>
              <Text style={s.brandMarkText}>DS</Text>
            </View>
            <Text style={s.brandText}>DISCSTATION</Text>
          </View>
          <View style={s.topActions}>
            <Pressable style={s.iconBtn} onPress={toggleTheme}>
              <Text style={s.iconBtnText}>{dark ? '☀' : '☾'}</Text>
            </Pressable>
            <Pressable style={s.iconBtn} onPress={() => setSettingsOpen(true)}>
              <Text style={s.iconBtnText}>{'⚙'}</Text>
            </Pressable>
            <View style={s.conn}>
              <View style={[s.connDot, { backgroundColor: connColor, borderColor: connColor }]} />
              <Text style={s.connText}>{connLabel}</Text>
            </View>
          </View>
        </View>

        {/* ---- capability strip ---- */}
        <View style={[s.capStrip, s.rule]}>
          {CAPABILITIES.map(([n, t]) => (
            <View key={n} style={s.capCell}>
              <Text style={s.capNum}>{n}</Text>
              <Text style={s.capLabel}>{t}</Text>
            </View>
          ))}
        </View>

        {/* ---- burn panel ---- */}
        <View style={[s.panel, s.rule]}>
          <View style={s.panelHead}>
            <View style={{ flexShrink: 1 }}>
              <Text style={s.kicker}>NEW BURN</Text>
              <Text style={s.h2}>LOAD THE MEDIA</Text>
            </View>
            <Text style={s.panelIndex}>DISCSTN-01</Text>
          </View>

          <View style={s.tabs}>
            {(['url', 'upload'] as Tab[]).map((t) => (
              <Pressable key={t} onPress={() => setTab(t)} style={[s.tab, tab === t && s.tabActive]}>
                <Text style={[s.tabText, tab === t && s.tabTextActive]}>
                  {t === 'url' ? 'URL / PATH' : 'UPLOAD FILES'}
                </Text>
              </Pressable>
            ))}
          </View>

          {(prog.active || prog.progress >= 0) && (
            <View style={s.globalProgress}>
              <View style={s.progRow}>
                <Text style={s.progPhase}>{(prog.status || 'READY').toUpperCase()}</Text>
                <Text style={s.progPhase}>{prog.progress >= 0 ? `${prog.progress}%` : '...'}</Text>
              </View>
              <Bar c={c} m={m} pct={prog.progress} />
            </View>
          )}

          {tab === 'url' ? (
            <View style={s.tabPanel}>
              <Text style={s.fieldNote}>
                YouTube URL, search term, or local path for a video DVD.
              </Text>
              <TextInput
                style={s.input}
                value={urlText}
                onChangeText={setUrlText}
                autoCapitalize="none"
                autoCorrect={false}
                placeholder="https://... or /path/to/file"
                placeholderTextColor={c.muted}
              />
              <Pressable
                style={[s.blackBtn, busy && s.btnDisabled]}
                onPress={doSubmitUrl}
                disabled={busy}
              >
                <Text style={s.blackBtnText}>{busy ? 'QUEUING //' : 'BURN TO DISC'}</Text>
                {!busy && <Text style={s.blackBtnText}>//</Text>}
              </Pressable>
            </View>
          ) : (
            <View style={s.tabPanel}>
              <Text style={s.fieldNote}>
                Select files from this device. They are written to a data disc.
              </Text>

              {showMeter && (
                <View style={s.discMeter}>
                  <View style={s.progRow}>
                    <Text style={s.meterText}>DISC: {discType.toUpperCase()}</Text>
                    <Text style={s.meterText}>
                      {api.formatSize(selectedBytes)} / {api.formatSize(discBytes)}
                    </Text>
                  </View>
                  <Bar c={c} m={m} pct={meterPct} />
                </View>
              )}

              <Pressable style={s.outlineBtn} onPress={addFiles}>
                <Text style={s.outlineBtnText}>ADD FILES</Text>
              </Pressable>

              <View style={s.selectionList}>
                {picked.length === 0 ? (
                  <Text style={s.emptySelection}>NO MEDIA SELECTED</Text>
                ) : (
                  picked.map((f, i) => (
                    <View key={f.uri + i} style={s.selRow}>
                      <Text style={s.selName} numberOfLines={1}>
                        {f.name}
                      </Text>
                      <Text style={s.selMeta}>{f.size ? api.formatSize(f.size) : ''}</Text>
                      <Pressable onPress={() => removeFile(i)} hitSlop={8}>
                        <Text style={s.selRemove}>X</Text>
                      </Pressable>
                    </View>
                  ))
                )}
              </View>

              <Text style={s.fieldLabel}>DISC LABEL</Text>
              <TextInput
                style={s.input}
                value={label}
                onChangeText={setLabel}
                maxLength={32}
                autoCapitalize="characters"
                autoCorrect={false}
                placeholder="DISCSTATION_ARCHIVE"
                placeholderTextColor={c.muted}
              />

              <Text style={s.summary}>
                {picked.length} FILE{picked.length === 1 ? '' : 'S'}
              </Text>

              <Pressable
                style={[s.blackBtn, (busy || !picked.length) && s.btnDisabled]}
                onPress={doUpload}
                disabled={busy || !picked.length}
              >
                <Text style={s.blackBtnText}>{busy ? 'UPLOADING //' : 'UPLOAD TO DISC'}</Text>
                {!busy && <Text style={s.blackBtnText}>//</Text>}
              </Pressable>
            </View>
          )}

          {formMsg && (
            <Text style={[s.formMsg, { color: formMsg.ok ? c.online : c.offline }]}>
              {formMsg.text}
            </Text>
          )}
        </View>

        {/* ---- footer note ---- */}
        <View style={[s.footerNote, s.rule]}>
          <Text style={[s.kicker, { color: c.ink }]}>DISCSTATION // CONTROL SURFACE</Text>
          <Text style={s.footerTitle}>PHYSICAL MEDIA, ENGINEERED.</Text>
          <Text style={s.footerBody}>
            A compact authoring and archival instrument for discs that still deserve a place on the
            shelf.
          </Text>
          <View style={s.specs}>
            <Text style={s.specText}>MODEL: ESP32 DEVKIT V1</Text>
            <Text style={s.specText}>REF: DISCSTN-01</Text>
            <Text style={s.specText}>REV: 001</Text>
          </View>
        </View>

        {/* ---- footer stamp ---- */}
        <View style={s.footerStamp}>
          <Text style={s.stampText}>SYSTEM: {(prog.status || 'READY').toUpperCase()}</Text>
          <Text style={s.stampText}>DISCSTATION // REV 001</Text>
        </View>
      </ScrollView>

      {/* ---- settings modal ---- */}
      <Modal
        visible={settingsOpen}
        transparent
        animationType="fade"
        onRequestClose={() => setSettingsOpen(false)}
      >
        <View style={s.modalWrap}>
          <View style={s.modalCard}>
            <Text style={s.kicker}>CONNECTION</Text>
            <Text style={s.h2Small}>APPLIANCE HOST</Text>
            <Text style={s.fieldNote}>
              The DiscStation IP on your network. Port defaults to 8081 (the app's HTTP port).
            </Text>
            <TextInput
              style={s.input}
              value={hostInput}
              onChangeText={setHostInput}
              autoCapitalize="none"
              autoCorrect={false}
              keyboardType="url"
              placeholder="192.168.1.50  or  192.168.1.50:8081"
              placeholderTextColor={c.muted}
            />
            {!!testMsg && <Text style={[s.formMsg, { color: c.muted }]}>{testMsg}</Text>}
            <Pressable style={s.outlineBtn} onPress={testConnection} disabled={testing}>
              {testing ? (
                <ActivityIndicator color={c.ink} />
              ) : (
                <Text style={s.outlineBtnText}>TEST CONNECTION</Text>
              )}
            </Pressable>
            <Pressable
              style={s.blackBtn}
              onPress={async () => {
                const v = hostInput.trim();
                api.setHost(v);
                setHost(v);
                await set(HOST_KEY, v);
                setSettingsOpen(false);
              }}
            >
              <Text style={s.blackBtnText}>SAVE</Text>
              <Text style={s.blackBtnText}>//</Text>
            </Pressable>
            <Pressable style={s.linkBtn} onPress={() => setSettingsOpen(false)}>
              <Text style={s.linkBtnText}>CLOSE</Text>
            </Pressable>
          </View>
        </View>
      </Modal>
    </View>
  );
}

/** Terracotta progress/meter bar — 1px border track + accent fill.
 *  pct < 0 → indeterminate (dimmed 34% fill). */
function Bar({ c, m, pct }: { c: Palette; m: Metrics; pct: number }) {
  const indeterminate = pct < 0;
  return (
    <View style={{ height: m.sp(8), borderWidth: 1, borderColor: c.ink }}>
      <View
        style={{
          height: '100%',
          backgroundColor: c.accent,
          width: indeterminate ? '34%' : `${Math.max(0, Math.min(100, pct))}%`,
          opacity: indeterminate ? 0.55 : 1,
        }}
      />
    </View>
  );
}

function makeStyles(c: Palette, m: Metrics, insets: EdgeInsets) {
  const { ms, sp } = m;
  const rule = { borderBottomWidth: 1, borderBottomColor: c.ink };
  return StyleSheet.create({
    root: { flex: 1, backgroundColor: c.paper },
    scroll: { flex: 1 },
    shell: {
      paddingHorizontal: m.gutter,
      paddingTop: insets.top,
      paddingBottom: insets.bottom + sp(40),
      width: '100%',
      maxWidth: m.contentMax,
      alignSelf: 'center',
    },
    rule,

    topbar: {
      minHeight: sp(60),
      flexDirection: 'row',
      alignItems: 'center',
      justifyContent: 'space-between',
      flexWrap: 'wrap',
      gap: sp(8),
      paddingVertical: sp(8),
      ...rule,
    },
    brand: { flexDirection: 'row', alignItems: 'center', gap: sp(10) },
    brandMark: {
      width: ms(25),
      height: ms(25),
      borderWidth: 1,
      borderColor: c.ink,
      alignItems: 'center',
      justifyContent: 'center',
    },
    brandMarkText: { color: c.ink, fontFamily: MONO, fontSize: ms(9) },
    brandText: {
      color: c.ink,
      fontFamily: MONO,
      fontWeight: '700',
      fontSize: ms(12),
      letterSpacing: 1.2,
    },
    topActions: { flexDirection: 'row', alignItems: 'center', gap: sp(8) },
    iconBtn: {
      width: ms(30),
      height: ms(30),
      borderWidth: 1,
      borderColor: c.ink,
      alignItems: 'center',
      justifyContent: 'center',
    },
    iconBtnText: { color: c.ink, fontSize: ms(15) },
    conn: { flexDirection: 'row', alignItems: 'center', gap: sp(6), marginLeft: sp(4) },
    connDot: { width: ms(8), height: ms(8), borderRadius: ms(8) / 2, borderWidth: 1 },
    connText: { color: c.ink, fontFamily: MONO, fontSize: ms(9), letterSpacing: 1 },

    capStrip: { flexDirection: 'row', flexWrap: 'wrap', ...rule },
    capCell: {
      width: m.wide ? '25%' : '50%',
      flexDirection: 'row',
      gap: sp(10),
      paddingVertical: sp(15),
    },
    capNum: { color: c.ink, fontFamily: MONO, fontSize: ms(10), fontWeight: '700' },
    capLabel: { color: c.ink, fontFamily: MONO, fontSize: ms(10), letterSpacing: 1 },

    panel: { paddingVertical: sp(30), ...rule },
    panelHead: {
      flexDirection: 'row',
      justifyContent: 'space-between',
      alignItems: 'flex-end',
      gap: sp(16),
    },
    kicker: { color: c.ink, fontFamily: MONO, fontSize: ms(10), fontWeight: '700', letterSpacing: 1.6 },
    h2: {
      color: c.ink,
      fontFamily: DISPLAY,
      fontSize: ms(40),
      fontWeight: '900',
      letterSpacing: -1,
      marginTop: sp(8),
    },
    h2Small: {
      color: c.ink,
      fontFamily: DISPLAY,
      fontSize: ms(26),
      fontWeight: '900',
      letterSpacing: -0.5,
      marginTop: sp(6),
      marginBottom: sp(6),
    },
    panelIndex: { color: c.muted, fontFamily: MONO, fontSize: ms(10) },

    tabs: { flexDirection: 'row', marginTop: sp(24), borderBottomWidth: 1, borderBottomColor: c.ink },
    tab: { paddingVertical: sp(12), paddingHorizontal: sp(14) },
    tabActive: { borderBottomWidth: 2, borderBottomColor: c.accent, marginBottom: -1 },
    tabText: { color: c.ink, fontFamily: MONO, fontSize: ms(10), letterSpacing: 1.2 },
    tabTextActive: { fontWeight: '700' },

    globalProgress: { marginTop: sp(18), gap: sp(7) },
    progRow: { flexDirection: 'row', justifyContent: 'space-between' },
    progPhase: { color: c.ink, fontFamily: MONO, fontSize: ms(10), letterSpacing: 1 },

    tabPanel: { paddingTop: sp(20) },
    fieldNote: {
      color: c.muted,
      fontFamily: MONO,
      fontSize: ms(11),
      lineHeight: ms(11) * 1.6,
      marginBottom: sp(14),
    },
    fieldLabel: {
      color: c.ink,
      fontFamily: MONO,
      fontSize: ms(10),
      fontWeight: '700',
      letterSpacing: 1.2,
      marginTop: sp(20),
      marginBottom: sp(7),
    },
    input: {
      borderWidth: 1,
      borderColor: c.ink,
      minHeight: sp(52),
      paddingHorizontal: sp(15),
      paddingVertical: sp(12),
      color: c.ink,
      backgroundColor: c.surface,
      fontFamily: MONO,
      fontSize: ms(12),
    },
    blackBtn: {
      marginTop: sp(14),
      borderWidth: 1,
      borderColor: c.ink,
      paddingVertical: sp(16),
      paddingHorizontal: sp(16),
      backgroundColor: c.accent,
      flexDirection: 'row',
      justifyContent: 'space-between',
    },
    blackBtnText: {
      color: c.accentText,
      fontFamily: MONO,
      fontWeight: '700',
      fontSize: ms(12),
      letterSpacing: 1.2,
    },
    btnDisabled: { opacity: 0.45 },
    outlineBtn: {
      borderWidth: 1,
      borderColor: c.ink,
      paddingVertical: sp(13),
      paddingHorizontal: sp(8),
      alignItems: 'center',
      marginBottom: sp(15),
    },
    outlineBtnText: {
      color: c.ink,
      fontFamily: MONO,
      fontSize: ms(10),
      fontWeight: '700',
      letterSpacing: 1,
    },
    linkBtn: { alignItems: 'center', paddingVertical: sp(12) },
    linkBtnText: { color: c.muted, fontFamily: MONO, fontSize: ms(10), letterSpacing: 1 },

    discMeter: { marginBottom: sp(18), gap: sp(7) },
    meterText: { color: c.ink, fontFamily: MONO, fontSize: ms(10), letterSpacing: 0.5 },

    selectionList: {
      borderTopWidth: 1,
      borderTopColor: c.softLine,
      maxHeight: Math.max(180, m.height * 0.35),
    },
    selRow: {
      flexDirection: 'row',
      alignItems: 'center',
      gap: sp(10),
      paddingVertical: sp(10),
      borderBottomWidth: 1,
      borderBottomColor: c.softLine,
    },
    selName: { flex: 1, color: c.ink, fontFamily: MONO, fontSize: ms(10) },
    selMeta: { color: c.muted, fontFamily: MONO, fontSize: ms(10) },
    selRemove: {
      color: c.ink,
      fontFamily: MONO,
      fontSize: ms(10),
      fontWeight: '700',
      paddingHorizontal: sp(4),
    },
    emptySelection: {
      color: c.muted,
      fontFamily: MONO,
      fontSize: ms(10),
      letterSpacing: 1,
      paddingVertical: sp(16),
    },
    summary: { color: c.muted, fontFamily: MONO, fontSize: ms(10), marginTop: sp(10), lineHeight: ms(10) * 1.6 },
    formMsg: { fontFamily: MONO, fontSize: ms(10), lineHeight: ms(10) * 1.6, marginTop: sp(10) },

    footerNote: { paddingVertical: sp(26), ...rule },
    footerTitle: {
      color: c.ink,
      fontFamily: DISPLAY,
      fontSize: ms(30),
      fontWeight: '900',
      letterSpacing: -0.5,
      marginTop: sp(8),
    },
    footerBody: {
      color: c.muted,
      fontFamily: MONO,
      fontSize: ms(10),
      lineHeight: ms(10) * 1.7,
      marginTop: sp(12),
      maxWidth: 520,
    },
    specs: { flexDirection: 'row', flexWrap: 'wrap', gap: sp(5), marginTop: sp(10) },
    specText: {
      color: c.muted,
      fontFamily: MONO,
      fontSize: ms(9),
      letterSpacing: 0.8,
      marginRight: sp(16),
    },

    footerStamp: {
      flexDirection: 'row',
      justifyContent: 'space-between',
      flexWrap: 'wrap',
      gap: sp(8),
      paddingVertical: sp(18),
    },
    stampText: { color: c.ink, fontFamily: MONO, fontSize: ms(10), letterSpacing: 0.8 },

    modalWrap: {
      flex: 1,
      backgroundColor: 'rgba(0,0,0,0.55)',
      justifyContent: 'center',
      alignItems: 'center',
      paddingHorizontal: m.gutter,
      paddingTop: insets.top,
      paddingBottom: insets.bottom,
    },
    modalCard: {
      backgroundColor: c.paper,
      borderWidth: 1,
      borderColor: c.ink,
      padding: sp(20),
      width: '100%',
      maxWidth: 460,
    },
  });
}
