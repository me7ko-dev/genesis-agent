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
import { mergeEvents, toChatItems } from '../src/lib/chat.ts';
import { errorText, MAX_MESSAGE, replyErrorText, trimDraft } from '../src/lib/messages.ts';
import { Outbox } from '../src/lib/outbox.ts';
import {
  GenesisClient, ProtocolError, b64urlDecode, b64urlEncode, cleanName, onLink, pairingFrom, parsePairingUrl, redirectFor,
  takeLink, utf8Decode, utf8Encode, type FetchLike, type GenesisEvent, type StatusReply,
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

// ── одит 2026-10-11 ─────────────────────────────────────────────────────────

async function idle(client: GenesisClient) {
  for (let i = 0; i < 200; i++) {
    if (!(await client.status()).busy) return;
    await new Promise((r) => setTimeout(r, 50));
  }
  throw new Error('the turn never finished');
}

/** Какво прави useGenesis: допитване, сливане, и веднага пак, докато има "more". */
async function catchUp(client: GenesisClient, phone: { events: GenesisEvent[]; after: number }) {
  let pages = 0;
  for (;;) {
    const reply = await client.events(phone.after, 0);
    pages += 1;
    phone.events = mergeEvents(phone.events, reply.events, reply.reset);
    phone.after = reply.last;
    if (!reply.more) return pages;
  }
}

test('a phone that slept through a long run gets the gap and keeps its conversation', async () => {
  const { info, stop } = await startServer();
  try {
    const client = new GenesisClient(parsePairingUrl(info.url)!, random);
    const phone = { events: [] as GenesisEvent[], after: 0 };
    assert.equal((await client.send('здравей')).ok, true);
    await idle(client);
    await catchUp(client, phone);
    const caughtUp = phone.after;
    // Телефонът е заключен: агентът прави 200 инструмента.
    assert.equal((await client.send('много')).ok, true);
    await idle(client);
    const pages = await catchUp(client, phone);
    assert.ok(pages > 1, `pages=${pages}`);
    const seqs = phone.events.map((e) => e.seq);
    assert.deepEqual(seqs, Array.from({ length: seqs.length }, (_, i) => i + 1), 'без дупка');
    assert.ok(phone.after > caughtUp + 200);
    const items = toChatItems(phone.events);
    assert.deepEqual(items.filter((i) => i.kind === 'user').map((i) => (i as { text: string }).text), ['здравей', 'много']);
    assert.equal(items.filter((i) => i.kind === 'tool').length, 201);
  } finally {
    stop();
  }
});

test('a double tap or a retry after a lost reply runs the command once', async () => {
  const { info, stop } = await startServer();
  try {
    const pairing = parsePairingUrl(info.url)!;
    const client = new GenesisClient(pairing, random);
    let n = 0;
    const outbox = new Outbox(() => `mid-${Date.now()}-${++n}`);
    const deliver = (t: string, mid: string) => client.send(t, mid);
    // Две докосвания: второто не стига до компютъра.
    const [a, b] = await Promise.all([outbox.send('/plan', deliver), outbox.send('/plan', deliver)]);
    assert.deepEqual([a, b], [{ ok: true }, null]);
    await idle(client);
    assert.equal((await client.events(0, 0)).state?.plan, true, '/plan не е изключен от второ докосване');

    // Заявката стига, отговорът се губи; повторният опит не пуска /todos пак.
    const lossy: FetchLike = async (url, init) => {
      await fetch(url, init);
      throw new Error('LAN dropped');
    };
    const lossyClient = new GenesisClient(pairing, random, lossy);
    await assert.rejects(outbox.send('/todos', (t, mid) => lossyClient.send(t, mid)),
      (e: unknown) => e instanceof ProtocolError && e.kind === 'network');
    await idle(client);
    assert.deepEqual(await outbox.send('/todos', deliver), { ok: true });
    await idle(client);
    const users = (await client.events(0, 0)).events.filter((e) => e.type === 'user').map((e) => e.text);
    assert.deepEqual(users, ['/plan', '/todos']);
  } finally {
    stop();
  }
});

test('a request error with the right key is not "re-pair", and does not lock the phone out', async () => {
  const { info, stop } = await startServer();
  try {
    const client = new GenesisClient(parsePairingUrl(info.url)!, random);
    assert.deepEqual(await client.call('newer_app_op'), { ok: false, error: 'unknown_op' });
    assert.equal('\x1c'.trim(), '\x1c', 'JS trim() го пази…');
    assert.equal(trimDraft('\x1c'), '', '…а приложението вече не');
    const empty = await client.send('\x1c');
    assert.deepEqual(empty, { ok: false, error: 'empty' });
    assert.match(replyErrorText(empty.error), /празно/);
    for (let i = 0; i < 35; i++) await client.call('newer_app_op');
    assert.equal((await client.status()).ok, true);
  } finally {
    stop();
  }
});

test('a web page hammering the same address does not lock out the paired phone', async () => {
  const { info, stop } = await startServer();
  try {
    const pairing = parsePairingUrl(info.url)!;
    const client = new GenesisClient(pairing, random);
    // „Обикновени“ cross-origin POST-ове: text/plain, без preflight.
    for (let i = 0; i < 35; i++) {
      const r = await fetch(`${pairing.base}/api/v1`, { method: 'POST', headers: { 'Content-Type': 'text/plain' }, body: 'x' });
      await r.arrayBuffer();
      if (i === 34) assert.equal(r.status, 429, 'провалите пак се спират');
    }
    assert.equal((await client.status()).ok, true);
  } finally {
    stop();
  }
});

test('a long paste is refused in Bulgarian, and the app limit is the server limit', async () => {
  const { info, stop } = await startServer();
  try {
    const client = new GenesisClient(parsePairingUrl(info.url)!, random);
    const over = await client.send('я'.repeat(MAX_MESSAGE + 1));
    assert.deepEqual(over, { ok: false, error: 'too_long' });
    assert.match(replyErrorText(over.error), /твърде дълго/);
    const huge = await client.send('я'.repeat(100_000)).catch((e: unknown) => e);
    assert.ok(huge instanceof ProtocolError && huge.status === 413);
    assert.match(errorText(huge), /твърде голямо/);
    assert.deepEqual(await client.send('я'.repeat(MAX_MESSAGE)), { ok: true });
    await idle(client);
  } finally {
    stop();
  }
});

test('the computer name from the server is cleaned before it is stored', () => {
  const pairing = parsePairingUrl('genesisremote://pair#u=http%3A%2F%2F192.168.1.5%3A8765&k=AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8&n=pc')!;
  const raw = 'моят-лаптоп‮\n\n\n' + 'x'.repeat(300);
  const status = { ok: true, app: 'genesis', name: raw, version: '1', key_id: '', busy: false } as StatusReply;
  const stored = pairingFrom(pairing, status);
  assert.equal(stored.name, cleanName(raw));
  assert.ok(!/[‮\n]/.test(stored.name) && [...stored.name].length <= 40);
  assert.equal(pairingFrom(pairing, { ...status, name: '‮\n' }).name, 'pc', 'празно след чистене → името от връзката');
  const src = readFileSync(new URL('../src/app/pair.tsx', import.meta.url), 'utf8');
  assert.match(src, /await pair\(pairingFrom\(pairing, status\)\)/);
});

function range(n: number): number[] {
  return Array.from({ length: n }, (_, i) => i);
}
