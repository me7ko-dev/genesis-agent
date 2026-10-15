/**
 * Картинки и глас в прозореца (2026-10-15): чистите правила (shared/media.ts),
 * разрешенията на Chromium (main/permissions.ts) и истинският сървър през
 * клиента на Desktop (tests/fake_media.py).   npm test
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { once } from 'node:events';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import { crc32, deflateSync } from 'node:zlib';
import { GenesisClient, ProtocolError } from '../src/main/protocol.ts';
import { allowPermission } from '../src/main/permissions.ts';
import {
  base64Bytes, baseMime, checkImages, clock, DEFAULT_LIMITS, fitSize, imageErrorText, imageMimeOf, limitsFrom,
  micErrorText, pastedName, recorderMime, replyToSpeak, sanitizeImages, speechText, takeImages, voiceErrorText,
  MAX_SPEAK_CHARS,
} from '../src/shared/media.ts';
import { toChatItems } from '../src/renderer/lib/chat.ts';
import type { GenesisEvent } from '../src/shared/types.ts';

const FAKE = fileURLToPath(new URL('./fake_media.py', import.meta.url));
const KEY = Buffer.from(Array.from({ length: 32 }, (_, i) => i));

// ── чистите правила ─────────────────────────────────────────────────────────

test('downscale keeps the aspect and never grows', () => {
  assert.deepEqual(fitSize(4032, 3024), { width: 1568, height: 1176, scaled: true });
  assert.deepEqual(fitSize(1080, 2400), { width: 706, height: 1568, scaled: true });
  assert.deepEqual(fitSize(1568, 900), { width: 1568, height: 900, scaled: false });
  assert.deepEqual(fitSize(300, 200), { width: 300, height: 200, scaled: false });
  assert.deepEqual(fitSize(100000, 1).height, 1);
});

test('base64 size is what the server decodes', () => {
  for (const n of [0, 1, 2, 3, 4, 5, 1000, 1001]) {
    assert.equal(base64Bytes(Buffer.alloc(n, 7).toString('base64')), n, `n=${n}`);
  }
  assert.equal(base64Bytes(Buffer.alloc(10).toString('base64url')), 10);   // без `=`
});

test('the server limits are checked before the network', () => {
  const im = (name: string, bytes: number, mime = 'image/png') => ({ name, mime, data: Buffer.alloc(bytes).toString('base64') });
  assert.deepEqual(checkImages([im('a', 10)]), { ok: true });
  assert.deepEqual(checkImages([1, 2, 3, 4, 5].map((i) => im(`${i}`, 10))), { ok: false, error: 'too_many_images' });
  assert.deepEqual(checkImages([im('a', 6 << 20), im('b', 5 << 20)]), { ok: false, error: 'images_too_large' });
  assert.deepEqual(checkImages([im('x.bmp', 10, 'image/bmp')]), { ok: false, error: 'bad_image', name: 'x.bmp' });
  // Таваните от агента печелят; по-стар агент без "limits" — тези от 2026-10-15.
  assert.deepEqual(checkImages([im('a', 10), im('b', 10)], { ...DEFAULT_LIMITS, images: 1 }), { ok: false, error: 'too_many_images' });
  assert.deepEqual(limitsFrom(undefined), DEFAULT_LIMITS);
  assert.deepEqual(limitsFrom({ images: 2, images_bytes: 'x', body: -1 }), { ...DEFAULT_LIMITS, images: 2 });
});

test('main takes only well-formed images from the window', () => {
  assert.deepEqual(sanitizeImages(undefined), []);
  assert.deepEqual(sanitizeImages([{ mime: 'image/png', data: 'AAAA', name: 'a.png', extra: 1 }]),
                   [{ mime: 'image/png', data: 'AAAA', name: 'a.png' }]);
  assert.equal(sanitizeImages('x'), undefined);
  assert.equal(sanitizeImages([{ mime: 'image/png', data: '' }]), undefined);
  assert.equal(sanitizeImages([{ mime: 1, data: 'AAAA' }]), undefined);
  assert.equal(sanitizeImages(Array.from({ length: 5 }, () => ({ mime: 'image/png', data: 'AAAA' }))), undefined);
  assert.equal(sanitizeImages([{ mime: 'image/png', data: 'AAAA', name: 'я'.repeat(500) }])![0].name.length, 120);
});

test('which files are pictures, how many fit, what a paste is called', () => {
  assert.equal(imageMimeOf('image/png', 'a'), 'image/png');
  assert.equal(imageMimeOf('image/jpg', 'a'), 'image/jpeg');
  assert.equal(imageMimeOf('', 'Снимка.JPEG'), 'image/jpeg');
  assert.equal(imageMimeOf('application/pdf', 'a.png'), undefined);   // видът казан — разширението не лъже
  assert.equal(imageMimeOf('', 'notes.txt'), undefined);
  assert.deepEqual(takeImages(3, ['a', 'b', 'c']), { take: ['a'], dropped: 2 });
  assert.deepEqual(takeImages(4, ['a']), { take: [], dropped: 1 });
  assert.equal(pastedName('image/png', new Date(2026, 9, 15, 9, 5, 7)), 'екран-20261015-090507.png');
  assert.equal(pastedName('image/jpeg', new Date(2026, 0, 1)), 'екран-20260101-000000.jpg');
});

test('recording format, clock and the speak cap', () => {
  assert.equal(recorderMime(() => true), 'audio/webm;codecs=opus');
  assert.equal(recorderMime((m) => m === 'audio/webm'), 'audio/webm');
  assert.equal(recorderMime(() => false), undefined);
  assert.equal(baseMime('audio/webm;codecs=opus'), 'audio/webm');
  assert.equal(clock(0), '0:00');
  assert.equal(clock(75_400), '1:15');
  assert.equal(clock(120_000), '2:00');
  assert.equal(speechText('кратко'), 'кратко');
  assert.equal(speechText('я'.repeat(MAX_SPEAK_CHARS + 50)).length, MAX_SPEAK_CHARS);
});

test('every server error reads as Bulgarian', () => {
  assert.match(imageErrorText({ error: 'too_many_images', max: 4 })!, /Най-много 4 картинки/);
  assert.match(imageErrorText({ error: 'images_too_large', max: 10 * 1048576 })!, /над 10 MB/);
  assert.match(imageErrorText({ error: 'bad_image', name: 'a.png', detail: 'невалиден base64' })!, /„a.png“ не става: невалиден base64/);
  assert.match(imageErrorText({ error: 'too_large' })!, /твърде голямо/);
  assert.equal(imageErrorText({ error: 'busy' }), undefined);
  for (const code of ['too_large', 'too_long', 'bad_mime', 'empty', 'busy', 'rate_limited', 'stt_failed', 'unknown_op']) {
    const text = voiceErrorText('transcribe', { error: code });
    assert.match(text, /[а-я]/, code);
    assert.doesNotMatch(text, new RegExp(`\\b${code}\\b`), `${code} must not leak the code`);
  }
  assert.match(voiceErrorText('transcribe', { error: 'stt_failed', detail: 'Няма ключ за Groq' }), /Няма ключ за Groq/);
  assert.match(voiceErrorText('speak', { error: 'tts_failed' }), /озвуча/);
  assert.match(voiceErrorText('transcribe', { error: 'too_long' }), /2 минути/);
  assert.match(micErrorText('NotAllowedError'), /Поверителност/);
  assert.match(micErrorText('NotFoundError'), /Не намирам микрофон/);
});

// ── „Чети отговорите на глас“ ───────────────────────────────────────────────

const ev = (seq: number, type: string, extra: Partial<GenesisEvent> = {}): GenesisEvent => ({ seq, ts: seq, type, ...extra });

test('only the last reply of a finished turn is read, once', () => {
  const turn = [
    ev(1, 'user', { text: 'здрасти' }), ev(2, 'busy', { busy: true }),
    ev(3, 'assistant', { text: 'Ще погледна.' }), ev(4, 'tool', { name: 'READ_FILE' }),
    ev(5, 'assistant', { text: 'Готово: всичко е наред.' }),
  ];
  assert.deepEqual(replyToSpeak(turn, 0), { text: undefined, key: undefined, seq: 5 }, 'not while the turn runs');
  const done = [...turn, ev(6, 'busy', { busy: false })];
  assert.deepEqual(replyToSpeak(done, 5), { text: 'Готово: всичко е наред.', key: 5, seq: 6 });
  assert.equal(replyToSpeak(done, 6).text, undefined, 'already read');
});

test('a replayed history (/resume) is not read aloud', () => {
  const events = [
    ev(1, 'user', { text: '/resume 1' }), ev(2, 'busy', { busy: true }), ev(3, 'cleared'),
    ev(4, 'user', { text: 'стар' }), ev(5, 'assistant', { text: 'стар отговор' }), ev(6, 'info', { text: '✓ Зареден' }),
    ev(7, 'busy', { busy: false }),
  ];
  assert.equal(replyToSpeak(events, 2).text, undefined);
  // Следващият истински ход — да.
  const next = [...events, ev(8, 'user'), ev(9, 'busy', { busy: true }), ev(10, 'assistant', { text: 'нов' }), ev(11, 'busy', { busy: false })];
  assert.deepEqual(replyToSpeak(next, 7), { text: 'нов', key: 10, seq: 11 });
  // Ход без отговор (само инструменти, спрян) — нищо.
  assert.equal(replyToSpeak([ev(1, 'busy', { busy: true }), ev(2, 'tool'), ev(3, 'busy', { busy: false })], 0).text, undefined);
});

test('a user event with pictures keeps their names for the chip', () => {
  const [item] = toChatItems([ev(1, 'user', { text: 'какво има тук?', images: [{ name: 'a.png', mime: 'image/png', bytes: 10, width: 4, height: 3 }] })]);
  assert.ok(item.kind === 'user');
  assert.deepEqual(item.images, [{ name: 'a.png', mime: 'image/png', bytes: 10, width: 4, height: 3 }]);
  const [plain] = toChatItems([ev(1, 'user', { text: 'x' })]);
  assert.equal('images' in plain, false);
});

// ── разрешенията ────────────────────────────────────────────────────────────

test('the window gets the microphone and the clipboard, nothing else', () => {
  assert.equal(allowPermission('media', { mediaTypes: ['audio'] }), true);
  assert.equal(allowPermission('media', { mediaTypes: ['video'] }), false);
  assert.equal(allowPermission('media', { mediaTypes: ['audio', 'video'] }), false);
  assert.equal(allowPermission('media', { mediaTypes: [] }), false);
  assert.equal(allowPermission('media', { mediaType: 'audio' }), true);
  assert.equal(allowPermission('media', { mediaType: 'unknown' }), true);
  assert.equal(allowPermission('media', { mediaType: 'video' }), false);
  assert.equal(allowPermission('clipboard-sanitized-write'), true);
  for (const p of ['geolocation', 'notifications', 'display-capture', 'clipboard-read', 'openExternal', 'hid', 'usb']) {
    assert.equal(allowPermission(p), false, p);
  }
});

// ── истинският сървър ───────────────────────────────────────────────────────

async function startServer() {
  const python = process.env.PYTHON ?? (process.platform === 'win32' ? 'python' : 'python3');
  const child = spawn(python, [FAKE], { stdio: ['pipe', 'pipe', 'inherit'], env: { ...process.env, PYTHONUTF8: '1' } });
  const lines = createInterface({ input: child.stdout! });
  const [first] = (await once(lines, 'line')) as [string];
  const info = JSON.parse(first) as { port: number };
  return { client: new GenesisClient(`http://127.0.0.1:${info.port}`, KEY), stop: () => { child.stdin!.end(); child.kill(); } };
}

/** Истински PNG с шум — не се свива, тоест пликът минава 256 KiB. */
function noisePng(width: number, height: number): Buffer {
  const chunk = (type: string, data: Buffer) => {
    const head = Buffer.alloc(8);
    head.writeUInt32BE(data.length, 0);
    head.write(type, 4, 'ascii');
    const crc = Buffer.alloc(4);
    crc.writeUInt32BE(crc32(Buffer.concat([Buffer.from(type, 'ascii'), data])) >>> 0, 0);
    return Buffer.concat([head, data, crc]);
  };
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8; ihdr[9] = 2;   // 8 бита, RGB
  const raw = randomBytes((width * 3 + 1) * height);
  for (let row = 0; row < height; row++) raw[row * (width * 3 + 1)] = 0;   // филтър „няма“
  return Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr), chunk('IDAT', deflateSync(raw)), chunk('IEND', Buffer.alloc(0))]);
}

