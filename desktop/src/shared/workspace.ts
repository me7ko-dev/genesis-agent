/**
 * The opened folder is the home folder or a drive root: an installed agent
 * refuses those and works in a folder of its own (genesis_agent/paths.py).
 * 2026-10-12: the window blamed that rule whenever the agent worked elsewhere —
 * also for a Genesis run from its source, which works in its own checkout.
 */
export function guardedFolder(path: string, home: string): boolean {
  const norm = (p: string) => p.replace(/[\\/]+$/, '').replace(/\//g, '\\').toLowerCase();
  const p = norm(path);
  return p === '' || /^[a-z]:$/.test(p) || (home !== '' && p === norm(home));
}
