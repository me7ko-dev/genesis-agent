/**
 * Картинки и звук в прозореца (2026-10-15) — частите, които искат браузъра:
 * смаляване през canvas, base64, запис от микрофона, пускане на mp3.
 * Чистите правила (тавани, размери, грешки) са в shared/media.ts.
 */
import { useSyncExternalStore } from 'react';
import { fitSize, imageMimeOf, MAX_RECORD_MS, recorderMime, type ImageAttachment } from '../../shared/media';

export type Attachment = ImageAttachment & { id: string; width: number; height: number; bytes: number; thumb: string };

// Над толкова и без смаляване все пак прекодираме: 10 MiB общо за четири.
const KEEP_AS_IS = 2.5 * 1024 * 1024;
// PNG снимка на екрана с много детайл — над това става JPEG.
const PNG_MAX = 1.5 * 1024 * 1024;
const THUMB = 112;

export function blobToBase64(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const url = String(reader.result ?? '');
      resolve(url.slice(url.indexOf(',') + 1));
    };
    reader.onerror = () => reject(reader.error ?? new Error('read failed'));
    reader.readAsDataURL(blob);
  });
}

export function base64ToBlob(data: string, mime: string): Blob {
  const bin = atob(data);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Blob([bytes], { type: mime });
}

function canvasBlob(canvas: HTMLCanvasElement, mime: string, quality?: number): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob((b) => (b ? resolve(b) : reject(new Error('encode failed'))), mime, quality);
  });
}

function draw(bitmap: ImageBitmap, width: number, height: number, background?: string): HTMLCanvasElement {
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext('2d');
  if (!ctx) throw new Error('canvas');
  if (background) { ctx.fillStyle = background; ctx.fillRect(0, 0, width, height); }
  ctx.imageSmoothingQuality = 'high';
  ctx.drawImage(bitmap, 0, 0, width, height);
  return canvas;
}

let counter = 0;

/**
 * Файл (избран, поставен, пуснат) → картинка за `send`, смалена тук до
 * 1568 px по дългата страна: снимка от 4 MB става ~300 KB и не пътува цяла
 * до агента. Хвърля Error с българско съобщение, ако не е картинка.
 */
export async function toAttachment(file: Blob, name: string): Promise<Attachment> {
  const mime = imageMimeOf(file.type, name);
  if (!mime) throw new Error(`„${name}“ не е PNG, JPEG, WebP или GIF.`);
  let bitmap: ImageBitmap;
  try {
    bitmap = await createImageBitmap(file);   // по EXIF — снимката не ляга
  } catch {
    throw new Error(`„${name}“ не се отваря като картинка.`);
  }
  try {
    const fit = fitSize(bitmap.width, bitmap.height);
    let out: Blob = file;
    let outMime = mime;
    if (fit.scaled || file.size > KEEP_AS_IS) {
      // Прозрачност има смисъл само в PNG/WebP/GIF; JPEG — направо JPEG.
      if (mime === 'image/jpeg') {
        out = await canvasBlob(draw(bitmap, fit.width, fit.height), 'image/jpeg', 0.88);
        outMime = 'image/jpeg';
      } else {
        out = await canvasBlob(draw(bitmap, fit.width, fit.height), 'image/png');
        outMime = 'image/png';
        if (out.size > PNG_MAX) {
          out = await canvasBlob(draw(bitmap, fit.width, fit.height, '#ffffff'), 'image/jpeg', 0.9);
          outMime = 'image/jpeg';
        }
      }
    }
    const t = fitSize(bitmap.width, bitmap.height, THUMB);
    const thumb = draw(bitmap, t.width, t.height).toDataURL('image/png');
    const data = await blobToBase64(out);
    const ext = outMime === 'image/jpeg' ? 'jpg' : outMime.split('/')[1];
    const finalName = outMime === mime ? name : name.replace(/\.[a-z0-9]+$/i, '') + `.${ext}`;
    return { id: `img${++counter}`, mime: outMime, data, name: finalName, width: fit.width, height: fit.height,
             bytes: out.size, thumb };
  } finally {
    bitmap.close();
  }
}

// ── микрофонът ──────────────────────────────────────────────────────────────

export type Recording = { blob: Blob; mime: string; ms: number };

