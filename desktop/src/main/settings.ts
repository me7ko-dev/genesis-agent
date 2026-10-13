import { app } from 'electron';
import { readFileSync, renameSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import type { Settings } from '../shared/types';

const MAX_RECENT = 12;
const DEFAULTS: Settings = { recent: [], sidebar: true };

function file(): string {
  return join(app.getPath('userData'), 'settings.json');
}

export function loadSettings(): Settings {
  try {
    return { ...DEFAULTS, ...(JSON.parse(readFileSync(file(), 'utf8')) as Partial<Settings>) };
  } catch {
    return { ...DEFAULTS };
  }
}

export function saveSettings(s: Settings): void {
  const tmp = `${file()}.tmp`;
  writeFileSync(tmp, JSON.stringify(s, null, 2), 'utf8');
  renameSync(tmp, file());
}

/** The folder goes to the top of the list, once. */
export function pushRecent(s: Settings, path: string): Settings {
  const same = (a: string) => a.toLowerCase() === path.toLowerCase();
  return { ...s, lastWorkspace: path, recent: [path, ...s.recent.filter((p) => !same(p))].slice(0, MAX_RECENT) };
}
