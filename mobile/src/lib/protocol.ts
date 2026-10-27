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

// Одит 2026-10-27: тези четири минаваха знак по знак с `indexOf`, `number[]`
// и `out +=` на всеки знак. Отговорът на `speak` няма таван като `events`
// (20 000 знака реч ≈ 7 MB mp3 → 13.4 M знака плик), а четири снимки за
// `send` са същия ред. Измерено под Node (V8 с JIT) върху 9.6 MB base64 в
// JSON: кодиране + декодиране 3.6 s → 0.5 s; тестът в tests/interop.test.ts
// (запечатване + отваряне) — 6.3 s → 0.9 s. Hermes без JIT е няколко пъти
// по-бавен, и екранът стоеше. Таблица вместо indexOf, байтовете в Uint8Array,
// низът — на парчета по 8192 знака от обикновен масив (String.fromCharCode
// .apply с обикновен масив работи навсякъде, и в Hermes).
const B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';
const B64_CODES = Array.from(B64, (ch) => ch.charCodeAt(0));
const B64_VALUE = (() => {
  const table = new Int16Array(128).fill(-1);
  for (let i = 0; i < 64; i++) table[B64.charCodeAt(i)] = i;
  table[43] = 62;   // '+' — приема се и обикновен base64
  table[47] = 63;   // '/'
  return table;
})();
const CHUNK = 8192;

/** Събира UTF-16 кодове и ги прави на низ на парчета: един apply с милиони
 *  аргумента гърми (RangeError), а `out +=` на всеки знак е бавно. */
class CodeBuffer {
  private codes: number[] = [];
  private parts: string[] = [];

  push(code: number): void {
    this.codes.push(code);
    if (this.codes.length >= CHUNK) this.flush();
  }

  text(): string {
    this.flush();
    return this.parts.join('');
  }

  private flush(): void {
    if (!this.codes.length) return;
    this.parts.push(String.fromCharCode.apply(null, this.codes));
    this.codes = [];
  }
}

export function b64urlEncode(bytes: Uint8Array): string {
  const len = bytes.length;
  const out = new CodeBuffer();
  for (let i = 0; i < len; i += 3) {
    const n = (bytes[i] << 16) | ((i + 1 < len ? bytes[i + 1] : 0) << 8) | (i + 2 < len ? bytes[i + 2] : 0);
    out.push(B64_CODES[(n >> 18) & 63]);
    out.push(B64_CODES[(n >> 12) & 63]);
    if (i + 1 < len) out.push(B64_CODES[(n >> 6) & 63]);
    if (i + 2 < len) out.push(B64_CODES[n & 63]);
  }
  return out.text();
}

export function b64urlDecode(text: string): Uint8Array {
  let end = text.length;
  while (end > 0 && text.charCodeAt(end - 1) === 61) end--;   // '=' в края
  const out = new Uint8Array(Math.floor((end * 6) / 8));
  let buffer = 0;
  let bits = 0;
  let o = 0;
  for (let i = 0; i < end; i++) {
    const code = text.charCodeAt(i);
    const v = code < 128 ? B64_VALUE[code] : -1;
    if (v < 0) throw new Error('bad base64');
    buffer = ((buffer << 6) | v) & 0xffffff;
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      out[o++] = (buffer >> bits) & 255;
    }
  }
  return out;
}

// Own UTF-8: TextDecoder is not guaranteed on every React Native runtime.
export function utf8Encode(text: string): Uint8Array {
  // Най-много 3 байта на UTF-16 единица (двойката surrogates — 4 за 2).
  const out = new Uint8Array(text.length * 3);
  let o = 0;
  for (let i = 0; i < text.length; i++) {
    let c = text.charCodeAt(i);
    if (c >= 0xd800 && c <= 0xdbff && i + 1 < text.length) {
      const low = text.charCodeAt(i + 1);
      if (low >= 0xdc00 && low <= 0xdfff) {
        c = 0x10000 + ((c - 0xd800) << 10) + (low - 0xdc00);
        i++;
      }
    }
    if (c < 0x80) out[o++] = c;
    else if (c < 0x800) {
      out[o++] = 0xc0 | (c >> 6);
      out[o++] = 0x80 | (c & 63);
    } else if (c < 0x10000) {
      out[o++] = 0xe0 | (c >> 12);
      out[o++] = 0x80 | ((c >> 6) & 63);
      out[o++] = 0x80 | (c & 63);
    } else {
      out[o++] = 0xf0 | (c >> 18);
      out[o++] = 0x80 | ((c >> 12) & 63);
      out[o++] = 0x80 | ((c >> 6) & 63);
      out[o++] = 0x80 | (c & 63);
    }
  }
  return out.slice(0, o);
}

