/**
 * Снимки и глас от телефона (2026-10-15) — частта без React Native: какво
 * поддържа компютърът, колко е голямо, как се смалява, какво казва грешката.
 * Чисти функции, за да вървят и под Node в tests/media.test.ts.
 */
// Само типове: файлът се зарежда и от тестовете под Node, където './protocol'
// без разширение не се намира.
import type { OutgoingImage, ServerLimits, StatusReply } from './protocol';

// ── какво поддържа компютърът ───────────────────────────────────────────────

export type Feature = 'voice' | 'images';

/** Всеки бутон за снимка или глас е зад флага от `status`: приложението
 *  работи и срещу по-стар `genesis serve`, който не ги знае (2026-10-15). */
export function hasFeature(status: Pick<StatusReply, 'features'> | null | undefined, feature: Feature): boolean {
  return Array.isArray(status?.features) && status.features.includes(feature);
}

/** Колкото казва remote_server.hello (2026-10-15). */
export const DEFAULT_LIMITS: ServerLimits = { images: 4, images_bytes: 10 * 1024 * 1024, body: 256 * 1024, large_body: 19 * 1024 * 1024 };

/** Таваните от сървъра; липсващо или нелепо число → нашето по подразбиране.
 *  Над 4 картинки не показваме, каквото и да каже сървърът — редът над полето
 *  е за толкова. */
export function limitsFrom(status: Pick<StatusReply, 'limits'> | null | undefined): ServerLimits {
  const raw = status?.limits ?? {};
  const pick = (key: keyof ServerLimits) => {
    const v = raw[key];
    return typeof v === 'number' && Number.isFinite(v) && v > 0 ? Math.floor(v) : DEFAULT_LIMITS[key];
  };
  return { images: Math.min(pick('images'), DEFAULT_LIMITS.images), images_bytes: pick('images_bytes'),
           body: pick('body'), large_body: pick('large_body') };
}

// ── base64 и размери ────────────────────────────────────────────────────────

/** Колко байта дава този base64 (обикновен или url-safe, с или без `=`). */
export function base64Bytes(b64: string): number {
  const body = b64.replace(/^data:[^,]*,/, '').replace(/\s+/g, '');
  const pad = body.endsWith('==') ? 2 : body.endsWith('=') ? 1 : 0;
  const n = body.length - pad;
  return Math.floor((n * 3) / 4);
}

/** Само base64 — без `data:…;base64,` отпред (уеб четецът го слага). */
export function stripDataUrl(text: string): string {
  const comma = text.indexOf(',');
  return text.startsWith('data:') && comma > 0 ? text.slice(comma + 1) : text;
}

/** Колко ще тежи запечатаният плик за JSON с тази дължина в UTF-8:
 *  шифротекстът е +16 байта таг, base64url го прави 4/3, плюс полетата. */
export function envelopeBytes(jsonBytes: number): number {
  return Math.ceil(((jsonBytes + 16) * 4) / 3) + 64;
}

/** Заявката с тези картинки — дали минава изобщо, и дали е „голям“ плик
 *  (по един наведнъж на сървъра). Текстът се брои грубо по 3 байта на знак. */
export function imagesEnvelope(text: string, images: readonly Pick<OutgoingImage, 'data' | 'name'>[]): number {
  let json = 200 + text.length * 3;
  for (const image of images) json += image.data.length + image.name.length * 3 + 60;
  return envelopeBytes(json);
}

export type ImagesProblem = 'too_many_images' | 'images_too_large' | null;

/** Проверката на сървъра (parse_images), направена тук — без да пращаме
 *  10 MiB, за да чуем „не“. */
export function checkImages(images: readonly Pick<OutgoingImage, 'data' | 'name'>[], limits: ServerLimits, text = ''): ImagesProblem {
  if (images.length > limits.images) return 'too_many_images';
  const total = images.reduce((sum, image) => sum + base64Bytes(image.data), 0);
  if (total > limits.images_bytes) return 'images_too_large';
  if (images.length && imagesEnvelope(text, images) > limits.large_body) return 'images_too_large';
  return null;
}

/** Колко още може да се добавят. */
export function imagesLeft(count: number, limits: ServerLimits): number {
  return Math.max(0, limits.images - count);
}

// ── смаляване ───────────────────────────────────────────────────────────────

/** Дългата страна след смаляването — колкото vision.MAX_SIDE на компютъра:
 *  по-голямо моделът и без това не вижда (2026-10-15). */
export const MAX_SIDE = 1568;

/** Към колко се стреми една снимка (2026-10-15, сметка, не измерване на
 *  телефон): плик под 256 KiB побира ≤ ~145 KB JPEG (base64 × 4/3, после
 *  шифротекстът още × 4/3), а при 1568 px това иска качество, при което
 *  текстът на екранна снимка се размазва — затова не го гоним. Над 256 KiB
 *  заявката минава по пътя за голям плик (по една наведнъж на сървъра), а
 *  ~300 KB пак пестят мрежа и батерия спрямо 3–6 MB оригинал. */
