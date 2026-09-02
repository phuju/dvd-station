// Device-adaptive sizing. Every visual value in App.tsx goes through ms() (type)
// or sp() (spacing) so the layout scales to phone / large phone / tablet and
// re-flows live on rotation (App.tsx feeds it useWindowDimensions()).

export type Metrics = {
  width: number;
  height: number;
  wide: boolean; // tablet-class shortest side → 4-col capability strip, wider gutter
  gutter: number; // page horizontal padding
  contentMax: number; // centred column cap (mirrors the web's max-width)
  ms: (size: number) => number; // font scale (dampened toward the base size)
  sp: (size: number) => number; // spacing scale
};

export function makeMetrics(width: number, height: number): Metrics {
  // Scale off the SHORTEST side so rotating a phone doesn't balloon the type.
  const f = Math.max(0.9, Math.min(1.5, Math.min(width, height) / 375));
  const ms = (size: number) => Math.round(size * (1 + (f - 1) * 0.6));
  const sp = (size: number) => Math.round(size * f);
  const wide = Math.min(width, height) >= 600;
  return {
    width,
    height,
    wide,
    gutter: wide ? sp(28) : sp(16),
    contentMax: wide ? 760 : 560,
    ms,
    sp,
  };
}
