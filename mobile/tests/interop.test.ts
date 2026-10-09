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
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { createInterface } from 'node:readline';
import {
  GenesisClient, ProtocolError, b64urlDecode, b64urlEncode, cleanName, onLink, parsePairingUrl, redirectFor, takeLink,
  utf8Decode, utf8Encode, type GenesisEvent,
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
  // The host shown is the host talked to: no user@, backslash, path or query.
  const k = 'AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8';
  for (const bad of [`http://my-pc@evil.example:8765/#k=${k}`, `http://evil.example\\@my-pc/#k=${k}`,
    `http://my-pc:8765/x#k=${k}`, `http://my-pc:8765/?a=1#k=${k}`]) {
    assert.equal(parsePairingUrl(bad), null, bad);
  }
  assert.equal(parsePairingUrl(`http://[fe80::1]:8765/#k=${k}`)?.base, 'http://[fe80::1]:8765');
  assert.equal(parsePairingUrl(`https://pc.local#k=${k}`)?.base, 'https://pc.local');
});

test('the QR code is an app link; its address must be a plain base', () => {
  const k = 'AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8';
  const p = parsePairingUrl(`genesisremote://pair#u=http%3A%2F%2F192.168.1.5%3A8765&k=${k}&n=my%20pc`);
  assert.deepEqual(p, { base: 'http://192.168.1.5:8765', key: k, name: 'my pc' });
  for (const u of ['http%3A%2F%2Fpc%40evil%3A1', 'http%3A%2F%2Fpc%3A1%2Fx', 'javascript%3Aalert(1)', 'ftp%3A%2F%2Fpc']) {
    assert.equal(parsePairingUrl(`genesisremote://pair#u=${u}&k=${k}`), null, u);
  }
  assert.equal(parsePairingUrl(`genesisremote://pair#k=${k}`), null, 'no address');
  assert.equal(parsePairingUrl(`genesisremote://other#u=http%3A%2F%2Fpc&k=${k}`), null);
});

test('a pairing link reaches the pair screen without the key in the route', () => {
  const k = 'AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8';
  let calls = 0;
  const off = onLink(() => {
    calls += 1;
  });
  try {
    assert.equal(redirectFor(`genesisremote://pair#u=http%3A%2F%2Fpc.local%3A8765&k=${k}&n=pc`), '/pair');
    assert.equal(calls, 1);
    const taken = takeLink();
    assert.ok(taken && taken !== 'invalid');
    assert.equal(taken.key, k);
    assert.equal(taken.base, 'http://pc.local:8765');
    assert.equal(takeLink(), null, 'taken once');
    // Other links go on unchanged and leave nothing behind (a broken pairing
    // link: see the audit test below).
    assert.equal(redirectFor('genesisremote://chat'), 'genesisremote://chat');
    // A plain http link is not taken from the system — only the app's own scheme.
    assert.equal(redirectFor(`http://pc:1/#k=${k}`), `http://pc:1/#k=${k}`);
    assert.equal(takeLink(), null);
  } finally {
    off();
  }
});

// ── audit 2026-10-09 ────────────────────────────────────────────────────────

test('the name on the "Сдвои?" card cannot hide or reverse the address', () => {
  assert.equal(cleanName('моят компютър\n\n\n\n на http://192.168.1.5'), 'моят компютър на http://192.168.1.5');
  assert.equal(cleanName('a‮b​c d'), 'a b c d');
  assert.equal([...cleanName('я'.repeat(500))].length, 40);
  const k = 'AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8';
  const p = parsePairingUrl(`genesisremote://pair#u=http%3A%2F%2Fpc%3A1&k=${k}&n=${encodeURIComponent('x\n\n\ny')}`);
  assert.equal(p?.name, 'x y');
});

test('a broken pairing link still opens the pair screen, which says so', () => {
  assert.equal(redirectFor('genesisremote://pair#u=http%3A%2F%2FPC_1.local%3A1&k=short'), '/pair');
  assert.equal(takeLink(), 'invalid');
  assert.equal(takeLink(), null);
});

test('an IPv6 address from `genesis serve --host` is readable', () => {
  const k = 'AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8';
  const p = parsePairingUrl(`genesisremote://pair#u=http%3A%2F%2F%5Bfd7a%3A%3A1%5D%3A8765&k=${k}`);
  assert.equal(p?.base, 'http://[fd7a::1]:8765');
});

test('leaving the pair screen pops back to the one chat, never stacks a second', () => {
  // The same router expo-router 57 runs: a link pushes /pair over /chat; the
  // old router.replace('/chat') left [chat, chat] with two live poll loops.
  const require = createRequire(import.meta.url);
  const { StackRouter } = require('expo-router/build/react-navigation/routers/StackRouter.js');
  const r = StackRouter({});
  const o = { routeNames: ['index', 'chat', 'pair'], routeParamList: {}, routeGetIdList: {} };
  let s = r.getStateForAction(r.getInitialState(o), { type: 'REPLACE', payload: { name: 'chat' } }, o);
  s = r.getStateForAction(s, { type: 'NAVIGATE', payload: { name: 'pair' } }, o);
  const names = (st: { routes: { name: string }[] }) => st.routes.map((x) => x.name);
  assert.deepEqual(names(r.getStateForAction(s, { type: 'REPLACE', payload: { name: 'chat' } }, o)), ['chat', 'chat']);
  assert.deepEqual(names(r.getStateForAction(s, { type: 'POP_TO', payload: { name: 'chat' } }, o)), ['chat']);
  // …and the pair screen uses dismissTo (POP_TO), not replace.
  const src = readFileSync(new URL('../src/app/pair.tsx', import.meta.url), 'utf8');
  assert.doesNotMatch(src, /router\.replace\('\/chat'\)/);
  assert.match(src, /router\.dismissTo\('\/chat'\)/);
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

test('chat commands and sub-agent progress reach the phone', async () => {
  const { info, stop } = await startServer();
  try {
    const client = new GenesisClient(parsePairingUrl(info.url)!, random);
    assert.equal((await client.events(0, 0)).state?.plan, false);

    assert.equal((await client.send('/plan')).ok, true);
    const { hit: note, after } = await waitFor(client, (e) => e.type === 'info');
    assert.match(note.text ?? '', /Режим план/);
    const idle = await waitFor(client, (e) => e.type === 'busy' && e.busy === false, after - 1);
    // /plan never reached the model: no assistant reply, and plan mode is on.
    assert.equal(idle.seen.some((e) => e.type === 'assistant'), false);
    assert.equal((await client.events(0, 0)).state?.plan, true);

    assert.equal((await client.send('агент')).ok, true);
    const { hit: step } = await waitFor(client, (e) => e.type === 'progress', idle.after);
    assert.equal(step.text, '↳ reviewer: чета diff-а');
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
