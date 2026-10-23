/**
 * Картинки и глас в прозореца (2026-10-15): таваните и проверките, общи за
 * прозореца и за main процеса, и текстовете на грешките от `genesis serve`
 * (genesis_agent/remote_server.py — `send` с "images", `transcribe`, `speak`).
 *
 * Тук няма DOM и Electron — само чисти функции, за `npm test`.
 */

/** Какво казва `hello`/`status` в "limits"; по-старият агент не казва нищо. */
export type MediaLimits = { images: number; images_bytes: number; body: number; large_body: number };

/** Таваните на сървъра от 2026-10-15 — когато агентът не ги казва сам. */
export const DEFAULT_LIMITS: MediaLimits = {
  images: 4,
  images_bytes: 10 * 1024 * 1024,
  body: 256 * 1024,
  large_body: 19 * 1024 * 1024,
};

/** Дългата страна след смаляването: над нея Claude (1568 px) и GPT смаляват
 *  сами, тоест повече пиксели са само мрежа и токени (genesis_agent/vision.py). */
export const MAX_SIDE = 1568;

/** Записът от микрофона: сървърът отказва над 2 минути ("too_long"). */
export const MAX_RECORD_MS = 120_000;

/** `speak` отказва над 20 000 знака ("too_long") — пращаме началото. */
export const MAX_SPEAK_CHARS = 20_000;

/** `send` отказва текст над 20 000 знака ("too_long", remote_server._send).
 *  Одит 2026-10-22: прозорецът показваше „Не се изпрати: too_long“, а над
 *  256 KiB — „Съобщението с картинките е твърде голямо.“ и без картинки. */
export const MAX_MESSAGE = 20_000;

/** От тук нататък под полето се вижда броячът. */
export const MESSAGE_HINT_FROM = 18_000;

/** Колко знака брои сървърът: Python len() — кодови точки, а не UTF-16
 *  единиците на `.length` (емоджи е 1, не 2). */
export function messageLength(text: string): number {
  let n = text.length;
  for (let i = 0; i < text.length - 1; i++) {
    const c = text.charCodeAt(i);
    if (c >= 0xd800 && c <= 0xdbff) {
      const d = text.charCodeAt(i + 1);
      if (d >= 0xdc00 && d <= 0xdfff) { n--; i++; }
    }
  }
  return n;
}

/** Броячът под полето близо до тавана; undefined — далеч е. */
export function lengthHint(text: string): string | undefined {
  if (text.length < MESSAGE_HINT_FROM) return undefined;
  const n = messageLength(text);
  if (n < MESSAGE_HINT_FROM) return undefined;
  return n > MAX_MESSAGE
    ? `${n} / ${MAX_MESSAGE} знака — твърде дълго; сложи текста във файл в папката и го посочи`
    : `${n} / ${MAX_MESSAGE} знака`;
}

/** Транскрипцията и гласът са секунди до минута (Whisper, edge-tts). */
export const VOICE_TIMEOUT_MS = 60_000;

/** Картинка, готова за `send`: base64 без `data:` отпред. */
export type ImageAttachment = { mime: string; data: string; name: string };

export const IMAGE_MIMES = ['image/png', 'image/jpeg', 'image/webp', 'image/gif'] as const;

export function limitsFrom(raw: unknown): MediaLimits {
  const r = (raw && typeof raw === 'object' ? raw : {}) as Partial<Record<keyof MediaLimits, unknown>>;
  const pick = (k: keyof MediaLimits) =>
    (typeof r[k] === 'number' && Number.isFinite(r[k]) && (r[k] as number) > 0 ? (r[k] as number) : DEFAULT_LIMITS[k]);
  return { images: pick('images'), images_bytes: pick('images_bytes'), body: pick('body'), large_body: pick('large_body') };
}

/** Колко байта са `data` след декодиране (base64 с или без `=`). */
export function base64Bytes(data: string): number {
  const clean = data.replace(/\s+/g, '');
  const pad = clean.endsWith('==') ? 2 : clean.endsWith('=') ? 1 : 0;
  return Math.max(0, Math.floor((clean.length * 3) / 4) - pad);
}

/** Размерът след смаляване до `max` по дългата страна (никога нагоре). */
export function fitSize(width: number, height: number, max = MAX_SIDE): { width: number; height: number; scaled: boolean } {
  const long = Math.max(width, height);
  if (!(long > max)) return { width, height, scaled: false };
  const k = max / long;
  return { width: Math.max(1, Math.round(width * k)), height: Math.max(1, Math.round(height * k)), scaled: true };
}

export type ImageCheck = { ok: true } | { ok: false; error: 'too_many_images' | 'images_too_large' | 'bad_image'; name?: string };

