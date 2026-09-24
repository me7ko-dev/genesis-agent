/**
 * The phone side of `genesis serve` (genesis_agent/remote_server.py).
 *
 * Every request and response is sealed with ChaCha20-Poly1305 under the
 * 32-byte key that reached this phone only through the QR code. Nothing here
 * depends on React Native, so the same file runs under Node in
 * tests/interop.test.ts against the real Python server.
 */
import { chacha20poly1305 } from '@noble/ciphers/chacha.js';
import { sha256 } from '@noble/hashes/sha2.js';

export const PROTOCOL = 1;
const REQ_AAD = 'genesis/1/req';
const RES_AAD = 'genesis/1/res:';

// ── bytes ───────────────────────────────────────────────────────────────────

const B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';

export function b64urlEncode(bytes: Uint8Array): string {
  let out = '';
  for (let i = 0; i < bytes.length; i += 3) {
    const n = (bytes[i] << 16) | ((bytes[i + 1] ?? 0) << 8) | (bytes[i + 2] ?? 0);
    out += B64[(n >> 18) & 63] + B64[(n >> 12) & 63];
    if (i + 1 < bytes.length) out += B64[(n >> 6) & 63];
    if (i + 2 < bytes.length) out += B64[n & 63];
  }
  return out;
}

export function b64urlDecode(text: string): Uint8Array {
  const clean = text.replace(/=+$/, '').replace(/\+/g, '-').replace(/\//g, '_');
  const out: number[] = [];
  let buffer = 0;
  let bits = 0;
  for (const ch of clean) {
    const v = B64.indexOf(ch);
    if (v < 0) throw new Error('bad base64');
    buffer = (buffer << 6) | v;
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      out.push((buffer >> bits) & 255);
    }
  }
  return new Uint8Array(out);
}

// Own UTF-8: TextDecoder is not guaranteed on every React Native runtime.
export function utf8Encode(text: string): Uint8Array {
  const out: number[] = [];
  for (const ch of text) {
    let c = ch.codePointAt(0) as number;
    if (c < 0x80) out.push(c);
    else if (c < 0x800) out.push(0xc0 | (c >> 6), 0x80 | (c & 63));
    else if (c < 0x10000) out.push(0xe0 | (c >> 12), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63));
    else {
      c = Math.min(c, 0x10ffff);
      out.push(0xf0 | (c >> 18), 0x80 | ((c >> 12) & 63), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63));
    }
  }
  return new Uint8Array(out);
}

export function utf8Decode(bytes: Uint8Array): string {
  let out = '';
  for (let i = 0; i < bytes.length; ) {
    const b = bytes[i];
    let c: number;
    let extra: number;
    if (b < 0x80) { c = b; extra = 0; }
    else if (b >= 0xf0) { c = b & 7; extra = 3; }
    else if (b >= 0xe0) { c = b & 15; extra = 2; }
    else if (b >= 0xc0) { c = b & 31; extra = 1; }
    else { out += '\ufffd'; i++; continue; }
    if (extra > 0 && i + extra >= bytes.length) { out += '\ufffd'; break; }
    for (let k = 1; k <= extra; k++) c = (c << 6) | (bytes[i + k] & 63);
    out += String.fromCodePoint(c);
    i += extra + 1;
  }
  return out;
}

