import { useEffect, useMemo, useRef, useState } from 'react';
import { Animated, AppState, Easing, StyleSheet, View } from 'react-native';
import EventSource from 'react-native-sse';

import { Palette } from './theme';
import { Metrics } from './responsive';
import * as api from './api';

type Props = { c: Palette; m: Metrics };

// Literal port of src/static/app.js's VU bar logic - see that file's own
// comments for the "why" behind each piece (irregular SSE arrival timing,
// nearest-neighbor vs interpolation, the spinning-disc fallback). Keep the
// constants in sync with app.js's RP_BAR_WIDTH/RP_BAR_GAP/easing/peak-decay/
// VU_TIMEOUT_MS if those are ever retuned there.
// BAR_WIDTH/BAR_GAP are deliberately wider than the web version's 2px/1px -
// ported verbatim at first, that produced 100+ bars even on a phone-width
// screen, and animating that many Animated.Values every frame (each
// .setValue() on a height/bottom-interpolated value crosses the JS/native
// bridge and forces a layout pass - unlike web's direct, cheap DOM style
// write) is what made the meter "very very slow." MAX_BARS is an explicit
// ceiling regardless of width, since a tablet's wider panel (contentMax up to
// 760, see responsive.ts) would otherwise still produce a lot of bars.
const BAR_WIDTH = 8, BAR_GAP = 3;
const MAX_BARS = 32;
const EASE = 0.35;
const PEAK_FALL_PER_FRAME = 0.8;
const VU_TIMEOUT_MS = 1200;

export default function VuMeter({ c, m }: Props) {
  const s = useMemo(() => makeStyles(c, m), [c, m]);
  const [barCount, setBarCount] = useState(16);
  const [showDisc, setShowDisc] = useState(true);

  // Plain-number and Animated.Value arrays all live in refs, resized IN PLACE
  // whenever barCount changes (synchronously, inside onLayout below) rather
  // than recreated via useMemo keyed on barCount or resized in a useEffect
  // (a useEffect-based resize runs one render too late - the render that
  // picks up a new, larger barCount would index past the still-old-sized
  // arrays). This also lets tick()/onLevels() - and the SSE connection that
  // calls them - never need barCount in a dependency array: they always read
  // the current arrays through the ref, so nothing ever goes stale and the
  // long-lived SSE effect can run exactly once (plus real AppState
  // transitions) instead of reconnecting on every layout pass. barCount
  // (state) still drives how many <View>s render; the ref arrays supply the
  // actual Animated.Value instances by index.
  const targets = useRef<number[]>(new Array(16).fill(0));
  const shown = useRef<number[]>(new Array(16).fill(0));
  const peak = useRef<number[]>(new Array(16).fill(0));
  const fillAnimsRef = useRef<Animated.Value[]>(Array.from({ length: 16 }, () => new Animated.Value(0)));
  const peakAnimsRef = useRef<Animated.Value[]>(Array.from({ length: 16 }, () => new Animated.Value(0)));
  const lastVuFrameAt = useRef(0);
  const rafId = useRef<number | null>(null);
  // Mirrors barCount, but read/written synchronously (state updates don't
  // apply until the next render) - lets onLayout below resize the ref arrays
  // in the SAME tick it changes barCount, instead of a render running with
  // the new (larger) barCount against still-old-sized ref arrays.
  const barCountRef = useRef(barCount);

  const onLayout = (e: { nativeEvent: { layout: { width: number } } }) => {
    const width = e.nativeEvent.layout.width;
    if (width <= 0) return;
    const count = Math.min(MAX_BARS, Math.max(16, Math.floor((width + BAR_GAP) / (BAR_WIDTH + BAR_GAP))));
    if (count === barCountRef.current) return;
    barCountRef.current = count;
    targets.current = new Array(count).fill(0);
    shown.current = new Array(count).fill(0);
    peak.current = new Array(count).fill(0);
    fillAnimsRef.current = Array.from({ length: count }, () => new Animated.Value(0));
    peakAnimsRef.current = Array.from({ length: count }, () => new Animated.Value(0));
    setBarCount(count);
  };

  const tick = () => {
    let moving = false;
    const n = targets.current.length;
    for (let i = 0; i < n; i++) {
      const diff = targets.current[i] - shown.current[i];
      if (Math.abs(diff) > 0.5) { shown.current[i] += diff * EASE; moving = true; }
      else { shown.current[i] = targets.current[i]; }
      fillAnimsRef.current[i]?.setValue(shown.current[i]);

      if (targets.current[i] >= peak.current[i]) { peak.current[i] = targets.current[i]; }
      else { peak.current[i] = Math.max(0, peak.current[i] - PEAK_FALL_PER_FRAME); }
      peakAnimsRef.current[i]?.setValue(peak.current[i]);
      if (peak.current[i] > 0) moving = true;
    }
    rafId.current = moving ? requestAnimationFrame(tick) : null;
  };

  const onLevels = (levels: number[]) => {
    const n = targets.current.length;
    const lastBand = levels.length - 1;
    for (let i = 0; i < n; i++) {
      const pos = (i * lastBand) / (n - 1);
      const lo = Math.floor(pos), hi = Math.min(lastBand, lo + 1), frac = pos - lo;
      const src = levels[lo] * (1 - frac) + levels[hi] * frac;
      targets.current[i] = Math.max(0, Math.min(100, (src / 63) * 100));
    }
    if (rafId.current === null) rafId.current = requestAnimationFrame(tick);
  };

  // SSE connection, AppState-aware (only open while foregrounded) - mirrors
  // the existing 1s-poll's own AppState guard in App.tsx. No barCount
  // dependency - see the big comment above the refs for why that's safe now.
  useEffect(() => {
    let es: EventSource<'message'> | null = null;
    const open = () => {
      if (es) return;
      es = new EventSource(api.getBase() + '/events');
      es.addEventListener('message', (event) => {
        if (!event.data) return;
        try {
          const d = JSON.parse(event.data);
          if (d.type === 'vu' && Array.isArray(d.levels)) {
            lastVuFrameAt.current = Date.now();
            onLevels(d.levels);
          }
        } catch {
          /* ignore malformed frame */
        }
      });
    };
    const close = () => {
      es?.close();
      es = null;
      if (rafId.current !== null) { cancelAnimationFrame(rafId.current); rafId.current = null; }
    };
    if (AppState.currentState === 'active') open();
    const sub = AppState.addEventListener('change', (st) => (st === 'active' ? open() : close()));
    return () => { close(); sub.remove(); };
  }, []);

  // Nothing re-checks the clock once frames stop arriving (paused, a host with
  // no audio capture) - this backstop is what actually notices and flips to
  // the spinning-disc fallback, same role as app.js's setInterval(updateVuSlot).
  useEffect(() => {
    const id = setInterval(() => {
      setShowDisc(Date.now() - lastVuFrameAt.current >= VU_TIMEOUT_MS);
    }, 300);
    return () => clearInterval(id);
  }, []);

  const spin = useRef(new Animated.Value(0)).current;
  useEffect(() => {
    const loop = Animated.loop(
      Animated.timing(spin, { toValue: 1, duration: 2400, easing: Easing.linear, useNativeDriver: true })
    );
    loop.start();
    return () => loop.stop();
  }, [spin]);

  if (showDisc) {
    const rotate = spin.interpolate({ inputRange: [0, 1], outputRange: ['0deg', '360deg'] });
    return (
      <View style={s.bars}>
        <Animated.View style={[s.disc, { transform: [{ rotate }] }]} />
      </View>
    );
  }

  // barCount (state) drives how many columns render; the actual Animated.Value
  // instances come from the refs, which onLayout above keeps in sync.
  return (
    <View style={s.bars} onLayout={onLayout}>
      {Array.from({ length: barCount }).map((_, i) => (
        <View key={i} style={s.col}>
          <Animated.View
            style={[s.fill, { height: fillAnimsRef.current[i].interpolate({ inputRange: [0, 100], outputRange: ['0%', '100%'] }) }]}
          />
          <Animated.View
            style={[s.peak, { bottom: peakAnimsRef.current[i].interpolate({ inputRange: [0, 100], outputRange: ['0%', '100%'] }) }]}
          />
        </View>
      ))}
    </View>
  );
}

