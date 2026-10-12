/**
 * The desktop's protocol code (@noble ChaCha20-Poly1305) against the real Python server
 * (genesis_agent/remote_server.py via mobile/tests/fake_genesis.py), plus the
 * pure chat helpers.   npm test
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import { b64d, b64e, GenesisClient, keyId, open, ProtocolError, seal } from '../src/main/protocol.ts';
import { mergeEvents, pendingConfirm, toChatItems, toolHeader, toolLook, turnInfo } from '../src/renderer/lib/chat.ts';
import type { GenesisEvent } from '../src/shared/types.ts';

const FAKE = fileURLToPath(new URL('../../mobile/tests/fake_genesis.py', import.meta.url));
const KEY = Buffer.from(Array.from({ length: 32 }, (_, i) => i));

async function startServer() {
  const python = process.env.PYTHON ?? (process.platform === 'win32' ? 'python' : 'python3');
  const child = spawn(python, [FAKE], { stdio: ['pipe', 'pipe', 'inherit'], env: { ...process.env, PYTHONUTF8: '1' } });
  const lines = createInterface({ input: child.stdout! });
  const [first] = (await once(lines, 'line')) as [string];
  const info = JSON.parse(first) as { port: number };
  return { base: `http://127.0.0.1:${info.port}`, stop: () => { child.stdin!.end(); child.kill(); } };
}

async function waitFor(client: GenesisClient, predicate: (e: GenesisEvent) => boolean, after = 0) {
  const seen: GenesisEvent[] = [];
  for (let i = 0; i < 20; i++) {
    const reply = await client.call<{ events: GenesisEvent[]; last: number }>('events', { after, wait: 5 });
    seen.push(...reply.events);
    after = reply.last;
    const hit = seen.find(predicate);
    if (hit) return { hit, after };
  }
  throw new Error('event never came: ' + JSON.stringify(seen));
}

test('seal/open round trip; a wrong AAD is refused', () => {
  const env = seal(KEY, { text: 'здравей 👋' }, Buffer.from('a'));
  assert.deepEqual(open(KEY, env, Buffer.from('a')), { text: 'здравей 👋' });
  assert.throws(() => open(KEY, env, Buffer.from('b')), ProtocolError);
  assert.equal(b64e(b64d('AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8')), 'AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8');
});

test('a whole conversation with the real server', async () => {
  const { base, stop } = await startServer();
  try {
    const client = new GenesisClient(base, KEY);
    const hello = await client.hello();
    assert.equal(hello.key_id, keyId(KEY), 'key id must match the Python one');

    const status = await client.call<{ app: string; model: string }>('status');
    assert.equal(status.app, 'genesis');
    assert.equal(status.model, 'fake/model');

    assert.deepEqual(await client.call('send', { text: 'здравей' }), { ok: true });
    const { hit } = await waitFor(client, (e) => e.type === 'assistant');
    assert.match(hit.text ?? '', /Получих: \*\*здравей\*\*/);

    const { after } = await waitFor(client, (e) => e.type === 'busy' && e.busy === false);
    assert.equal((await client.call<{ ok: boolean }>('send', { text: 'опасно' })).ok, true);
    const { hit: confirm, after: next } = await waitFor(client, (e) => e.type === 'confirm', after);
    assert.equal(confirm.operation, 'rm -rf build');
    assert.deepEqual(await client.call('confirm', { id: confirm.id, allow: false }), { ok: true });
    const { hit: result } = await waitFor(client, (e) => e.type === 'info', next);
    assert.equal(result.text, 'allowed=False');
  } finally {
    stop();
  }
});

test('a wrong key is refused, not misread', async () => {
  const { base, stop } = await startServer();
  try {
    const wrong = new GenesisClient(base, Buffer.alloc(32));
    await assert.rejects(wrong.call('status'), (e: unknown) => e instanceof ProtocolError && e.kind === 'unauthorized');
  } finally {
    stop();
  }
});