export const TARGET_IMAGE_BYTES = 300 * 1024;
export const JPEG_QUALITY = 0.7;
/** По-ниските стъпки, ако снимката остане много над целта (шум, трева). */
const QUALITY_STEPS = [0.7, 0.55, 0.4];

/** Размерът за `resize`: дългата страна до MAX_SIDE, пропорционално, никога
 *  нагоре. null — не се смалява (или размерът е неизвестен: 0). */
export function fitSize(width: number, height: number, maxSide = MAX_SIDE): { width: number; height: number } | null {
  if (!(width > 0 && height > 0)) return null;
  const long = Math.max(width, height);
  if (long <= maxSide) return null;
  const scale = maxSide / long;
  return width >= height
    ? { width: maxSide, height: Math.max(1, Math.round(height * scale)) }
    : { width: Math.max(1, Math.round(width * scale)), height: maxSide };
}

/** Следващото качество, ако тази снимка е над 1.5 × целта; null — стига. */
export function nextQuality(bytes: number, quality: number): number | null {
  if (bytes <= TARGET_IMAGE_BYTES * 1.5) return null;
  const next = QUALITY_STEPS.find((q) => q < quality - 1e-9);
  return next ?? null;
}

/** Името в чата и в модела: от файла, ако го има, но с `.jpg` — телефонът
 *  праща JPEG след смаляването. Без пътища, контролни знаци и до 80 знака. */
export function imageName(fileName: string | null | undefined, index: number): string {
  const base = String(fileName ?? '').split(/[\\/]/).pop() ?? '';
  const clean = base.replace(/[\u0000-\u001f\u007f-\u009f​-‏‪-‮⁦-⁩]/g, '').trim();
  const stem = clean.replace(/\.[A-Za-z0-9]{1,5}$/, '');
  const name = [...(stem || `снимка-${index + 1}`)].slice(0, 76).join('');
  return `${name}.jpg`;
}

// ── гласът ──────────────────────────────────────────────────────────────────

/** Таванът на сървъра (voice.MAX_AUDIO_SECONDS = 120); записът спира сам. */
export const MAX_RECORDING_MS = 120 * 1000;

/** mime на записа: на телефона — m4a (AAC); в браузъра — каквото MediaRecorder
 *  е дал (Chrome webm/opus, Safari mp4), без `;codecs=…`. */
export function recordingMime(platform: string, blobType?: string): string {
  if (platform !== 'web') return 'audio/m4a';
  const base = String(blobType ?? '').split(';', 1)[0].trim().toLowerCase();
  if (base === 'audio/mp4' || base === 'audio/x-m4a' || base === 'audio/m4a' || base === 'video/mp4') return 'audio/mp4';
  if (base === 'audio/ogg') return 'audio/ogg';
  if (base === 'audio/wav' || base === 'audio/x-wav') return 'audio/wav';
  return 'audio/webm';
}

/** „0:07 / 2:00“ */
export function formatClock(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
}

export type WebMicEnv = { isSecureContext?: boolean; hasGetUserMedia: boolean; hasMediaRecorder: boolean };

/** Браузърът дава микрофона само в защитен контекст (https или localhost) —
 *  `genesis serve --web` в LAN е обикновен http, и там бутонът не се
 *  показва, вместо да гърми при натискане (expo-audio, Notes on web usage). */
export function webMicAvailable(env: WebMicEnv): boolean {
  return env.isSecureContext === true && env.hasGetUserMedia && env.hasMediaRecorder;
}

/** Текстът от диктовката в полето: след написаното, с интервал. */
export function appendDictation(draft: string, heard: string): string {
  const text = heard.trim();
  if (!text) return draft;
  if (!draft.trim()) return text;
  return /\s$/.test(draft) ? draft + text : `${draft} ${text}`;
}

/** Кой отговор да се прочете на глас, щом ходът свърши: последният отговор
 *  на асистента след `afterSeq` (ключът на елемента е seq-ът му). Историята
 *  отпреди включването не се чете — само новото (2026-10-15). */
export function replyToRead(items: readonly { kind: string; key: string; text?: string }[], afterSeq: number):
  { seq: number; text: string } | null {
  for (let i = items.length - 1; i >= 0; i--) {
    const item = items[i];
    const seq = Number(item.key);
    if (!(seq > afterSeq)) return null;
    if (item.kind === 'assistant' && (item.text ?? '').trim()) return { seq, text: item.text ?? '' };
  }
  return null;
}

/** Най-големият seq в списъка — оттук нататък е „ново“. */
export function lastSeqOf(items: readonly { key: string }[]): number {
  let max = 0;
  for (const item of items) {
    const seq = Number(item.key);
    if (seq > max) max = seq;
  }
  return max;
}
