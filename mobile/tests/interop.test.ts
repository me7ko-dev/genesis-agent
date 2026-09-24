/**
 * The phone's protocol code against the real Python server
 * (genesis_agent/remote_server.py via tests/fake_genesis.py).
 *   node --test tests/
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { once } from 'node:events';
import { createInterface } from 'node:readline';
import {
  GenesisClient, ProtocolError, b64urlDecode, b64urlEncode, parsePairingUrl, utf8Decode, utf8Encode,
  type GenesisEvent,
} from '../src/lib/protocol.ts';

const random = (n: number) => new Uint8Array(randomBytes(n));

async function startServer() {
  const python = process.env.PYTHON ?? 'python3';
  const child = spawn(python, [new URL('./fake_genesis.py', import.meta.url).pathname], {
    stdio: ['pipe', 'pipe', 'inherit'],
  });
  const lines = createInterface({ input: child.stdout! });
  const [first] = (await once(lines, 'line')) as [string];
  const info = JSON.parse(first) as { port: number; url: string };
  return { info, stop: () => { child.stdin!.end(); child.kill(); } };
}

async function waitFor(client: GenesisClient, predicate: (e: GenesisEvent) => boolean, after = 0) {
  const seen: GenesisEvent[] = [];
  for (let i = 0; i < 20; i++) {
    const reply = await client.events(after, 5);
    seen.push(...reply.events);
    after = reply.last;
    const hit = seen.find(predicate);
    if (hit) return { hit, seen, after };
  }
  throw new Error('event never came: ' + JSON.stringify(seen));
}

test('base64url and utf-8 agree with Python', () => {
  const bytes = new Uint8Array([0, 1, 2, 250, 251, 252, 253, 254, 255]);
  assert.deepEqual(b64urlDecode(b64urlEncode(bytes)), bytes);
  assert.equal(b64urlEncode(new Uint8Array(range(32))), 'AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8');
  for (const text of ['здравей', '👋 emoji', 'a\u0000b', '']) {
    assert.equal(utf8Decode(utf8Encode(text)), text);
    assert.deepEqual(utf8Encode(text), new Uint8Array(Buffer.from(text, 'utf8')));
  }
});

test('pairing URL: key only from the fragment', () => {
  const p = parsePairingUrl('http://192.168.1.5:8765/#k=AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8&n=my%20pc');
  assert.deepEqual(p, { base: 'http://192.168.1.5:8765', key: 'AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8', name: 'my pc' });
  assert.equal(parsePairingUrl('http://192.168.1.5:8765/?k=AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8'), null);
  assert.equal(parsePairingUrl('http://x:1/#k=short'), null);
  assert.equal(parsePairingUrl('not a url'), null);
});

test('a whole conversation with the real server', async () => {
  const { info, stop } = await startServer();
  try {
    const pairing = parsePairingUrl(info.url)!;
    assert.equal(pairing.name, 'тест-компютър');
    const client = new GenesisClient(pairing, random);

    const status = await client.status();
    assert.equal(status.app, 'genesis');
    assert.equal(status.key_id, client.keyId(), 'key id must match the Python one');
    assert.equal(status.model, 'fake/model');

    assert.deepEqual(await client.send('здравей'), { ok: true });
    const { hit } = await waitFor(client, (e) => e.type === 'assistant');
    assert.equal(hit.text, "Получих: **здравей** ✅\n\n```python\nprint('здравей')\n```");

    // a dangerous command waits for the phone
    const before = (await client.events(0, 0)).last;
    await waitFor(client, (e) => e.type === 'busy' && e.busy === false, before - 1);
    assert.equal((await client.send('опасно')).ok, true);
    const { hit: confirm, after } = await waitFor(client, (e) => e.type === 'confirm', before);
    assert.equal(confirm.operation, 'rm -rf build');
    assert.deepEqual(await client.confirm(confirm.id!, true), { ok: true });
    const { hit: result } = await waitFor(client, (e) => e.type === 'info', after);
    assert.equal(result.text, 'allowed=True');
  } finally {
    stop();
  }
});

test('a wrong key is refused, not misread', async () => {
  const { info, stop } = await startServer();
  try {
    const pairing = parsePairingUrl(info.url)!;
    const wrong = new GenesisClient({ ...pairing, key: b64urlEncode(new Uint8Array(32)) }, random);
    await assert.rejects(wrong.status(), (e: unknown) => e instanceof ProtocolError && e.kind === 'unauthorized');
  } finally {
    stop();
  }
});

test('an unreachable computer is a network error', async () => {
  const client = new GenesisClient({ base: 'http://127.0.0.1:9', key: b64urlEncode(new Uint8Array(32)), name: 'x' }, random);
  await assert.rejects(client.status(), (e: unknown) => e instanceof ProtocolError && e.kind === 'network');
});

function range(n: number): number[] {
  return Array.from({ length: n }, (_, i) => i);
}
