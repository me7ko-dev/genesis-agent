import type { GenesisEvent, Todo } from './protocol';

export type ChatItem =
  /** `images` — имената на изпратените снимки (събитието носи само име и
   *  размер, никога байтовете; 2026-10-15). Винаги масив, за sameItem. */
  | { kind: 'user'; key: string; text: string; images: string[] }
  | { kind: 'assistant'; key: string; text: string }
  | { kind: 'tool'; key: string; name: string; result: string; clipped: boolean }
  | { kind: 'note'; key: string; tone: 'info' | 'warn' | 'error' | 'asked' | 'progress'; text: string }
  | { kind: 'confirm'; key: string; id: string; operation: string; reasons: string[];
      state: 'pending' | 'allowed' | 'denied'; note: string;
      /** What „За тази сесия“ remembers; '' = the server does not offer it. */
      sessionRule: string; remembered: boolean };

export type ConfirmChoice = 'deny' | 'allow' | 'session';

/** The buttons of a pending confirm card. The third one only when the server
 *  said the command can be remembered (plain shell command, no secrets, never
 *  BLOCKED) — the phone does not decide that itself (2026-10-15). */
export function confirmChoices(item: Extract<ChatItem, { kind: 'confirm' }>): ConfirmChoice[] {
  if (item.state !== 'pending') return [];
  return item.sessionRule ? ['deny', 'session', 'allow'] : ['deny', 'allow'];
}

/** The server's event log → what the chat shows. Pure, so it is tested. */
export function toChatItems(events: GenesisEvent[]): ChatItem[] {
  let items: ChatItem[] = [];
  const confirms = new Map<string, number>();
  for (const e of events) {
    const key = String(e.seq);
    switch (e.type) {
      case 'cleared':
        items = [];
        confirms.clear();
        break;
      case 'user':
        items.push({ kind: 'user', key, text: e.text ?? '', images: imageNames(e) });
        break;
      case 'assistant':
        items.push({ kind: 'assistant', key, text: e.text ?? '' });
        break;
      case 'tool':
        items.push({ kind: 'tool', key, name: e.name ?? '', result: e.result ?? '', clipped: !!e.clipped });
        break;
      case 'info':
      case 'warn':
      case 'error':
      case 'asked':
      case 'progress':
        items.push({ kind: 'note', key, tone: e.type, text: e.text ?? '' });
        break;
      case 'confirm':
        confirms.set(e.id ?? '', items.length);
        items.push({ kind: 'confirm', key, id: e.id ?? '', operation: e.operation ?? '',
                     reasons: e.reasons ?? [], state: 'pending', note: '',
                     sessionRule: typeof e.session_rule === 'string' ? e.session_rule : '',
                     remembered: false });
        break;
      case 'confirm_done': {
        const at = confirms.get(e.id ?? '');
        const item = at === undefined ? undefined : items[at];
        if (item && item.kind === 'confirm') {
          items[at as number] = { ...item, state: e.allow ? 'allowed' : 'denied', note: e.note ?? '',
                                  remembered: !!e.allow && e.session === true };
        }
        break;
      }
      default:
        break;
    }
  }
  return items;
}

/** Имената от "images" на събитието "user"; без име — „снимка“. Името е
 *  от който и да е сдвоен клиент: без контролни и bidi знаци, до 60. */
function imageNames(e: GenesisEvent): string[] {
  if (!Array.isArray(e.images)) return [];
  return e.images.map((image) => {
    const raw = typeof image?.name === 'string' ? image.name : '';
    const name = raw.replace(/[\u0000-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e\u2066-\u2069]/g, '').trim();
    return [...name].slice(0, 60).join('') || 'снимка';
  });
}

/** Колкото пази и сървърът (remote_server._EVENT_LOG). */
export const MAX_EVENTS = 1000;

/** Merge a reply into what we have: by seq, no duplicates, in order. */
export function mergeEvents(current: GenesisEvent[], incoming: GenesisEvent[], reset: boolean): GenesisEvent[] {
  if (reset) return trimEvents([...incoming].sort((a, b) => a.seq - b.seq));
  const last = current.length ? current[current.length - 1].seq : 0;
  const fresh = incoming.filter((e) => e.seq > last);
  return fresh.length ? trimEvents([...current, ...fresh]) : current;
}

/** Само последните MAX_EVENTS и нищо преди последното "cleared" — то и без
 *  това не се показва. Одит 2026-10-11: телефонът пазеше всяко събитие
 *  завинаги (10 801 след ден работа, ~80 MB низове), а toChatItems минаваше
 *  през всички на всяко допитване. "cleared" остава: по него е последният seq. */
export function trimEvents(events: GenesisEvent[]): GenesisEvent[] {
  let start = Math.max(0, events.length - MAX_EVENTS);
  for (let i = events.length - 1; i > start; i--) {
    if (events[i].type === 'cleared') {
      start = i;
      break;
    }
  }
  return start ? events.slice(start) : events;
}

/** Новите елементи, но със СТАРИТЕ обекти там, където нищо не се е
 *  променило — React.memo на картите тогава не ги рисува пак, а RichText не
 *  разбира Markdown-а наново на всяко „thinking“ (одит 2026-10-11). Ако нищо
 *  не е променено — същият масив. */
export function reuseItems(previous: readonly ChatItem[], next: ChatItem[]): ChatItem[] {
  if (!previous.length) return next;
  const old = new Map(previous.map((item) => [item.key, item]));
  let same = previous.length === next.length;
  const out = next.map((item, i) => {
    const before = old.get(item.key);
    const kept = before !== undefined && sameItem(before, item) ? before : item;
    if (kept !== previous[i]) same = false;
    return kept;
  });
  return same ? (previous as ChatItem[]) : out;
}

function sameItem(a: ChatItem, b: ChatItem): boolean {
  const ra = a as Record<string, unknown>;
  const rb = b as Record<string, unknown>;
  const keys = Object.keys(ra);
  if (keys.length !== Object.keys(rb).length) return false;
  return keys.every((k) => {
    const x = ra[k];
    const y = rb[k];
    if (Array.isArray(x) && Array.isArray(y)) return x.length === y.length && x.every((v, i) => v === y[i]);
    return x === y;
  });
}

/** What the busy row says: the latest "thinking" label or sub-agent step of
 *  the running turn — a sub-agent works for minutes, "thinking…" said nothing. */
export function thinkingLabel(events: GenesisEvent[]): string {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.type === 'thinking') return e.label ?? '';
    if (e.type === 'progress') return (e.text ?? '').trim();
    if (e.type === 'user') break;
  }
  return 'Genesis мисли…';
}

/** The task list in one line: "2/5 · пусни тестовете" (the item in work). */
export function todoSummary(todos: Todo[]): string {
  const done = todos.filter((t) => t.status === 'completed').length;
  const current = todos.find((t) => t.status === 'in_progress')
    ?? todos.find((t) => t.status === 'pending');
  return `${done}/${todos.length}${current ? ` · ${current.content}` : ''}`;
}