function hex(bytes: Uint8Array): string {
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

// ── pairing ─────────────────────────────────────────────────────────────────

export type Pairing = {
  /** http://192.168.1.5:8765 — no path, no trailing slash. */
  base: string;
  /** base64url of the 32-byte key. */
  key: string;
  /** The computer's name, for the header. */
  name: string;
};

/**
 * What the QR code on the computer says:
 *   http://192.168.1.5:8765/#k=<key>&n=<name>
 * The key is in the fragment, which a browser never sends over the network.
 */
export function parsePairingUrl(raw: string): Pairing | null {
  const text = raw.trim();
  const match = /^(https?:\/\/[^/#?\s]+)[^#]*#(.+)$/i.exec(text);
  if (!match) return null;
  const params: Record<string, string> = {};
  for (const part of match[2].split('&')) {
    const eq = part.indexOf('=');
    if (eq > 0) {
      try {
        params[part.slice(0, eq)] = decodeURIComponent(part.slice(eq + 1));
      } catch {
        return null;
      }
    }
  }
  if (!params.k) return null;
  let key: Uint8Array;
  try {
    key = b64urlDecode(params.k);
  } catch {
    return null;
  }
  if (key.length !== 32) return null;
  return { base: match[1].replace(/\/+$/, ''), key: params.k, name: params.n || match[1] };
}

// ── the envelope ────────────────────────────────────────────────────────────

export type Envelope = { v: number; n: string; c: string };

export type RandomBytes = (n: number) => Uint8Array;

export function seal(key: Uint8Array, payload: unknown, aad: string, random: RandomBytes): Envelope {
  const nonce = random(12);
  const sealed = chacha20poly1305(key, nonce, utf8Encode(aad)).encrypt(utf8Encode(JSON.stringify(payload)));
  return { v: PROTOCOL, n: b64urlEncode(nonce), c: b64urlEncode(sealed) };
}

export function open(key: Uint8Array, envelope: Envelope, aad: string): unknown {
  if (!envelope || envelope.v !== PROTOCOL) throw new ProtocolError('unsupported envelope', 'protocol');
  let plain: Uint8Array;
  try {
    plain = chacha20poly1305(key, b64urlDecode(envelope.n), utf8Encode(aad)).decrypt(b64urlDecode(envelope.c));
  } catch {
    throw new ProtocolError('the reply could not be verified', 'protocol');
  }
  return JSON.parse(utf8Decode(plain));
}

// ── the client ──────────────────────────────────────────────────────────────

export type ErrorKind = 'network' | 'unauthorized' | 'clock' | 'protocol' | 'server';

export class ProtocolError extends Error {
  kind: ErrorKind;
  constructor(message: string, kind: ErrorKind) {
    super(message);
    this.kind = kind;
  }
}

export type GenesisEvent = {
  seq: number;
  ts: number;
  type: string;
  text?: string;
  name?: string;
  result?: string;
  clipped?: boolean;
  id?: string;
  operation?: string;
  reasons?: string[];
  allow?: boolean;
  note?: string;
  busy?: boolean;
  label?: string;
};

export type EventsReply = {
  ok: boolean;
  events: GenesisEvent[];
  last: number;
  reset: boolean;
  busy: boolean;
  epoch: string;
};

export type StatusReply = {
  ok: boolean;
  app: string;
  name: string;
  version: string;
  key_id: string;
  busy: boolean;
  model?: string;
  workspace?: string;
};

export type FetchLike = (url: string, init: {
  method: string;
  headers: Record<string, string>;
  body: string;
  signal?: AbortSignal;
}) => Promise<{ status: number; json(): Promise<unknown> }>;

export class GenesisClient {
  readonly base: string;
  private key: Uint8Array;
  private random: RandomBytes;
  private fetcher: FetchLike;

  constructor(pairing: Pairing, random: RandomBytes, fetcher?: FetchLike) {
    this.base = pairing.base;
    this.key = b64urlDecode(pairing.key);
    this.random = random;
    this.fetcher = fetcher ?? ((url, init) => fetch(url, init));
  }

  /** Must match what the server announces: guards against pairing with one
   *  computer and talking to another at the same address. */
  keyId(): string {
    return hexPrefix(this.key);
  }

  async call<T>(op: string, args: Record<string, unknown> = {}, timeoutMs = 15000): Promise<T> {
    const rid = hex(this.random(8));
    const payload = { ts: Date.now(), rid, op, ...args };
    const body = JSON.stringify(seal(this.key, payload, REQ_AAD, this.random));
    const controller = typeof AbortController !== 'undefined' ? new AbortController() : undefined;
    const timer = setTimeout(() => controller?.abort(), timeoutMs);
    let response: { status: number; json(): Promise<unknown> };
    try {
      response = await this.fetcher(`${this.base}/api/v1`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body,
        signal: controller?.signal,
      });
    } catch {
      throw new ProtocolError(`cannot reach ${this.base}`, 'network');
    } finally {
      clearTimeout(timer);
    }
    let data: unknown;
    try {
      data = await response.json();
    } catch {
      throw new ProtocolError(`unexpected reply (${response.status})`, 'server');
    }
    if (response.status === 401) {
      const detail = String((data as { detail?: string })?.detail ?? '');
      if (detail.includes('stale')) throw new ProtocolError('clock', 'clock');
      throw new ProtocolError(detail || 'rejected', 'unauthorized');
    }
    if (response.status === 429) throw new ProtocolError('too many failed attempts', 'unauthorized');
    if (response.status !== 200) throw new ProtocolError(`server error ${response.status}`, 'server');
    return open(this.key, data as Envelope, RES_AAD + rid) as T;
  }

  status(): Promise<StatusReply> {
    return this.call<StatusReply>('status');
  }

  events(after: number, waitSeconds: number): Promise<EventsReply> {
    return this.call<EventsReply>('events', { after, wait: waitSeconds }, (waitSeconds + 10) * 1000);
  }

  send(text: string): Promise<{ ok: boolean; error?: string }> {
    return this.call('send', { text });
  }

  confirm(id: string, allow: boolean): Promise<{ ok: boolean }> {
    return this.call('confirm', { id, allow });
  }

  stop(): Promise<{ ok: boolean }> {
    return this.call('stop');
  }

  clear(): Promise<{ ok: boolean; error?: string }> {
    return this.call('clear');
  }
}

/** sha256(key)[:8] as hex — same as remote_server.key_id. */
function hexPrefix(key: Uint8Array): string {
  return hex(sha256(key)).slice(0, 16);
}
