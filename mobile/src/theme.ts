import { Platform } from 'react-native';

// Ported 1:1 from src/static/style.css :root tokens.
export type Palette = {
  paper: string;
  paperDeep: string;
  ink: string;
  muted: string;
  line: string;
  softLine: string;
  surface: string;
  accent: string;
  accentText: string;
  online: string;
  offline: string;
};

export const LIGHT: Palette = {
  paper: '#f0ede4',
  paperDeep: '#e3dfd4',
  ink: '#1a1a1a',
  muted: '#6f6a60',
  line: '#b9b4a9',
  softLine: 'rgba(26, 26, 26, 0.15)',
  surface: 'rgba(250, 248, 240, 0.7)',
  accent: '#bc7355', // terracotta — constant across themes
  accentText: '#1a1a1a',
  online: '#287346',
  offline: '#9d4035',
};

export const DARK: Palette = {
  paper: '#11213a',
  paperDeep: '#1a3150',
  ink: '#f0ede4',
  muted: '#b8c2cf',
  line: '#8fa0b8',
  softLine: 'rgba(240, 237, 228, 0.2)',
  surface: 'rgba(24, 45, 73, 0.82)',
  accent: '#bc7355',
  accentText: '#1a1a1a',
  online: '#287346',
  offline: '#9d4035',
};

// style.css uses "JetBrains Mono" for body and Impact/Haettenschweiler for
// headings. Neither ships with RN, so approximate with the platform faces.
export const MONO = Platform.select({ ios: 'Menlo', android: 'monospace', default: 'monospace' })!;
export const DISPLAY = Platform.select({
  ios: 'Impact',
  android: 'sans-serif-condensed',
  default: 'System',
})!;