/**
 * Запис от микрофона в webm/opus (32 kbit/s: 2 минути ≈ 480 KB). Спира сам на
 * MAX_RECORD_MS — `onLimit` казва, че е спрял по тавана.
 */
export class MicRecorder {
  private stream?: MediaStream;
  private recorder?: MediaRecorder;
  private chunks: Blob[] = [];
  private startedAt = 0;
  private limit?: ReturnType<typeof setTimeout>;
  private done?: (r: Recording | undefined) => void;
  mime = '';

  /** Хвърля DOMException от getUserMedia (виж micErrorText). */
  async start(onLimit: () => void): Promise<void> {
    const mime = recorderMime((m) => MediaRecorder.isTypeSupported(m));
    if (!mime) throw new DOMException('no webm/opus', 'NotSupportedError');
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      video: false,
    });
    this.mime = mime;
    this.chunks = [];
    const rec = new MediaRecorder(this.stream, { mimeType: mime, audioBitsPerSecond: 32_000 });
    this.recorder = rec;
    rec.ondataavailable = (e) => { if (e.data.size) this.chunks.push(e.data); };
    rec.onstop = () => {
      const ms = Date.now() - this.startedAt;
      const blob = new Blob(this.chunks, { type: mime });
      this.release();
      this.done?.(blob.size ? { blob, mime, ms } : undefined);
      this.done = undefined;
    };
    rec.start(1000);
    this.startedAt = Date.now();
    this.limit = setTimeout(() => { onLimit(); }, MAX_RECORD_MS);
  }

  elapsed(): number {
    return this.startedAt ? Date.now() - this.startedAt : 0;
  }

  /** Спира и връща записа (undefined — нищо не е записано). */
  stop(): Promise<Recording | undefined> {
    const rec = this.recorder;
    if (!rec || rec.state === 'inactive') { this.release(); return Promise.resolve(undefined); }
    return new Promise((resolve) => {
      this.done = resolve;
      rec.stop();
    });
  }

  /** Отказ: записът се изхвърля, микрофонът се освобождава. */
  cancel(): void {
    this.done = undefined;
    const rec = this.recorder;
    if (rec && rec.state !== 'inactive') {
      rec.onstop = () => this.release();
      rec.stop();
    } else {
      this.release();
    }
  }

  private release(): void {
    if (this.limit) clearTimeout(this.limit);
    this.limit = undefined;
    // Без това лампичката „микрофонът се ползва“ на Windows остава да свети.
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = undefined;
    this.recorder = undefined;
  }
}

// ── пускането на глас ───────────────────────────────────────────────────────

export type PlayerState = { key: string; phase: 'loading' | 'playing' } | null;

let playerState: PlayerState = null;
let audio: HTMLAudioElement | null = null;
let audioUrl = '';
let token = 0;
const listeners = new Set<() => void>();

function setPlayer(next: PlayerState): void {
  playerState = next;
  listeners.forEach((l) => l());
}

function release(): void {
  if (audio) { audio.onended = null; audio.onerror = null; audio.pause(); }
  audio = null;
  if (audioUrl) URL.revokeObjectURL(audioUrl);
  audioUrl = '';
}

/** Спира, каквото се чете (и чакащото озвучаване). */
export function stopAudio(): void {
  token++;
  release();
  setPlayer(null);
}

/**
 * Озвучава един отговор: едно наведнъж — нов спира стария. `load` е `speak`
 * към агента; връща текста на грешката или undefined.
 */
export async function playSpeech(key: string,
  load: () => Promise<{ mime: string; data: string } | { error: string }>): Promise<string | undefined> {
  stopAudio();
  const my = token;
  setPlayer({ key, phase: 'loading' });
  const r = await load();
  if (my !== token) return undefined;          // спрян или сменен междувременно
  if ('error' in r) { setPlayer(null); return r.error; }
  audioUrl = URL.createObjectURL(base64ToBlob(r.data, r.mime));
  audio = new Audio(audioUrl);
  audio.onended = () => { if (my === token) stopAudio(); };
  audio.onerror = () => { if (my === token) stopAudio(); };
  setPlayer({ key, phase: 'playing' });
  try {
    await audio.play();
  } catch (e) {
    if (my === token) stopAudio();
    return `Звукът не тръгна: ${(e as Error).message}`;
  }
  return undefined;
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

export function usePlayer(): PlayerState {
  return useSyncExternalStore(subscribe, () => playerState);
}
