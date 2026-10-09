import { redirectFor } from '../lib/protocol';

/**
 * Every link that opens the app passes here first (Expo Router, SDK 57: the
 * raw URL, fragment included). A pairing link goes to /pair WITHOUT the key in
 * the path; the pair screen asks before it pairs — any web page can open a
 * genesisremote:// link, and silently re-pairing the phone to someone else's
 * computer would hand them everything typed next.
 */
export function redirectSystemPath({ path }: { path: string; initial: boolean }): string {
  return redirectFor(path);
}