function makeStyles(c: Palette, m: Metrics) {
  const { sp } = m;
  // BAR_WIDTH/BAR_GAP stay fixed, unscaled pixels (not run through sp()) - the
  // onLayout bar-count math below needs these to match the actually-rendered
  // size exactly, same reason the web version keeps its own bars at a fixed
  // 2px/1px regardless of window size rather than scaling them.
  return StyleSheet.create({
    bars: {
      flexDirection: 'row',
      alignItems: 'stretch',
      justifyContent: 'center',
      gap: BAR_GAP,
      height: sp(44),
    },
    col: { position: 'relative', width: BAR_WIDTH, flexGrow: 0, flexShrink: 0 },
    fill: {
      position: 'absolute', left: 0, right: 0, bottom: 0,
      backgroundColor: c.accent,
      shadowColor: c.accent, shadowOffset: { width: 0, height: 0 }, shadowOpacity: 0.9, shadowRadius: 4,
    },
    peak: {
      position: 'absolute', left: 0, right: 0, height: 2,
      backgroundColor: c.accent,
      shadowColor: c.accent, shadowOffset: { width: 0, height: 0 }, shadowOpacity: 0.9, shadowRadius: 4,
    },
    disc: {
      width: sp(28), height: sp(28), borderRadius: sp(14),
      borderWidth: 2, borderColor: c.accent,
      alignSelf: 'center',
      shadowColor: c.accent, shadowOffset: { width: 0, height: 0 }, shadowOpacity: 0.9, shadowRadius: 4,
    },
  });
}
