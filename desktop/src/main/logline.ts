/**
 * A line of `genesis serve` output for the window's log, or null to hide it.
 *
 * `genesis serve` prints a QR code and the pairing link for phones: the
 * window has no use for them, and the key has no business in a log.
 * 2026-10-12: since 2026-10-09 the link is `genesisremote://pair#u=…&k=<key>&n=…`
 * and the old `#k=` pattern missed it — the key showed in the log drawer.
 */
const QR_LINE = /^[\s█▀▄]+$/;

export function logLine(raw: string): string | null {
  if (QR_LINE.test(raw)) return null;
  return raw.replace(/([#&?]k=)[A-Za-z0-9_%-]+/g, '$1•••');
}
