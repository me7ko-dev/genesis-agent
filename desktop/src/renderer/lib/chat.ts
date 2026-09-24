import type { GenesisEvent } from '../../shared/types';

export type ChatItem =
  | { kind: 'user'; key: string; text: string; ts: number }
  | { kind: 'assistant'; key: string; text: string; ts: number }
  | { kind: 'tool'; key: string; name: string; result: string; clipped: boolean; ts: number }
  | { kind: 'note'; key: string; tone: 'info' | 'warn' | 'error' | 'asked'; text: string; ts: number }
  | { kind: 'confirm'; key: string; id: string; operation: string; reasons: string[];
      state: 'pending' | 'allowed' | 'denied'; note: string; ts: number };

/** The server's event log → what the chat shows (same rules as mobile/src/lib/chat.ts). */
export function toChatItems(events: GenesisEvent[]): ChatItem[] {
  let items: ChatItem[] = [];
  const confirms = new Map<string, number>();
  for (const e of events) {
    const key = String(e.seq);
    const ts = e.ts;
    switch (e.type) {
      case 'cleared':
        items = [];
        confirms.clear();
        break;
      case 'user':
        items.push({ kind: 'user', key, text: e.text ?? '', ts });
        break;
      case 'assistant':
        items.push({ kind: 'assistant', key, text: e.text ?? '', ts });
        break;
      case 'tool':
        items.push({ kind: 'tool', key, name: e.name ?? '', result: e.result ?? '', clipped: !!e.clipped, ts });
        break;
      case 'info':
      case 'warn':
      case 'error':
      case 'asked':
        items.push({ kind: 'note', key, tone: e.type, text: e.text ?? '', ts });
        break;
      case 'confirm':
        confirms.set(e.id ?? '', items.length);
        items.push({ kind: 'confirm', key, id: e.id ?? '', operation: e.operation ?? '',
                     reasons: e.reasons ?? [], state: 'pending', note: '', ts });
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

/** Merge a batch into what we have: by seq, no duplicates, in order. */
export function mergeEvents(current: GenesisEvent[], incoming: GenesisEvent[], reset: boolean): GenesisEvent[] {
  if (reset) return [...incoming].sort((a, b) => a.seq - b.seq);
  const last = current.length ? current[current.length - 1].seq : 0;
  const fresh = incoming.filter((e) => e.seq > last);
  return fresh.length ? [...current, ...fresh] : current;
}

/** The running turn: its latest "thinking" label and when it started. */
export function turnInfo(events: GenesisEvent[]): { label: string; since: number } {
  let label = '';
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.type === 'thinking' && !label) label = e.label ?? '';
    if (e.type === 'user') return { label: label || 'Мисли…', since: e.ts };
  }
  return { label: label || 'Мисли…', since: Date.now() };
}

/** The pending confirmation, if the agent is waiting for one. */
export function pendingConfirm(items: ChatItem[]): Extract<ChatItem, { kind: 'confirm' }> | undefined {
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i];
    if (it.kind === 'confirm' && it.state === 'pending') return it;
  }
  return undefined;
}

type ToolLook = { label: string; icon: string };

/** A friendlier name and an icon for the agent's tool names (READ_FILE, RUN…). */
export function toolLook(name: string): ToolLook {
  const n = name.toUpperCase();
  if (/READ|OPEN|VIEW|CAT/.test(n)) return { label: 'Чете', icon: 'file' };
  if (/EDIT|PATCH|REPLACE/.test(n)) return { label: 'Редактира', icon: 'edit' };
  if (/WRITE|CREATE|SAVE/.test(n)) return { label: 'Записва', icon: 'save' };
  if (/LIST|GLOB|TREE|DIR|REPO_MAP/.test(n)) return { label: 'Разглежда', icon: 'folder' };
  if (/SEARCH_CODE|GREP|FIND/.test(n)) return { label: 'Търси в кода', icon: 'search' };
  if (/WEB|RESEARCH|FETCH|BROWSE|NAVIGATE|URL/.test(n)) return { label: 'Интернет', icon: 'globe' };
  if (/RUN|EXEC|SHELL|BASH|CMD|TEST/.test(n)) return { label: 'Изпълнява', icon: 'terminal' };
  if (/SKILL/.test(n)) return { label: 'Умение', icon: 'sparkles' };
  if (/MEMORY|REMEMBER|RECALL/.test(n)) return { label: 'Памет', icon: 'brain' };
  if (/GIT/.test(n)) return { label: 'Git', icon: 'git' };
  return { label: 'Инструмент', icon: 'wrench' };
}

/**
 * The agent's results start with a header like
 *   "[RUN_CMD: pytest -q] (rc=1)" or "[READ_FILE: C:\x\calc.py] (редове 1–6 от 6)".
 * Split it into what the tool was given, the rest of the header line, and
 * whether it failed (a non-zero exit code or an error mark).
 */
export function toolHeader(name: string, result: string): { target: string; extra: string; failed: boolean } {
  const first = result.split(/\r?\n/, 1)[0] ?? '';
  const m = /^\[([A-Z_]+):\s*([\s\S]*?)\]\s*(.*)$/.exec(first.trim());
  const rc = /\(rc=(-?\d+)\)/.exec(first);
  const failed = (rc !== null && rc[1] !== '0') || /^(✗|❌|error|грешка|traceback)/i.test(result.trim());
  if (!m || m[1] !== name.toUpperCase()) return { target: summarize(result), extra: '', failed };
  const extra = m[3].replace(/\(rc=0\)/, '').replace(/^✓\s*/, '').trim();
  return { target: m[2], extra: extra.length > 80 ? `${extra.slice(0, 79)}…` : extra, failed };
}

/** First meaningful line of a tool result, for the collapsed card. */
export function summarize(result: string, max = 120): string {
  const line = result.split(/\r?\n/).map((l) => l.trim()).find((l) => l.length > 0) ?? '';
  return line.length > max ? `${line.slice(0, max - 1)}…` : line;
}

export function shortPath(path: string): string {
  const parts = path.split(/[\\/]+/).filter(Boolean);
  return parts.length <= 2 ? path : `…\\${parts.slice(-2).join('\\')}`;
}

export function baseName(path: string): string {
  const parts = path.split(/[\\/]+/).filter(Boolean);
  return parts[parts.length - 1] ?? path;
}

export function elapsed(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${s % 60} s`;
}
