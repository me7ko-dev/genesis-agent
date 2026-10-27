// Одит 2026-10-27, второ: сканирането след неуспешно сдвояване, срокът на
// транскрипцията, грешка на заявка при живи допитвания, скъсано тяло.
// Всеки тест падаше без поправката си (пуснато срещу стария код).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
import { once } from 'node:events';
import { readFileSync } from 'node:fs';
import { createServer } from 'node:http';
import type { Socket } from 'node:net';

import { losesConnection } from '../src/lib/busy.ts';
import { errorText } from '../src/lib/messages.ts';
import {
  b64urlEncode, GenesisClient, MEDIA_TIMEOUT_MS, ProtocolError, TRANSCRIBE_TIMEOUT_MS,
} from '../src/lib/protocol.ts';
import { RESCAN_AFTER_FAIL_MS, ScanGate } from '../src/lib/scanGate.ts';

const random = (n: number) => new Uint8Array(randomBytes(n));

test('a failed QR code still in view is retried at most every few seconds', () => {
  // expo-camera 57 подава същия код на всеки 500 ms; сдвояването пада (100 ms).
  const gate = new ScanGate();
  let tries = 0;
  for (let now = 0; now < 60_000; now += 500) {
    if (!gate.allows('genesisremote://pair#u=x&k=y', now)) continue;
    tries++;
    gate.failedWith('genesisremote://pair#u=x&k=y', now + 100);
  }
  assert.ok(tries <= 12, `${tries} tries a minute`);       // спирачката на сървъра е над 30
  assert.ok(tries >= 10, 'the same code still gets retried on its own');
  // Друг код минава веднага; успехът забравя неуспеха.
  assert.ok(gate.allows('genesisremote://pair#u=z&k=w', 60_000));
  gate.succeeded();
  assert.ok(gate.allows('genesisremote://pair#u=x&k=y', 60_000));
  assert.equal(RESCAN_AFTER_FAIL_MS, 5000);
});

test('transcription waits longer than the computer waits for Whisper', () => {
  const voice = readFileSync(new URL('../../genesis_agent/voice.py', import.meta.url), 'utf8');
  const server = Number(/stt_timeout_s: float = ([\d.]+)/.exec(voice)?.[1]);
  assert.ok(server > 0);
  // + качването на запис до 10 MB по Wi-Fi
  assert.ok(TRANSCRIBE_TIMEOUT_MS >= (server + 20) * 1000, `${TRANSCRIBE_TIMEOUT_MS} ms vs ${server} s`);
  assert.equal(MEDIA_TIMEOUT_MS, 60000);
});

test('a slow voice call while polls are fine does not mark the computer offline', () => {
  const timeout = new ProtocolError('no reply in time', 'network', undefined, 'timeout');
  assert.equal(losesConnection(timeout, true), false);
  assert.equal(losesConnection(timeout, false), true);
  assert.equal(losesConnection(new ProtocolError('server error 500', 'server'), false), false);
  assert.equal(errorText(timeout), 'Компютърът не отговори навреме — опитай пак.');
});

async function server(handler: (res: import('node:http').ServerResponse, socket: Socket) => void) {
  const sockets = new Set<Socket>();
  const srv = createServer((req, res) => handler(res, req.socket));
  srv.on('connection', (s) => sockets.add(s));
  srv.listen(0, '127.0.0.1');
  await once(srv, 'listening');
  const { port } = srv.address() as { port: number };
  const client = new GenesisClient({ base: `http://127.0.0.1:${port}`, key: b64urlEncode(new Uint8Array(32)), name: 'x' }, random);
  return { client, close: () => { for (const s of sockets) s.destroy(); srv.close(); } };
}

test('a reply that stalls says "timeout", not "cannot reach"', async () => {
  const { client, close } = await server((res) => {
    res.writeHead(200, { 'Content-Type': 'application/json', 'Content-Length': '1000' });
    res.write('{"v":1,');
  });
  try {
    const e = await client.call('status', {}, 300).then(() => null, (x: unknown) => x);
    assert.ok(e instanceof ProtocolError);
    assert.equal(e.kind, 'network');
    assert.equal(e.code, 'timeout');
  } finally {
    close();
  }
});

test('a connection cut in the middle of the reply is the network, not the server', async () => {
  const { client, close } = await server((res, socket) => {
    res.writeHead(200, { 'Content-Type': 'application/json', 'Content-Length': '1000' });
    res.write('{"v":1,', () => setTimeout(() => socket.destroy(), 50));
  });
  try {
    const e = await client.call('status', {}, 5000).then(() => null, (x: unknown) => x);
    assert.ok(e instanceof ProtocolError);
    assert.equal(e.kind, 'network', String(e.message));
  } finally {
    close();
  }
});