async function waitFor(client: GenesisClient, predicate: (e: GenesisEvent) => boolean) {
  let after = 0;
  for (let i = 0; i < 20; i++) {
    const reply = await client.call<{ events: GenesisEvent[]; last: number }>('events', { after, wait: 5 });
    const hit = reply.events.find(predicate);
    if (hit) return hit;
    after = reply.last;
  }
  throw new Error('event never came');
}

test('Desktop sends pictures, dictation and speech through the real server', async () => {
  const { client, stop } = await startServer();
  try {
    const status = await client.call<{ features: string[]; limits: unknown }>('status');
    assert.ok(status.features.includes('images') && status.features.includes('voice'));
    assert.deepEqual(limitsFrom(status.limits), DEFAULT_LIMITS);

    // Над 256 KiB плик — големият път на сървъра; само картинка, без текст.
    const png = noisePng(320, 320);
    assert.ok(png.length > 256 * 1024);
    const images = [{ mime: 'image/png', data: png.toString('base64'), name: 'шум.png' }];
    assert.deepEqual(checkImages(images), { ok: true });
    assert.deepEqual(await client.call('send', { text: '', mid: 'desktop-media-1', images }, 60_000), { ok: true });
    const user = await waitFor(client, (e) => e.type === 'user');
    assert.deepEqual(user.images, [{ name: 'шум.png', mime: 'image/png', bytes: png.length, width: 320, height: 320 }]);
    assert.ok(user.text, 'an empty text with a picture gets the server question');
    assert.match((await waitFor(client, (e) => e.type === 'assistant')).text ?? '', /^Видях 1 картинки/);

    // Отказите идват като кодове, които прозорецът превежда.
    const many = await client.call<{ ok: boolean; error: string }>('send', { text: 'x', images: [...images, ...images, ...images, ...images, ...images] }, 60_000);
    assert.equal(many.error, 'too_many_images');
    const bad = await client.call<{ error: string; name: string }>('send', { text: 'x', images: [{ mime: 'image/png', data: 'aGVsbG8=', name: 'a.png' }] });
    assert.equal(bad.error, 'bad_image');
    assert.match(imageErrorText(bad)!, /„a.png“/);

    // Голям плик само с текст: HTTP 413 → „твърде голямо“ (server 2026-10-15).
    await assert.rejects(client.call('send', { text: 'я'.repeat(200_000) }, 30_000),
      (e: unknown) => e instanceof ProtocolError && e.message === 'too_large' && e.kind === 'server');
    assert.match(imageErrorText({ error: 'too_large' })!, /твърде голямо/);

    // Диктовка: webm от MediaRecorder; езикът — по настройката на оператора.
    const audio = Buffer.from('webm-ish audio');
    const heard = await client.call<{ ok: boolean; text: string }>('transcribe',
      { mime: baseMime('audio/webm;codecs=opus'), data: audio.toString('base64') }, 60_000);
    assert.deepEqual(heard, { ok: true, text: `чух ${audio.length} байта audio/webm [-]` });
    const empty = await client.call<{ error: string }>('transcribe', { mime: 'audio/webm', data: '' });
    assert.equal(empty.error, 'empty');
    const mime = await client.call<{ error: string }>('transcribe', { mime: 'video/mp4', data: 'AAAA' });
    assert.equal(mime.error, 'bad_mime');

    // Гласът: mp3 като base64.
    const spoken = await client.call<{ ok: boolean; mime: string; data: string }>('speak', { text: speechText('Готово.') }, 60_000);
    assert.equal(spoken.mime, 'audio/mpeg');
    assert.equal(Buffer.from(spoken.data, 'base64').toString('utf8'), 'ID3Готово.');
    const blank = await client.call<{ error: string }>('speak', { text: '**' });
    assert.equal(blank.error, 'empty');
    assert.match(voiceErrorText('speak', blank), /Няма какво да се прочете/);
  } finally {
    stop();
  }
});
