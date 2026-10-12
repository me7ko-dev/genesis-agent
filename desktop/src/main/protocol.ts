/**
 * The desktop side of `genesis serve` (genesis_agent/remote_server.py) —
 * the same protocol v1 as the phone (mobile/src/lib/protocol.ts).
 *
 * ChaCha20-Poly1305 comes from @noble, not node:crypto: Electron's Node is
 * built on BoringSSL, whose createCipheriv has no 'chacha20-poly1305'.
 *
 * The key never reaches the window: only the main process holds it.
 */
import { chacha20poly1305 } from '@noble/ciphers/chacha.js';
import { createHash, randomBytes } from 'node:crypto';

export const PROTOCOL = 1;
const REQ_AAD = Buffer.from('genesis/1/req');
const RES_AAD = 'genesis/1/res:';

export type Envelope = { v: number; n: string; c: string };

export function b64e(raw: Buffer): string {
  return raw.toString('base64url');
}

export function b64d(text: string): Buffer {
  return Buffer.from(text, 'base64url');
}

/** sha256(key)[:16 hex] — what /api/hello announces as key_id. */
export function keyId(key: Buffer): string {
  return createHash('sha256').update(key).digest('hex').slice(0, 16);
}

export function seal(key: Buffer, payload: unknown, aad: Buffer): Envelope {
  const nonce = randomBytes(12);
  const plain = Buffer.from(JSON.stringify(payload), 'utf8');
  const body = chacha20poly1305(key, nonce, aad).encrypt(plain);
  return { v: PROTOCOL, n: b64e(nonce), c: b64e(Buffer.from(body)) };
}

export function open(key: Buffer, envelope: Envelope, aad: Buffer): unknown {
  if (!envelope || envelope.v !== PROTOCOL) throw new ProtocolError('unsupported envelope', 'protocol');
  try {
    const plain = chacha20poly1305(key, b64d(envelope.n), aad).decrypt(b64d(envelope.c));
    return JSON.parse(Buffer.from(plain).toString('utf8'));
  } catch {
    throw new ProtocolError('the reply could not be verified', 'protocol');
  }
}

export type ErrorKind = 'network' | 'unauthorized' | 'protocol' | 'server';

export class ProtocolError extends Error {
  kind: ErrorKind;
  constructor(message: string, kind: ErrorKind) {
    super(message);
    this.kind = kind;
  }
}

export class GenesisClient {
  readonly base: string;
  private key: Buffer;

  constructor(base: string, key: Buffer) {
    this.base = base;
    this.key = key;
  }

  async hello(timeoutMs = 2000): Promise<{ app: string; key_id: string; version?: string }> {
    const res = await fetch(`${this.base}/api/hello`, { signal: AbortSignal.timeout(timeoutMs) });
    return (await res.json()) as { app: string; key_id: string; version?: string };
  }

  async call<T>(op: string, args: Record<string, unknown> = {}, timeoutMs = 15000): Promise<T> {
    const rid = randomBytes(8).toString('hex');
    const envelope = seal(this.key, { ts: Date.now(), rid, op, ...args }, REQ_AAD);
    let response: Response;
    try {
      response = await fetch(`${this.base}/api/v1`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(envelope),
        signal: AbortSignal.timeout(timeoutMs),
      });
    } catch {
      throw new ProtocolError(`cannot reach ${this.base}`, 'network');
    }
    let data: unknown;
    try {
      data = await response.json();
    } catch {
      throw new ProtocolError(`unexpected reply (${response.status})`, 'server');
    }
    if (response.status === 401 || response.status === 429) {
      throw new ProtocolError(String((data as { detail?: string })?.detail ?? 'rejected'), 'unauthorized');
    }
    if (response.status !== 200) throw new ProtocolError(`server error ${response.status}`, 'server');
    return open(this.key, data as Envelope, Buffer.from(RES_AAD + rid)) as T;
  }
}