/** Същите тавани, които сървърът проверява — отказ още в прозореца, без мрежа. */
export function checkImages(images: ImageAttachment[], limits: MediaLimits = DEFAULT_LIMITS): ImageCheck {
  if (images.length > limits.images) return { ok: false, error: 'too_many_images' };
  for (const im of images) {
    if (!im.data || !(IMAGE_MIMES as readonly string[]).includes(im.mime)) return { ok: false, error: 'bad_image', name: im.name };
  }
  const total = images.reduce((n, im) => n + base64Bytes(im.data), 0);
  return total > limits.images_bytes ? { ok: false, error: 'images_too_large' } : { ok: true };
}

/**
 * Какво main приема от прозореца за `send`: само {mime, data, name} низове,
 * най-много `limits.images`. Всичко друго — undefined (заявката не тръгва).
 */
export function sanitizeImages(raw: unknown, limits: MediaLimits = DEFAULT_LIMITS): ImageAttachment[] | undefined {
  if (raw === undefined || raw === null) return [];
  if (!Array.isArray(raw) || raw.length > limits.images) return undefined;
  const out: ImageAttachment[] = [];
  for (const x of raw) {
    if (!x || typeof x !== 'object') return undefined;
    const { mime, data, name } = x as Record<string, unknown>;
    if (typeof mime !== 'string' || typeof data !== 'string' || !data) return undefined;
    out.push({ mime, data, name: typeof name === 'string' ? name.slice(0, 120) : '' });
  }
  return out;
}

/** Каква картинка е файлът — по вида, а без вид (поставено от някои
 *  програми) — по разширението. Сървърът проверява първите байтове. */
export function imageMimeOf(type: string, name: string): string | undefined {
  const t = type.toLowerCase();
  if ((IMAGE_MIMES as readonly string[]).includes(t)) return t;
  if (t === 'image/jpg') return 'image/jpeg';
  if (t) return undefined;
  const ext = /\.([a-z0-9]+)$/i.exec(name)?.[1]?.toLowerCase();
  return ext === 'png' ? 'image/png' : ext === 'jpg' || ext === 'jpeg' ? 'image/jpeg'
    : ext === 'webp' ? 'image/webp' : ext === 'gif' ? 'image/gif' : undefined;
}

/** Колко още картинки се събират и кои файлове остават отвън. */
export function takeImages<T>(have: number, files: T[], max = DEFAULT_LIMITS.images): { take: T[]; dropped: number } {
  const room = Math.max(0, max - have);
  return { take: files.slice(0, room), dropped: Math.max(0, files.length - room) };
}

/** Името на поставена от клипборда снимка — те идват като „image.png“. */
export function pastedName(mime: string, now = new Date()): string {
  const ext = mime === 'image/jpeg' ? 'jpg' : mime.split('/')[1] || 'png';
  const p = (n: number) => String(n).padStart(2, '0');
  return `екран-${now.getFullYear()}${p(now.getMonth() + 1)}${p(now.getDate())}-${p(now.getHours())}${p(now.getMinutes())}${p(now.getSeconds())}.${ext}`;
}

/** Първият формат, който MediaRecorder на Chromium може: webm/opus. */
export function recorderMime(isSupported: (mime: string) => boolean): string | undefined {
  return ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus'].find((m) => isSupported(m));
}

/** `audio/webm;codecs=opus` → `audio/webm` — каквото voice.normalize_mime приема. */
export function baseMime(mime: string): string {
  return mime.split(';', 1)[0].trim().toLowerCase() || 'audio/webm';
}