test('an unreachable agent is a network error', async () => {
  const client = new GenesisClient('http://127.0.0.1:9', KEY);
  await assert.rejects(client.call('status', {}, 2000), (e: unknown) => e instanceof ProtocolError && e.kind === 'network');
});

// ── chat helpers ────────────────────────────────────────────────────────────

const ev = (seq: number, type: string, extra: Partial<GenesisEvent> = {}): GenesisEvent => ({ seq, ts: seq * 1000, type, ...extra });

test('events become chat items; a confirmation is updated in place', () => {
  const items = toChatItems([
    ev(1, 'user', { text: 'hi' }),
    ev(2, 'thinking', { label: 'Чете файлове' }),
    ev(3, 'tool', { name: 'READ_FILE', result: 'x' }),
    ev(4, 'confirm', { id: 'c1', operation: 'rm x', reasons: ['r'] }),
    ev(5, 'confirm_done', { id: 'c1', allow: true }),
    ev(6, 'assistant', { text: 'done' }),
  ]);
  assert.deepEqual(items.map((i) => i.kind), ['user', 'tool', 'confirm', 'assistant']);
  assert.equal(items[2].kind === 'confirm' && items[2].state, 'allowed');
  assert.equal(pendingConfirm(items), undefined);
});

test('cleared empties the chat; merge keeps order without duplicates', () => {
  assert.equal(toChatItems([ev(1, 'user', { text: 'a' }), ev(2, 'cleared')]).length, 0);
  const merged = mergeEvents([ev(1, 'user'), ev(2, 'busy')], [ev(2, 'busy'), ev(3, 'assistant')], false);
  assert.deepEqual(merged.map((e) => e.seq), [1, 2, 3]);
  assert.deepEqual(mergeEvents([ev(5, 'x')], [ev(2, 'b'), ev(1, 'a')], true).map((e) => e.seq), [1, 2]);
});

test('the busy row shows the latest label of the running turn', () => {
  const t = turnInfo([ev(1, 'user'), ev(2, 'thinking', { label: 'A' }), ev(3, 'thinking', { label: 'B' })]);
  assert.deepEqual(t, { label: 'B', since: 1000 });
  assert.equal(toolLook('RUN_CMD').icon, 'terminal');
  assert.equal(toolLook('SEARCH_CODE').icon, 'search');
  assert.equal(toolLook('EDIT_FILE').icon, 'edit');
});

test('a tool header is split into target, extra and failure', () => {
  assert.deepEqual(toolHeader('RUN_CMD', '[RUN_CMD: pytest -q] (rc=1)\nFAILED'), { target: 'pytest -q', extra: '(rc=1)', failed: true });
  assert.deepEqual(toolHeader('RUN_CMD', '[RUN_CMD: pytest -q] (rc=0)\n..'), { target: 'pytest -q', extra: '', failed: false });
  assert.deepEqual(toolHeader('READ_FILE', '[READ_FILE: C:\\x\\calc.py] (редове 1–6 от 6)'),
                   { target: 'C:\\x\\calc.py', extra: '(редове 1–6 от 6)', failed: false });
  assert.equal(toolHeader('RUN_CMD', 'just text').target, 'just text');
});

test('token counts read short, with Bulgarian decimals', async () => {
  const { fmtTokens, niceMax } = await import('../src/renderer/lib/format.ts');
  assert.equal(fmtTokens(0), '0');
  assert.equal(fmtTokens(999), '999');
  assert.equal(fmtTokens(1234), '1,2K');
  assert.equal(fmtTokens(82_277), '82K');
  assert.equal(fmtTokens(999_700), '1,00M');  // never "1000K"
  assert.equal(fmtTokens(2_795_326), '2,80M');
  assert.equal(niceMax(82_277), 100_000);
  assert.equal(niceMax(18_000), 20_000);
  assert.equal(niceMax(0), 1);
});
