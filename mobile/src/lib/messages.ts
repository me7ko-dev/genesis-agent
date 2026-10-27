/**
 * Какво вижда операторът при грешка — на български, без кодове като
 * "too_long" или "server error 413" (одит 2026-10-11: тези текстове стигаха
 * до екрана дословно, на английски). e.message остава за дневника.
 */
// Само типът: файлът се зарежда и от тестовете под Node, където './protocol'
// без разширение не се намира.
import type { ProtocolError } from './protocol';

/** Таванът на сървъра за едно съобщение (remote_server._send, 20 000). */
export const MAX_MESSAGE = 20000;

function isProtocolError(e: unknown): e is ProtocolError {
  return e instanceof Error && typeof (e as { kind?: unknown }).kind === 'string';
}

const REPLY_ERRORS: Record<string, string> = {
  busy: 'Genesis още работи по предишното.',
  too_long: `Съобщението е твърде дълго — най-много ${MAX_MESSAGE} знака.`,
  empty: 'Съобщението е празно.',
  unknown_op: 'Genesis на компютъра е по-стар от приложението — обнови го.',
  bad_args: 'Компютърът не разбра заявката — обнови приложението или Genesis.',
  // Снимките (2026-10-15): сървърът ги проверява преди хода.
  too_many_images: 'Най-много 4 снимки в едно съобщение.',
  images_too_large: 'Снимките са твърде големи — най-много 10 MB общо.',
  bad_image: 'Компютърът не прочете снимката.',
  // Плик над 256 KiB за операция, която не го приема (2026-10-15).
  too_large: 'Съобщението е твърде голямо за компютъра.',
};

/** {ok:false, error} от сървъра → текст за екрана. */
export function replyErrorText(code: string | undefined): string {
  const known = code !== undefined && Object.prototype.hasOwnProperty.call(REPLY_ERRORS, code);
  return known ? REPLY_ERRORS[code as string] : 'Компютърът отказа заявката.';
}

/** Отказ на `send` с подробности: "bad_image" казва коя снимка и защо (на
 *  български от сървъра), "too_many_images" — колко са позволени. */
export function sendErrorText(reply: { error?: string; name?: string; max?: number; detail?: string }): string {
  if (reply.error === 'bad_image') {
    const what = reply.name ? `Компютърът не прочете „${reply.name}“` : 'Компютърът не прочете снимката';
    return reply.detail ? `${what}: ${reply.detail}` : `${what}.`;
  }
  if (reply.error === 'too_many_images' && typeof reply.max === 'number') {
    return `Най-много ${reply.max} снимки в едно съобщение.`;
  }
  return replyErrorText(reply.error);
}

const VOICE_ERRORS: Record<string, string> = {
  too_large: 'Записът е твърде голям — най-много 10 MB.',
  too_long: 'Записът е над 2 минути — раздели го на по-кратки.',
  bad_mime: 'Компютърът не познава формата на записа.',
  empty: 'Записът е празен — не се чу нищо.',
  busy: 'Компютърът още обработва предишния запис — опитай след малко.',
  rate_limited: 'Твърде много записи за минута — изчакай малко.',
  stt_failed: 'Говорът не стана текст.',
  tts_failed: 'Отговорът не можа да се изговори.',
  unknown_op: 'Genesis на компютъра не поддържа глас — обнови го.',
  bad_args: 'Компютърът не разбра заявката — обнови приложението или Genesis.',
};

/** Отказ на `transcribe`/`speak` → текст за екрана (2026-10-15). "too_long" и
 *  "empty" значат друго, отколкото при `send`; "detail" на
 *  "stt_failed"/"tts_failed" е на български от компютъра — показва се. */
export function voiceErrorText(op: 'transcribe' | 'speak', reply: { error?: string; detail?: string }): string {
  const code = reply.error;
  if (op === 'speak' && code === 'too_long') return 'Отговорът е твърде дълъг за четене на глас.';
  if (op === 'speak' && code === 'empty') return 'В отговора няма какво да се прочете.';
  if (op === 'speak' && code === 'busy') return 'Компютърът още изговаря предишния отговор — опитай след малко.';
  const known = code !== undefined && Object.prototype.hasOwnProperty.call(VOICE_ERRORS, code);
  const text = known ? VOICE_ERRORS[code as string] : 'Компютърът отказа заявката.';
  return (code === 'stt_failed' || code === 'tts_failed') && reply.detail ? `${text} ${reply.detail}` : text;
}

/** Хвърлена грешка (мрежа, ключ, часовник…) → текст за екрана. */
export function errorText(e: unknown): string {
  if (!isProtocolError(e)) return 'Неочаквана грешка в приложението.';
  switch (e.kind) {
    case 'network':
      return e.status === 429
        ? 'Компютърът временно отказва заявки от този адрес след много неуспешни опити — изчакай минута.'
        : 'Няма връзка с компютъра. В една Wi-Fi мрежа ли сте? Опитай пак.';
    case 'unauthorized':
      return 'Компютърът не прие ключа на този телефон.';
    case 'clock':
      return 'Часовниците на телефона и компютъра се разминават с над 5 минути.';
    case 'protocol':
      return 'Отговорът на компютъра не можа да бъде проверен.';
    case 'server':
      // 429 "busy": сървърът чете друг голям плик (2026-10-15).
      if (e.status === 429 && e.code === 'busy') return 'Компютърът приема друг голям файл в момента — опитай след малко.';
      return e.status === 413 ? 'Съобщението е твърде голямо за компютъра.'
        : e.status ? `Грешка на компютъра (${e.status}).` : 'Компютърът върна неочакван отговор.';
    default:
      return 'Неочаквана грешка.';
  }
}

/** Неуспешно сдвояване → текст за екрана „Сдвои“.
 *
 *  Одит 2026-10-27: екранът делеше грешките на три — всяка не-ProtocolError
 *  (записът в SecureStore) и 429 бяха „Не стигам до … защитната стена“, а
 *  'server' (друго нещо на порта, 500) — „не прие ключа“. Измерено срещу
 *  истинския remote_server: стар код след `genesis serve --reset`, пробван
 *  31 пъти за минута, получава 429 — компютърът отговаря, а екранът пращаше
 *  оператора да оправя Wi-Fi и защитната стена. */
export function pairErrorText(e: unknown, base: string): string {
  if (!isProtocolError(e)) return errorText(e);
  switch (e.kind) {
    case 'network':
      // 429 — спирачката след много откази: компютърът е тук.
      return e.status === 429 ? errorText(e)
        : `Не стигам до ${base}. Телефонът и компютърът в една Wi-Fi мрежа ли са? На Windows защитната стена трябва да пуска „genesis“.`;
    case 'clock':
      return 'Часовникът на телефона или компютъра е разминат с повече от 5 минути.';
    case 'server':
      return errorText(e);
    default:
      return 'Компютърът не прие ключа. Сканирай кода отново (или `genesis serve --reset` е сменил ключа).';
  }
}

// Python str.strip() маха и \x1c–\x1f и \x85, които JS trim() пази: такова
// „съобщение“ минаваше проверката тук и сървърът го намираше празно (одит
// 2026-10-11).
const EDGE_SPACE = /^[\s\x1c-\x1f\x85]+|[\s\x1c-\x1f\x85]+$/g;

/** Текстът за изпращане: без празното по краищата, както го вижда сървърът. */
export function trimDraft(text: string): string {
  return text.replace(EDGE_SPACE, '');
}

/** Броят знаци, както ги брои сървърът (code points, не UTF-16). */
export function messageLength(text: string): number {
  let n = 0;
  for (const _ of text) n++;
  return n;
}