/** 75 000 ms → „1:15“. */
export function clock(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

/** Текстът за `speak`: началото, ако е над тавана на сървъра. */
export function speechText(text: string): string {
  return text.length > MAX_SPEAK_CHARS ? text.slice(0, MAX_SPEAK_CHARS) : text;
}

/** 1536 → „1,5 KB“ (за подсказката на картинката). */
export function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10 * 1024 ? 1 : 0).replace('.', ',')} KB`;
  return `${(n / 1024 / 1024).toFixed(1).replace('.', ',')} MB`;
}

type Reply = { ok?: boolean; error?: string; detail?: string; name?: string; max?: number };

/** Грешка на `send` с картинки → изречение за оператора; undefined — не е от картинките. */
export function imageErrorText(r: Reply, limits: MediaLimits = DEFAULT_LIMITS): string | undefined {
  switch (r.error) {
    case 'too_many_images': return `Най-много ${r.max ?? limits.images} картинки в едно съобщение.`;
    case 'images_too_large': return `Картинките са над ${Math.round((r.max ?? limits.images_bytes) / 1048576)} MB общо — махни някоя.`;
    case 'bad_image': {
      const what = r.name ? `„${r.name}“` : 'Картинката';
      return r.detail ? `${what} не става: ${r.detail}` : `${what} не е PNG, JPEG, WebP или GIF.`;
    }
    case 'too_large': return 'Съобщението с картинките е твърде голямо.';
    default: return undefined;
  }
}

/** Отказ на `send` → изречение за оператора. "too_large" без картинки е
 *  плик над 256 KiB, тоест текст далеч над 20 000 знака. */
export function sendErrorText(r: Reply, hasImages: boolean, limits: MediaLimits = DEFAULT_LIMITS): string {
  if (r.error === 'busy') return 'Genesis още работи.';
  if (r.error === 'old_agent') return 'Този genesis не приема картинки — обнови го (/update).';
  if (r.error === 'too_long' || (r.error === 'too_large' && !hasImages)) {
    return `Съобщението е твърде дълго — най-много ${MAX_MESSAGE} знака. Сложи дългия текст във файл в папката и го посочи.`;
  }
  return imageErrorText(r, limits) ?? `Не се изпрати: ${r.error ?? 'неизвестна грешка'}`;
}

/** Грешка на `transcribe` или `speak` → изречение за оператора. */
export function voiceErrorText(op: 'transcribe' | 'speak', r: Reply): string {
  switch (r.error) {
    case 'too_large': return 'Записът е твърде голям (над 10 MB).';
    case 'too_long': return op === 'speak' ? 'Отговорът е твърде дълъг за четене.' : 'Записът е над 2 минути.';
    case 'bad_mime': return 'Агентът не приема този аудио формат.';
    case 'empty': return op === 'speak' ? 'Няма какво да се прочете.' : 'Записът е празен — не се чу нищо.';
    case 'busy': return op === 'speak' ? 'Друг отговор се озвучава — опитай след малко.' : 'Друг запис се обработва — опитай след малко.';
    case 'rate_limited': return 'Твърде много заявки за минута — изчакай малко.';
    case 'stt_failed': return r.detail ? `Не разпознах говора: ${r.detail}` : 'Не разпознах говора.';
    case 'tts_failed': return r.detail ? `Не можах да озвуча: ${r.detail}` : 'Не можах да озвуча отговора.';
    case 'unknown_op': return 'Този genesis не знае гласа — обнови го (/update).';
    case 'not_ready': return 'Агентът още не е готов.';
    case 'bad_args': return 'Агентът не прие заявката.';
    default: return `${op === 'speak' ? 'Озвучаването' : 'Разпознаването'} не стана: ${r.detail || r.error || 'неизвестна грешка'}`;
  }
}

/** Отказът на getUserMedia → какво да направи операторът (Windows пази
 *  микрофона и за настолните приложения: Поверителност → Микрофон). */
export function micErrorText(name: string): string {
  if (name === 'NotAllowedError' || name === 'SecurityError') {
    return 'Микрофонът е забранен. Windows: Настройки → Поверителност и защита → Микрофон → „Достъп до микрофона“ и „Разреши на настолните приложения“.';
  }
  if (name === 'NotFoundError' || name === 'OverconstrainedError') return 'Не намирам микрофон.';
  if (name === 'NotReadableError' || name === 'AbortError') return 'Микрофонът е зает от друга програма.';
  return `Микрофонът не тръгна (${name || 'неизвестна грешка'}).`;
}

type Ev = { seq: number; type: string; text?: string; busy?: boolean };

/**
 * Отговорът за „Чети отговорите на глас“: последният "assistant" на ход,
 * който е свършил след `after` (събитието "busy": false). Изпуска ход с
 * "cleared" вътре — /resume и /rewind пращат цялата история наново, а тя
 * не се чете на глас. Връща и докъде е видяно, за следващия път; `key` е
 * seq на отговора (ключът на балончето в чата).
 */
export function replyToSpeak(events: Ev[], after: number): { text?: string; key?: number; seq: number } {
  let seen = after;
  let found: Ev | undefined;
  let start = -1;
  for (let i = 0; i < events.length; i++) {
    const e = events[i];
    seen = Math.max(seen, e.seq);
    if (e.type !== 'busy' || e.busy !== false) continue;
    const begin = start;
    start = i;
    if (e.seq <= after) continue;
    // Назад до началото на този ход.
    let reply: Ev | undefined;
    let cleared = false;
    for (let j = i - 1; j > begin; j--) {
      const p = events[j];
      if (p.type === 'busy' && p.busy === true) break;
      if (p.type === 'cleared') cleared = true;
      if (p.type === 'assistant' && reply === undefined && p.text?.trim()) reply = p;
    }
    found = cleared ? undefined : reply;
  }
  return { text: found?.text, key: found?.seq, seq: seen };
}
