import type { GenesisEvent } from './protocol';

export type ChatItem =
  | { kind: 'user'; key: string; text: string }
  | { kind: 'assistant'; key: string; text: string }
  | { kind: 'tool'; key: string; name: string; result: string; clipped: boolean }
  | { kind: 'note'; key: string; tone: 'info' | 'warn' | 'error' | 'asked'; text: string }
  | { kind: 'confirm'; key: string; id: string; operation: string; reasons: string[];
      state: 'pending' | 'allowed' | 'denied'; note: string };

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
        items.push({ kind: 'user', key, text: e.text ?? '' });
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
        items.push({ kind: 'note', key, tone: e.type, text: e.text ?? '' });
        break;
      case 'confirm':
        confirms.set(e.id ?? '', items.length);
        items.push({ kind: 'confirm', key, id: e.id ?? '', operation: e.operation ?? '',
                     reasons: e.reasons ?? [], state: 'pending', note: '' });
        break;
      case 'confirm_done': {
        const at = confirms.get(e.id ?? '');
        const item = at === undefined ? undefined : items[at];
        if (item && item.kind === 'confirm') {
          items[at as number] = { ...item, state: e.allow ? 'allowed' : 'denied', note: e.note ?? '' };
        }
        break;
      }
      default:
        break;
    }
  }
  return items;
}

/** Merge a reply into what we have: by seq, no duplicates, in order. */
export function mergeEvents(current: GenesisEvent[], incoming: GenesisEvent[], reset: boolean): GenesisEvent[] {
  if (reset) return [...incoming].sort((a, b) => a.seq - b.seq);
  const last = current.length ? current[current.length - 1].seq : 0;
  const fresh = incoming.filter((e) => e.seq > last);
  return fresh.length ? [...current, ...fresh] : current;
}

/** What the busy row says: the latest "thinking" label of the running turn. */
export function thinkingLabel(events: GenesisEvent[]): string {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.type === 'thinking') return e.label ?? '';
    if (e.type === 'user') break;
  }
  return 'Genesis мисли…';
}
