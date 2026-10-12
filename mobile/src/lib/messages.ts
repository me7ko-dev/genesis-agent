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
};

/** {ok:false, error} от сървъра → текст за екрана. */
export function replyErrorText(code: string | undefined): string {
  const known = code !== undefined && Object.prototype.hasOwnProperty.call(REPLY_ERRORS, code);
  return known ? REPLY_ERRORS[code as string] : 'Компютърът отказа заявката.';
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
      return e.status === 413 ? 'Съобщението е твърде голямо за компютъра.'
        : e.status ? `Грешка на компютъра (${e.status}).` : 'Компютърът върна неочакван отговор.';
    default:
      return 'Неочаквана грешка.';
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
