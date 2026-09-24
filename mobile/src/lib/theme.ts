import { Platform, useColorScheme } from 'react-native';

const light = {
  bg: '#f6f5f9',
  surface: '#ffffff',
  surfaceAlt: '#eeecf4',
  text: '#1b1924',
  muted: '#6b6878',
  border: '#dcd9e5',
  accent: '#6d28d9',
  accentText: '#ffffff',
  userBubble: '#6d28d9',
  userText: '#ffffff',
  code: '#f0eef6',
  ok: '#15803d',
  warn: '#b45309',
  danger: '#b91c1c',
};

const dark: typeof light = {
  bg: '#0f0e14',
  surface: '#1a1822',
  surfaceAlt: '#24212f',
  text: '#ecebf2',
  muted: '#9b98a8',
  border: '#2f2c3b',
  accent: '#a78bfa',
  accentText: '#140f22',
  userBubble: '#5b21b6',
  userText: '#ffffff',
  code: '#12111a',
  ok: '#4ade80',
  warn: '#fbbf24',
  danger: '#f87171',
};

export type Theme = typeof light;

export function useTheme(): Theme {
  return useColorScheme() === 'dark' ? dark : light;
}

export const mono = {
  fontFamily: Platform.select({ ios: 'Menlo', android: 'monospace', default: 'ui-monospace, Menlo, Consolas, monospace' }),
};
