/**
 * Снимки и глас от телефона (2026-10-15): чистите функции в lib/media.ts, и
 * клиентът срещу истинския remote_server (tests/fake_genesis.py с подменени
 * Whisper и edge-tts).
 *   node --test tests/
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { once } from 'node:events';
import { readFileSync } from 'node:fs';
import { createInterface } from 'node:readline';

import { toChatItems } from '../src/lib/chat.ts';
import {
  appendDictation, base64Bytes, checkImages, DEFAULT_LIMITS, envelopeBytes, fitSize, formatClock, hasFeature, imageName,
  imagesLeft, lastSeqOf, limitsFrom, MAX_RECORDING_MS, MAX_SIDE, nextQuality, recordingMime, replyToRead, stripDataUrl,
  TARGET_IMAGE_BYTES, webMicAvailable,
} from '../src/lib/media.ts';
import { errorText, replyErrorText, sendErrorText, voiceErrorText } from '../src/lib/messages.ts';
import { Outbox } from '../src/lib/outbox.ts';
import {
  GenesisClient, MEDIA_TIMEOUT_MS, TRANSCRIBE_TIMEOUT_MS, open, parsePairingUrl, ProtocolError, seal, sendArgs, speakArgs, transcribeArgs,
  type FetchLike, type GenesisEvent, type OutgoingImage,
} from '../src/lib/protocol.ts';

const random = (n: number) => new Uint8Array(randomBytes(n));
const ev = (seq: number, type: string, extra: Partial<GenesisEvent> = {}): GenesisEvent => ({ seq, ts: seq, type, ...extra });

// 1×1 PNG — истинска картинка за vision.image_part на сървъра.
const PNG_1PX = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==';
const image = (id: string, data = PNG_1PX, name = 'снимка.png'): OutgoingImage => ({ id, mime: 'image/png', data, name });

// ── чистите функции ─────────────────────────────────────────────────────────

test('media buttons follow the server features; an older server has none', () => {
  assert.equal(hasFeature(null, 'voice'), false);
  assert.equal(hasFeature({}, 'images'), false, 'по-стар `genesis serve` не праща "features"');
  assert.equal(hasFeature({ features: ['commands'] }, 'voice'), false);
  assert.equal(hasFeature({ features: ['commands', 'voice', 'images'] }, 'voice'), true);
  assert.equal(hasFeature({ features: ['images'] }, 'images'), true);
  assert.equal(hasFeature({ features: 'voice' as unknown as string[] }, 'voice'), false);
});

test('limits come from the server, with sane fallbacks', () => {
  assert.deepEqual(limitsFrom(null), DEFAULT_LIMITS);
  assert.deepEqual(limitsFrom({ limits: { images: 4, images_bytes: 10485760, body: 262144, large_body: 19922944 } }), DEFAULT_LIMITS);
  const odd = limitsFrom({ limits: { images: 2, images_bytes: -1, body: Number.NaN } });
  assert.equal(odd.images, 2);
  assert.equal(odd.images_bytes, DEFAULT_LIMITS.images_bytes);
  assert.equal(odd.body, DEFAULT_LIMITS.body);
  assert.equal(limitsFrom({ limits: { images: 50 } }).images, 4, 'редът над полето е за 4');
  assert.equal(imagesLeft(3, DEFAULT_LIMITS), 1);
  assert.equal(imagesLeft(9, DEFAULT_LIMITS), 0);
});

test('base64 sizes agree with Node, with or without padding and data: prefix', () => {
  for (const n of [0, 1, 2, 3, 4, 5, 1000, 300 * 1024 + 1]) {
    const b64 = Buffer.alloc(n, 7).toString('base64');
    assert.equal(base64Bytes(b64), n, String(n));
    assert.equal(base64Bytes(b64.replace(/=+$/, '')), n, `${n} без =`);
    assert.equal(base64Bytes(`data:image/jpeg;base64,${b64}`), n, `${n} data:`);
  }
  assert.equal(stripDataUrl('data:audio/webm;codecs=opus;base64,QUJD'), 'QUJD');
  assert.equal(stripDataUrl('QUJD'), 'QUJD');
});

test('the phone refuses what the server would refuse, before 10 MiB travel', () => {
  const small = [image('a'), image('b')];
  assert.equal(checkImages(small, DEFAULT_LIMITS), null);
  assert.equal(checkImages([1, 2, 3, 4, 5].map((i) => image(String(i))), DEFAULT_LIMITS), 'too_many_images');
  const big = Buffer.alloc(6 * 1024 * 1024, 1).toString('base64');
  assert.equal(checkImages([image('a', big), image('b', big)], DEFAULT_LIMITS), 'images_too_large');
  assert.equal(checkImages([image('a', big)], DEFAULT_LIMITS), null);
  // Пликът също има таван (large_body): 10 MiB картинки × 4/3 × 4/3 ≈ 17.8 MiB < 19 MiB.
  assert.ok(envelopeBytes(Math.ceil((10 * 1024 * 1024 * 4) / 3)) < DEFAULT_LIMITS.large_body);
  assert.equal(checkImages([image('a', big)], { ...DEFAULT_LIMITS, large_body: 1024 * 1024 }), 'images_too_large');
});

test('downscale: the long side to 1568 px, never up, unknown size left alone', () => {
  assert.deepEqual(fitSize(4032, 3024), { width: MAX_SIDE, height: 1176 });
  assert.deepEqual(fitSize(3024, 4032), { width: 1176, height: MAX_SIDE });
  assert.deepEqual(fitSize(10000, 10), { width: MAX_SIDE, height: 2 });
  assert.deepEqual(fitSize(100000, 1), { width: MAX_SIDE, height: 1 });
  assert.equal(fitSize(1568, 1000), null);
  assert.equal(fitSize(800, 600), null);
  assert.equal(fitSize(0, 0), null, 'изборът може да не знае размера — решава декодираната картинка');
  // Качеството пада само ако снимката е много над целта, и не без край.
  assert.equal(nextQuality(TARGET_IMAGE_BYTES, 0.7), null);
  assert.equal(nextQuality(TARGET_IMAGE_BYTES * 2, 0.7), 0.55);
  assert.equal(nextQuality(TARGET_IMAGE_BYTES * 2, 0.55), 0.4);
  assert.equal(nextQuality(TARGET_IMAGE_BYTES * 9, 0.4), null);
});

test('picture names: from the file, as .jpg, no paths or control characters', () => {
  assert.equal(imageName('IMG_0001.HEIC', 0), 'IMG_0001.jpg');
  assert.equal(imageName('/data/user/0/cache/ImagePicker/abc.png', 1), 'abc.jpg');
  assert.equal(imageName(null, 1), 'снимка-2.jpg');
  assert.equal(imageName('‮gpj.exe', 0), 'gpj.jpg');
  assert.equal(imageName('a\nb.jpeg', 0), 'ab.jpg');
  assert.equal([...imageName('я'.repeat(300), 0)].length, 80);
});

test('the payloads: images only when there are any, the phone-side id never leaves', () => {
  assert.deepEqual(sendArgs('здравей'), { text: 'здравей' });
  assert.deepEqual(sendArgs('здравей', 'mid-123456'), { text: 'здравей', mid: 'mid-123456' });
  assert.deepEqual(sendArgs('', 'mid-123456', [image('local-id')]),
    { text: '', mid: 'mid-123456', images: [{ mime: 'image/png', data: PNG_1PX, name: 'снимка.png' }] });
  assert.deepEqual(transcribeArgs('audio/m4a', 'QUJD'), { mime: 'audio/m4a', data: 'QUJD', language: 'bg' });
  assert.deepEqual(speakArgs('Готово.'), { text: 'Готово.' });
});

test('the recording mime is what the server accepts', () => {
  assert.equal(recordingMime('ios'), 'audio/m4a');
  assert.equal(recordingMime('android', 'whatever'), 'audio/m4a');
  assert.equal(recordingMime('web', 'audio/webm;codecs=opus'), 'audio/webm');
  assert.equal(recordingMime('web', 'audio/mp4'), 'audio/mp4', 'Safari');
  assert.equal(recordingMime('web', 'audio/ogg; codecs=opus'), 'audio/ogg');
  assert.equal(recordingMime('web', ''), 'audio/webm');
  assert.equal(formatClock(0), '0:00');
  assert.equal(formatClock(7_900), '0:07');
  assert.equal(formatClock(MAX_RECORDING_MS), '2:00');
  assert.equal(MAX_RECORDING_MS, 120_000, 'voice.MAX_AUDIO_SECONDS');
});

test('the browser gets a mic button only where it can record (https or localhost)', () => {
  // `genesis serve --web` в LAN е http — там getUserMedia го няма.
  assert.equal(webMicAvailable({ isSecureContext: false, hasGetUserMedia: false, hasMediaRecorder: true }), false);
  assert.equal(webMicAvailable({ isSecureContext: undefined, hasGetUserMedia: true, hasMediaRecorder: true }), false);
  assert.equal(webMicAvailable({ isSecureContext: true, hasGetUserMedia: true, hasMediaRecorder: false }), false);
  assert.equal(webMicAvailable({ isSecureContext: true, hasGetUserMedia: true, hasMediaRecorder: true }), true);
});

test('dictation goes into the draft, after what is written — never sent by itself', () => {
  assert.equal(appendDictation('', ' пусни тестовете '), 'пусни тестовете');
  assert.equal(appendDictation('Виж', 'грешката'), 'Виж грешката');
  assert.equal(appendDictation('Виж\n', 'грешката'), 'Виж\nгрешката');
  assert.equal(appendDictation('Виж', '   '), 'Виж');
  const chat = readFileSync(new URL('../src/app/chat.tsx', import.meta.url), 'utf8');
  const after = chat.slice(chat.indexOf('const heard = await g.transcribe('));
  assert.match(after.slice(0, 200), /setDraft\(\(d\) => appendDictation\(d, heard\)\)/);
  assert.doesNotMatch(after.slice(0, 200), /g\.send\(/);
});

test('reading aloud: only the newest reply of a turn, never the history', () => {
  const items = toChatItems([ev(1, 'user', { text: 'а' }), ev(2, 'assistant', { text: 'старо' }),
    ev(3, 'user', { text: 'б' }), ev(4, 'assistant', { text: 'първо' }), ev(5, 'tool', { name: 'X', result: '' }),
    ev(6, 'assistant', { text: 'последно' }), ev(7, 'info', { text: 'бележка' })]);
  assert.deepEqual(replyToRead(items, 3), { seq: 6, text: 'последно' });
  assert.equal(replyToRead(items, 7), null, 'нищо ново');
  assert.equal(replyToRead(items, 6), null, 'вече прочетено');
  assert.equal(replyToRead(toChatItems([ev(1, 'assistant', { text: '  ' })]), 0), null);
  assert.equal(lastSeqOf(items), 7);
  assert.equal(lastSeqOf([]), 0);
});

test('a sent picture shows as a chip with its name; old events have none', () => {
  const [old] = toChatItems([ev(1, 'user', { text: 'здравей' })]);
  assert.deepEqual(old, { kind: 'user', key: '1', text: 'здравей', images: [] });
  const [withPics] = toChatItems([ev(2, 'user', {
    text: 'Какво виждаш на картинката?',
    images: [{ name: 'IMG_1.jpg', mime: 'image/jpeg', bytes: 1000, width: 10, height: 10 }, { name: 'a‮b' }, {}],
  })]);
  assert.ok(withPics.kind === 'user');
  assert.deepEqual(withPics.images, ['IMG_1.jpg', 'ab', 'снимка']);
  const cards = readFileSync(new URL('../src/components/Cards.tsx', import.meta.url), 'utf8');
  assert.match(cards, /📷 \{name\}/);
});

test('a retry is "the same send" only with the same pictures (2026-10-15)', async () => {
  // Без това: празен текст със снимка А, изгубен отговор, после празен текст
  // със снимка Б — същият mid, и сървърът (RemoteServer._sent) връщаше хода
  // на А, без да погледне Б.
  let n = 0;
  const box = new Outbox(() => `mid-${++n}-xxxxxx`);
  const lost = async (): Promise<{ ok: boolean }> => { throw new Error('lost'); };
  const mids: string[] = [];
  const ok = async (_t: string, mid: string) => { mids.push(mid); return { ok: true }; };
  await assert.rejects(box.send('', lost, 'A'));
  await box.send('', ok, 'B');
  assert.deepEqual(mids, ['mid-2-xxxxxx'], 'други снимки — нов mid');
  await assert.rejects(box.send('', lost, 'C'));
  await box.send('', ok, 'C');
  assert.deepEqual(mids, ['mid-2-xxxxxx', 'mid-3-xxxxxx'], 'същите снимки — същият mid');
  // Само снимки: балончето носи въпроса на сървъра, не празния текст — по
  // него mid-ът не се забравя, по `mids` — да.
  await assert.rejects(box.send('', lost, 'D'));
  box.seen([{ type: 'user', text: 'Какво виждаш на картинката?' }]);
  await box.send('', ok, 'D');
  assert.equal(mids[2], 'mid-4-xxxxxx');
  const hook = readFileSync(new URL('../src/lib/useGenesis.ts', import.meta.url), 'utf8');
  assert.match(hook, /images\.map\(\(i\) => i\.id\)\.join\(','\)/);
});

test('picture and voice errors reach the operator in Bulgarian', () => {
  for (const code of ['too_many_images', 'images_too_large', 'bad_image', 'too_large']) {
    const text = replyErrorText(code);
    assert.notEqual(text, 'Компютърът отказа заявката.', code);
    assert.doesNotMatch(text.replace(/Genesis/g, ''), /[a-z]{4,}/, code);
  }
  assert.match(sendErrorText({ error: 'bad_image', name: 'x.jpg', detail: 'невалиден base64' }), /„x\.jpg“: невалиден base64/);
  assert.match(sendErrorText({ error: 'too_many_images', max: 4 }), /4 снимки/);
  assert.match(sendErrorText({ error: 'busy' }), /още работи/);
  for (const code of ['too_large', 'too_long', 'bad_mime', 'empty', 'busy', 'rate_limited', 'stt_failed', 'unknown_op']) {
    const text = voiceErrorText('transcribe', { error: code });
    assert.notEqual(text, 'Компютърът отказа заявката.', code);
    assert.doesNotMatch(text.replace(/Genesis/g, ''), /[a-z]{4,}/, code);
  }
  assert.match(voiceErrorText('transcribe', { error: 'too_long' }), /2 минути/);
  assert.match(voiceErrorText('transcribe', { error: 'stt_failed', detail: 'Няма ключ за Groq.' }), /Няма ключ за Groq/);
  assert.match(voiceErrorText('speak', { error: 'tts_failed', detail: 'edge-tts липсва' }), /edge-tts липсва/);
  assert.match(voiceErrorText('speak', { error: 'empty' }), /няма какво да се прочете/);
  assert.doesNotMatch(voiceErrorText('speak', { error: 'too_long' }), /2 минути/, '„too_long“ на speak е друго');
  assert.equal(voiceErrorText('transcribe', { error: 'toString' }), 'Компютърът отказа заявката.');
});

test('429 "busy" on a large envelope is "try again shortly", not the brake or "offline"', async () => {
  // remote_server._voice_post: по един голям плик наведнъж; вторият получава
  // 429 {"error":"busy"}. Беше „много неуспешни опити — изчакай минута“ и
  // червена точка „няма връзка“.
  const pairing = parsePairingUrl('http://pc:1/#k=AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8')!;
  const reply = (status: number, body: unknown): FetchLike => async () => ({ status, json: async () => body });
  const busy = await new GenesisClient(pairing, random, reply(429, { error: 'busy' })).transcribe('audio/m4a', 'QUJD')
    .catch((e: unknown) => e);
  assert.ok(busy instanceof ProtocolError && busy.kind === 'server' && busy.status === 429 && busy.code === 'busy');
  assert.match(errorText(busy), /друг голям файл/);
  const brake = await new GenesisClient(pairing, random, reply(429, { error: 'slow_down' })).status().catch((e: unknown) => e);
  assert.ok(brake instanceof ProtocolError && brake.kind === 'network');
  assert.match(errorText(brake), /изчакай минута/);
  const hook = readFileSync(new URL('../src/lib/useGenesis.ts', import.meta.url), 'utf8');
  // Правилото „server не сваля връзката“ е в losesConnection (lib/busy.ts, 2026-10-27).
  assert.match(hook, /if \(losesConnection\(e, !noPoll\(\)\)\) fail\(e\);/);
});

test('media requests wait 60 s, the rest 15 s', async () => {
  const pairing = parsePairingUrl('http://pc:1/#k=AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8')!;
  const key = Buffer.from('AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8', 'base64url');
  const waits: number[] = [];
  const realSetTimeout = globalThis.setTimeout;
  const spy = ((fn: () => void, ms?: number) => {
    waits.push(ms ?? 0);
    return realSetTimeout(fn, 1e9);
  }) as typeof setTimeout;
  const fetcher: FetchLike = async (_url, init) => {
    const req = open(new Uint8Array(key), JSON.parse(init.body), 'genesis/1/req') as { rid: string };
    return { status: 200, json: async () => seal(new Uint8Array(key), { ok: true }, `genesis/1/res:${req.rid}`, random) };
  };
  const client = new GenesisClient(pairing, random, fetcher);
  globalThis.setTimeout = spy;
  try {
    await client.send('здравей');
    await client.send('', undefined, [image('x')]);
    await client.transcribe('audio/m4a', 'QUJD');
    await client.speak('Готово.');
  } finally {
    globalThis.setTimeout = realSetTimeout;
  }
  assert.deepEqual(waits, [15000, MEDIA_TIMEOUT_MS, TRANSCRIBE_TIMEOUT_MS, MEDIA_TIMEOUT_MS]);
  assert.equal(MEDIA_TIMEOUT_MS, 60000);
});

test('app.json: Bulgarian permission texts, the mic is no longer blocked, no background audio', () => {
  const app = JSON.parse(readFileSync(new URL('../app.json', import.meta.url), 'utf8')).expo;
  assert.ok(!(app.android.blockedPermissions ?? []).includes('android.permission.RECORD_AUDIO'));
  const plugin = (name: string) => app.plugins.find((p: unknown) => Array.isArray(p) && p[0] === name)?.[1];
  const audio = plugin('expo-audio');
  const picker = plugin('expo-image-picker');
  const camera = plugin('expo-camera');
  assert.ok(audio && picker && camera);
  assert.equal(audio.enableBackgroundPlayback, false);
  assert.equal(audio.enableBackgroundRecording, false);
  // Един и същ текст навсякъде — кой плъгин пише последен в Info.plist, не е наша грижа.
  assert.equal(picker.microphonePermission, audio.microphonePermission);
  assert.equal(camera.microphonePermission, audio.microphonePermission);
  assert.equal(picker.cameraPermission, camera.cameraPermission);
  for (const text of [audio.microphonePermission, picker.photosPermission, camera.cameraPermission]) {
    assert.match(text, /[а-я]{4,}/);
    assert.doesNotMatch(text, /Allow|\$\(PRODUCT_NAME\)/);
  }
});

// ── срещу истинския сървър ──────────────────────────────────────────────────

async function startServer(args: string[] = []) {
  const python = process.env.PYTHON ?? 'python3';
  const child = spawn(python, [new URL('./fake_genesis.py', import.meta.url).pathname, ...args], {
    stdio: ['pipe', 'pipe', 'inherit'],
  });
  const lines = createInterface({ input: child.stdout! });
  const [first] = (await once(lines, 'line')) as [string];
  const info = JSON.parse(first) as { port: number; url: string };
  return { info, stop: () => { child.stdin!.end(); child.kill(); } };
}

async function waitFor(client: GenesisClient, predicate: (e: GenesisEvent) => boolean) {
  let after = 0;
  const seen: GenesisEvent[] = [];
  for (let i = 0; i < 20; i++) {
    const reply = await client.events(after, 5);
    seen.push(...reply.events);
    after = reply.last;
    const hit = seen.find(predicate);
    if (hit) return { hit, seen };
  }
  throw new Error('event never came: ' + JSON.stringify(seen));
}

test('the server says "voice" and "images" and its limits; without voice — no "voice"', async () => {
  const on = await startServer();
  try {
    const status = await new GenesisClient(parsePairingUrl(on.info.url)!, random).status();
    assert.equal(hasFeature(status, 'voice'), true);
    assert.equal(hasFeature(status, 'images'), true);
    assert.deepEqual(limitsFrom(status), DEFAULT_LIMITS);
    assert.deepEqual(status.limits, DEFAULT_LIMITS, 'числата на сървъра са нашите по подразбиране');
  } finally {
    on.stop();
  }
  const off = await startServer(['--no-voice']);
  try {
    const client = new GenesisClient(parsePairingUrl(off.info.url)!, random);
    assert.equal(hasFeature(await client.status(), 'voice'), false);
    const reply = await client.transcribe('audio/m4a', 'QUJD');
    assert.deepEqual(reply, { ok: false, error: 'unknown_op' });
    assert.match(voiceErrorText('transcribe', reply), /не поддържа глас/);
  } finally {
    off.stop();
  }
});

test('a picture reaches the turn; the transcript gets its name, never its bytes', async () => {
  const { info, stop } = await startServer();
  try {
    const client = new GenesisClient(parsePairingUrl(info.url)!, random);
    assert.deepEqual(await client.send('какво е това?', 'mid-image-0001', [image('local', PNG_1PX, 'точка.png')]), { ok: true });
    const { hit, seen } = await waitFor(client, (e) => e.type === 'assistant');
    assert.match(hit.text ?? '', /Видях: точка\.png image\/png — какво е това\?/);
    const user = seen.find((e) => e.type === 'user')!;
    assert.deepEqual(user.images?.map((i) => i.name), ['точка.png']);
    assert.ok(!JSON.stringify(seen).includes(PNG_1PX.slice(0, 40)), 'байтовете не пътуват в `events`');
    const [bubble] = toChatItems([user]);
    assert.ok(bubble.kind === 'user' && bubble.images[0] === 'точка.png');
  } finally {
    stop();
  }
});

test('a picture without text is sent; a broken one is refused in Bulgarian', async () => {
  const { info, stop } = await startServer();
  try {
    const client = new GenesisClient(parsePairingUrl(info.url)!, random);
    const bad = await client.send('', undefined, [image('x', Buffer.from('not an image').toString('base64'), 'x.png')]);
    assert.equal(bad.error, 'bad_image');
    assert.match(sendErrorText(bad), /„x\.png“/);
    const many = await client.send('', undefined, [1, 2, 3, 4, 5].map((i) => image(String(i))));
    assert.deepEqual(many, { ok: false, error: 'too_many_images', max: 4 });
    assert.deepEqual(await client.send('', undefined, [image('y')]), { ok: true });
    const { hit } = await waitFor(client, (e) => e.type === 'assistant');
    assert.match(hit.text ?? '', /Какво виждаш на картинката\?/, 'празен текст → въпросът на сървъра');
  } finally {
    stop();
  }
});

test('a picture over 256 KiB goes as a large envelope and still gets a sealed answer', async () => {
  const { info, stop } = await startServer();
  try {
    const client = new GenesisClient(parsePairingUrl(info.url)!, random);
    // ~300 KB: PNG с опашка — Pillow я отваря, сървърът я праща както е.
    const png = Buffer.concat([Buffer.from(PNG_1PX, 'base64'), Buffer.alloc(300 * 1024)]);
    const big = image('big', png.toString('base64'), 'голяма.png');
    assert.ok(envelopeBytes(big.data.length + 200) > DEFAULT_LIMITS.body, 'наистина голям плик');
    assert.equal(checkImages([big], DEFAULT_LIMITS), null);
    assert.deepEqual(await client.send('погледни', 'mid-big-000001', [big]), { ok: true });
    const { hit } = await waitFor(client, (e) => e.type === 'assistant');
    assert.match(hit.text ?? '', /голяма\.png/);
  } finally {
    stop();
  }
});

test('transcribe gives text for the input box; speak gives mp3 bytes; errors carry the detail', async () => {
  const { info, stop } = await startServer();
  try {
    const client = new GenesisClient(parsePairingUrl(info.url)!, random);
    const audio = Buffer.from('fake m4a audio').toString('base64');
    assert.deepEqual(await client.transcribe('audio/m4a', audio), { ok: true, text: 'чух 14 байта audio/m4a bg' });
    // Браузърът: webm с ;codecs и data: адрес отпред — сървърът ги приема.
    const web = await client.transcribe('audio/webm;codecs=opus', `data:audio/webm;base64,${audio}`);
    assert.equal(web.text, 'чух 14 байта audio/webm bg');
    const failed = await client.transcribe('audio/m4a', Buffer.from('FAIL').toString('base64'));
    assert.equal(failed.error, 'stt_failed');
    assert.match(voiceErrorText('transcribe', failed), /няма ключ за Groq/);
    assert.equal((await client.transcribe('audio/x-unknown', audio)).error, 'bad_mime');
    assert.equal((await client.transcribe('audio/m4a', '')).error, 'empty');

    const spoken = await client.speak('**Готово.** Пуснах тестовете.');
    assert.equal(spoken.ok, true);
    assert.equal(spoken.mime, 'audio/mpeg');
    const mp3 = Buffer.from(spoken.data!, 'base64');
    assert.equal(mp3.subarray(0, 3).toString(), 'ID3');
    assert.doesNotMatch(mp3.subarray(3).toString('utf8'), /\*\*/, 'без markdown');
    const code = await client.speak('   ');
    assert.equal(code.error, 'empty');
    assert.match(voiceErrorText('speak', code), /няма какво да се прочете/);
  } finally {
    stop();
  }
});

test('a long recording goes as a large envelope', async () => {
  const { info, stop } = await startServer();
  try {
    const client = new GenesisClient(parsePairingUrl(info.url)!, random);
    // ~1 MB = 2 минути AAC при 64 kbps (lib/audio.ts SPEECH).
    const audio = Buffer.alloc(960 * 1024, 3).toString('base64');
    assert.ok(envelopeBytes(audio.length + 100) > DEFAULT_LIMITS.body);
    const reply = await client.transcribe('audio/webm', audio);
    assert.deepEqual(reply, { ok: true, text: `чух ${960 * 1024} байта audio/webm bg` });
  } finally {
    stop();
  }
});
