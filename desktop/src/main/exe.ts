/**
 * Which genesis.exe the window runs, in order: the app's own setting,
 * GENESIS_EXE, then the `genesis` the terminal runs (PATH), and only then the
 * installer's folder.
 *
 * 2026-10-12: `%LOCALAPPDATA%\Programs\Genesis` still held the 24.09 build
 * while the terminal's `genesis` (pipx, `~\.local\bin`) was the 12.10 one —
 * the installer's folder came first, so the window ran the old agent.
 * GENESIS_INSTALL_DIR is no choice of the user's: `scripts/install.ps1` sets
 * it on every install, and it pointed at that same old folder.
 */
import { join, normalize } from 'node:path';

export interface ExeSources {
  /** The app's own setting (`settings.exe`). */
  preferred?: string;
  env: Record<string, string | undefined>;
  home: string;
  /** The lines of `where.exe genesis`: what the terminal runs comes first. */
  onPath: string[];
  /**
   * The window's own executable (`process.execPath`) — never the agent.
   * 2026-10-12, this laptop: the shortcut starts the window in its own folder,
   * `where.exe` looks in the current folder first, and Windows names are
   * case-blind, so `genesis` found the window's `Genesis.exe`. It ran itself
   * as `Genesis.exe serve …`, the second window quit at once (one instance),
   * and the user saw „genesis спря (код 0)“.
   */
  self?: string;
}

export function exeCandidates(s: ExeSources): string[] {
  const local = s.env.LOCALAPPDATA || join(s.home, 'AppData', 'Local');
  const self = s.self && normalize(s.self).toLowerCase();
  const notSelf = (c: string) => !self || normalize(c).toLowerCase() !== self;
  const list = [
    s.preferred,
    s.env.GENESIS_EXE,
    s.onPath.map((l) => l.trim()).find((l) => l.toLowerCase().endsWith('.exe') && notSelf(l)),
    // pipx's folder, for a window started before `pipx ensurepath` reached PATH.
    join(s.env.PIPX_BIN_DIR || join(s.home, '.local', 'bin'), 'genesis.exe'),
    s.env.GENESIS_INSTALL_DIR && join(s.env.GENESIS_INSTALL_DIR, 'genesis.exe'),
    join(local, 'Programs', 'Genesis', 'genesis.exe'),
  ];
  return list.filter((c): c is string => !!c && notSelf(c));
}