export function utf8Decode(bytes: Uint8Array): string {
  const out = new CodeBuffer();
  for (let i = 0; i < bytes.length; ) {
    const b = bytes[i];
    let c: number;
    let extra: number;
    if (b < 0x80) { out.push(b); i++; continue; }
    else if (b >= 0xf0) { c = b & 7; extra = 3; }
    else if (b >= 0xe0) { c = b & 15; extra = 2; }
    else if (b >= 0xc0) { c = b & 31; extra = 1; }
    else { out.push(0xfffd); i++; continue; }
    if (i + extra >= bytes.length) { out.push(0xfffd); break; }
    for (let k = 1; k <= extra; k++) c = (c << 6) | (bytes[i + k] & 63);
    // Като String.fromCodePoint преди: над U+10FFFF е грешка, не знак.
    if (c > 0x10ffff) throw new RangeError(`Invalid code point ${c}`);
    if (c > 0xffff) {
      c -= 0x10000;
      out.push(0xd800 + (c >> 10));
      out.push(0xdc00 + (c & 0x3ff));
    } else {
      out.push(c);
    }
    i += extra + 1;
  }
  return out.text();
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

// Only scheme://host[:port] — no user@, no backslash, no path or query
// (audit 2026-10-07: `http://pc@evil:1/#k=…` showed "pc" and talked to "evil").
const BASE = /^https?:\/\/(?:[a-z0-9-]+(?:\.[a-z0-9-]+)*|\[[0-9a-f:.]+\])(?::\d{1,5})?$/i;

/** The app's own link scheme (app.json "scheme"). */
export const APP_LINK = 'genesisremote://pair';

/**
 * What the QR code on the computer says (2026-10-09):
 *   genesisremote://pair#u=<http://192.168.1.5:8765>&k=<key>&n=<name>
 * A phone camera hands it to this app, never to a browser: the old
 * `http://…/#k=…` code opened the web build over plain HTTP in the LAN, where
 * anyone on the same Wi-Fi who swapped the page could read the key. That code
 * is now only `genesis serve --web` (the web build), and still accepted here.
 */
export function parsePairingUrl(raw: string): Pairing | null {
  const text = raw.trim();
  let base: string;
  let fragment: string;
  const app = /^genesisremote:\/\/pair\/?#(.+)$/i.exec(text);
  if (app) {
    fragment = app[1];
    const u = readParams(fragment)?.u;
    if (!u || !BASE.test(u)) return null;
    base = u;
  } else {
    const match = /^(https?:\/\/[^/?#\\]*)\/?#(.+)$/i.exec(text);
    if (!match || !BASE.test(match[1])) return null;
    base = match[1];
    fragment = match[2];
  }
  const params = readParams(fragment);
  if (!params?.k) return null;
  let key: Uint8Array;
  try {
    key = b64urlDecode(params.k);
  } catch {
    return null;
  }
  if (key.length !== 32) return null;
  return { base, key: params.k, name: cleanName(params.n) || base };
}

/** Какво се записва след успешна проверка: името, което компютърът казва,
 *  изчистено като това от връзката. То се показва в заглавката и в
 *  предупреждението „Ще замени сдвояването с …“ (одит 2026-10-11: пазеше се
 *  сурово — с U+202E, нови редове и 300 знака). */
export function pairingFrom(pairing: Pairing, status: StatusReply): Pairing {
  return { ...pairing, name: cleanName(status.name) || pairing.name };
}

/** The computer's name as shown: no control or bidi characters, one line, at
 *  most 40 characters — a link could otherwise push the real address off the
 *  "Сдвои?" card with newlines or reverse it with U+202E (audit 2026-10-09). */
export function cleanName(raw: string | undefined): string {
  // Explicit ranges, not \p{…}: control, soft hyphen, bidi marks/overrides/
  // isolates, zero-width, line/paragraph separators, BOM.
  const flat = (raw ?? '')
    .replace(/[\u0000-\u001f\u007f-\u009f\u00ad\u061c\u180e\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  return [...flat].slice(0, 40).join('');
}

function readParams(fragment: string): Record<string, string> | null {
  const params: Record<string, string> = {};
  for (const part of fragment.split('&')) {
    const eq = part.indexOf('=');
    if (eq > 0) {
      try {
        params[part.slice(0, eq)] = decodeURIComponent(part.slice(eq + 1));
      } catch {
        return null;
      }
    }
  }
  return params;
}

// ── a pairing link that opened the app ──────────────────────────────────────
// genesisremote://pair#… waits here for the pair screen — not in the route, so
// the key never enters the router's path or history.

type Listener = () => void;

// 'invalid': a genesisremote://pair link that is not a valid code — the pair
// screen says so instead of opening silently (audit 2026-10-09).
let pending: Pairing | 'invalid' | null = null;
const listeners = new Set<Listener>();

/** A link from the system: a pairing link (valid or not) → kept for the pair
 *  screen, true. Any other link → false. */
export function offerLink(url: string): boolean {
  if (!/^genesisremote:\/\/pair\b/i.test(url.trim())) return false;
  pending = parsePairingUrl(url) ?? 'invalid';
  for (const l of listeners) l();
  return true;
}

export function takeLink(): Pairing | 'invalid' | null {
  const p = pending;
  pending = null;
  return p;
}

export function onLink(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Where a system link goes: a pairing → '/pair' (the key stays out of the
 *  path), anything else unchanged. Used by app/+native-intent.tsx. */
export function redirectFor(path: string): string {
  try {
    return offerLink(path) ? '/pair' : path;
  } catch {
    return path;
  }
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
  /** HTTP кодът, ако е имало такъв (429 спирачка, 413 твърде голямо…). */
  status?: number;
  /** "error" от явния (незапечатан) отговор — 429 "busy": друг голям плик се
   *  чете в момента (2026-10-15), не спирачката срещу налучкване. */
  code?: string;
  constructor(message: string, kind: ErrorKind, status?: number, code?: string) {
    super(message);
    this.kind = kind;
    this.status = status;
    this.code = code;
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
  /** "confirm": what „За тази сесия“ would remember; absent = no such button
   *  (server 2026-10-15; older servers never send it). */
  session_rule?: string;
  /** "confirm_done": allowed for the rest of the session. */
  session?: boolean;
  /** "user": the pictures sent with it — name and size only, never the bytes
   *  (server 2026-10-15). */
  images?: ImageRef[];
};

/** What an event says about a sent picture (vision.image_ref). */
export type ImageRef = { name?: string; mime?: string; bytes?: number; width?: number; height?: number };

export type Todo = { content: string; status: 'pending' | 'in_progress' | 'completed' };

/** What the phone shows all the time, not as an event: plan mode and the
 *  agent's task list (remote_server.phone_state). Older servers omit it. */
export type PhoneState = { plan: boolean; todos: Todo[] };

export type EventsReply = {
  ok: boolean;
  events: GenesisEvent[];
  last: number;
  reset: boolean;
  busy: boolean;
  epoch: string;
  state?: PhoneState;
  /** Отговорът е стигнал тавана на сървъра: питай веднага пак от `last`
   *  (2026-10-11). По-старите сървъри не го пращат. */
  more?: boolean;
  /** Последните приети `mid` на `send` (вълна 5, 2026-10-11): по тях
   *  outbox.seen забравя повторения опит. По-старите сървъри не ги пращат. */
  mids?: string[];
  error?: string;
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
  /** "voice", "images", "commands" … — an older server omits it or the new
   *  ones (2026-10-15): every media button is gated on it (lib/media.ts). */
  features?: string[];
  /** The server's caps for images and envelopes (2026-10-15). */
  limits?: Partial<ServerLimits>;
};

export type ServerLimits = { images: number; images_bytes: number; body: number; large_body: number };

/** One picture for `send`: standard base64 (no data: prefix), JPEG from the
 *  phone. `id` stays on the phone — it tells the outbox which pictures a retry
 *  carries (2026-10-15). */
export type OutgoingImage = { id: string; mime: string; data: string; name: string };

export type SendReply = { ok: boolean; error?: string; name?: string; max?: number; detail?: string };
export type TranscribeReply = { ok: boolean; text?: string; error?: string; detail?: string };
export type SpeakReply = { ok: boolean; mime?: string; data?: string; text?: string; error?: string; detail?: string };

/** Снимка, глас и изговаряне чакат повече от 15 s (2026-10-15): Whisper и
 *  edge-tts са секунди, а голям плик (до 19 MiB) по Wi-Fi — още толкова. */
export const MEDIA_TIMEOUT_MS = 60000;

/** Езикът на диктовката — операторът говори български (2026-10-15). */
export const SPEECH_LANGUAGE = 'bg';

/** Аргументите на `send`: "images" само когато ги има — по-стар сървър вижда
 *  същата заявка като преди, а `id` не напуска телефона (2026-10-15). */
export function sendArgs(text: string, mid?: string, images: readonly OutgoingImage[] = []): Record<string, unknown> {
  const args: Record<string, unknown> = { text };
  if (mid) args.mid = mid;
  if (images.length) args.images = images.map(({ mime, data, name }) => ({ mime, data, name }));
  return args;
}

export function transcribeArgs(mime: string, data: string, language = SPEECH_LANGUAGE): Record<string, unknown> {
  return { mime, data, language };
}

export function speakArgs(text: string): Record<string, unknown> {
  return { text };
}

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
    let data: unknown;
    // Срокът важи и за тялото, не само до заглавките (одит 2026-10-27):
    // fetch() се изпълнява със заглавките, и отговор, спрял насред тялото
    // (Wi-Fi изчезва насред 512 KiB `events`), висеше завинаги — цикълът в
    // useGenesis не опитваше пак. Измерено с Node fetch срещу сървър, който
    // праща заглавките и 12 байта: преди — виси (> 3 s при срок 0.3 s), сега —
    // 'network' след 0.3 s. abort() прекъсва и четенето на тялото.
    try {
      try {
        response = await this.fetcher(`${this.base}/api/v1`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body,
          signal: controller?.signal,
        });
      } catch {
        throw new ProtocolError(`cannot reach ${this.base}`, 'network');
      }
      try {
        data = await response.json();
      } catch {
        if (controller?.signal.aborted) throw new ProtocolError(`no reply from ${this.base} in time`, 'network');
        throw new ProtocolError(`unexpected reply (${response.status})`, 'server');
      }
    } finally {
      clearTimeout(timer);
    }
    if (response.status === 401) {
      const detail = String((data as { detail?: string })?.detail ?? '');
      if (detail.includes('stale')) throw new ProtocolError('clock', 'clock');
      throw new ProtocolError(detail || 'rejected', 'unauthorized');
    }
    // The server's brake after failed attempts from this address — temporary,
    // retry later; it says nothing about our key (unpairing over it lost the pairing).
    // 429 "busy" е друго (2026-10-15): сървърът чете по един голям плик
    // наведнъж (снимки, глас) — това не е „няма връзка“, опитай след малко.
    if (response.status === 429 && (data as { error?: unknown })?.error === 'busy') {
      throw new ProtocolError('another large request is being read', 'server', 429, 'busy');
    }
    if (response.status === 429) throw new ProtocolError('too many failed attempts — wait a minute', 'network', 429);
    if (response.status !== 200) throw new ProtocolError(`server error ${response.status}`, 'server', response.status);
    return open(this.key, data as Envelope, RES_AAD + rid) as T;
  }

  status(): Promise<StatusReply> {
    return this.call<StatusReply>('status');
  }

  async events(after: number, waitSeconds: number): Promise<EventsReply> {
    const reply = await this.call<EventsReply>('events', { after, wait: waitSeconds }, (waitSeconds + 10) * 1000);
    // Грешка в заявката вече идва запечатана, не като 401 (2026-10-11) —
    // отговор без събития не бива да стига до mergeEvents.
    if (!reply.ok || !Array.isArray(reply.events)) {
      throw new ProtocolError(reply.error ?? 'bad events reply', 'server');
    }
    return reply;
  }

  /** `mid` — идентификаторът на съобщението от телефона: повторен опит със
   *  същия след изгубен отговор получава първия отговор, ходът не тръгва
   *  втори път (2026-10-11). */
  send(text: string, mid?: string, images: readonly OutgoingImage[] = []): Promise<SendReply> {
    return this.call('send', sendArgs(text, mid, images), images.length ? MEDIA_TIMEOUT_MS : undefined);
  }

  /** Говор → текст на компютъра (2026-10-15). Текстът НЕ се праща сам —
   *  отива в полето за писане. */
  transcribe(mime: string, data: string, language = SPEECH_LANGUAGE): Promise<TranscribeReply> {
    return this.call('transcribe', transcribeArgs(mime, data, language), MEDIA_TIMEOUT_MS);
  }

  /** Текст → mp3 на компютъра (2026-10-15). */
  speak(text: string): Promise<SpeakReply> {
    return this.call('speak', speakArgs(text), MEDIA_TIMEOUT_MS);
  }

  /** `session` — „За тази сесия“; only sent when true, so an older server
   *  sees the same request as before (2026-10-15). */
  confirm(id: string, allow: boolean, session = false): Promise<{ ok: boolean }> {
    return this.call('confirm', session && allow ? { id, allow, session: true } : { id, allow });
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
