import type { GenesisEvent, ImageRef, Todo } from '../../shared/types';

export type ChatItem =
  | { kind: 'user'; key: string; text: string; ts: number; images?: ImageRef[] }
  | { kind: 'assistant'; key: string; text: string; ts: number }
  | { kind: 'tool'; key: string; name: string; result: string; clipped: boolean; ts: number }
  | { kind: 'note'; key: string; tone: 'info' | 'warn' | 'error' | 'asked' | 'progress'; text: string; ts: number }
  | { kind: 'confirm'; key: string; id: string; operation: string; reasons: string[];
      state: 'pending' | 'allowed' | 'denied'; note: string; ts: number;
      /** What „За тази сесия“ remembers; '' = the agent does not offer it. */
      sessionRule: string; remembered: boolean };

export type ConfirmChoice = 'deny' | 'allow' | 'session';

/** The buttons of a pending confirmation (same rule as mobile/src/lib/chat.ts):
 *  the third one only when the agent said the command can be remembered. */
export function confirmChoices(item: Extract<ChatItem, { kind: 'confirm' }>): ConfirmChoice[] {
  if (item.state !== 'pending') return [];
  return item.sessionRule ? ['allow', 'session', 'deny'] : ['allow', 'deny'];
}

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
        // Картинките — само име и размер (server 2026-10-15); байтовете не идват.
        items.push({ kind: 'user', key, text: e.text ?? '', ts,
                     ...(Array.isArray(e.images) && e.images.length ? { images: e.images } : {}) });
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
      case 'progress':   // a sub-agent's step (server 2026-10-09)
        items.push({ kind: 'note', key, tone: e.type, text: e.text ?? '', ts });
        break;
      case 'confirm':
        confirms.set(e.id ?? '', items.length);
        items.push({ kind: 'confirm', key, id: e.id ?? '', operation: e.operation ?? '',
                     reasons: e.reasons ?? [], state: 'pending', note: '', ts,
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

/** Merge a batch into what we have: by seq, no duplicates, in order. */
export function mergeEvents(current: GenesisEvent[], incoming: GenesisEvent[], reset: boolean): GenesisEvent[] {
  if (reset) return [...incoming].sort((a, b) => a.seq - b.seq);
  const last = current.length ? current[current.length - 1].seq : 0;
  const fresh = incoming.filter((e) => e.seq > last);
  return fresh.length ? [...current, ...fresh] : current;
}

/** The running turn: its latest "thinking" label (or a sub-agent's step —
 *  a sub-agent works for minutes and "Мисли…" said nothing) and when it started. */
export function turnInfo(events: GenesisEvent[]): { label: string; since: number } {
  let label = '';
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if ((e.type === 'thinking' || e.type === 'progress') && !label) {
      label = (e.type === 'thinking' ? e.label : e.text)?.trim() ?? '';
    }
    if (e.type === 'user') return { label: label || 'Мисли…', since: e.ts };
  }
  return { label: label || 'Мисли…', since: Date.now() };
}

/** The task list in one line: "2/5 · пусни тестовете" (the item in work). */
export function todoSummary(todos: Todo[]): string {
  const done = todos.filter((t) => t.status === 'completed').length;
  const current = todos.find((t) => t.status === 'in_progress') ?? todos.find((t) => t.status === 'pending');
  return `${done}/${todos.length}${current ? ` · ${current.content}` : ''}`;
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
export function toolHeader(name: string, result: string, workspace?: string): { target: string; extra: string; failed: boolean } {
  const first = (result.split(/\r?\n/, 1)[0] ?? '').trim();
  const upper = name.toUpperCase();
  const m = /^\[([A-Z_]+):\s*([\s\S]*?)\]\s*(.*)$/.exec(first);
  // Многоредова команда (`python -c "…"`) затваря скобата и казва rc=N чак
  // на по-долен ред (2026-11-20, на живо: червената грешка не се виждаше).
  const rc = /\(rc=(-?\d+)\)/.exec(first) ?? /^\[[A-Z_]+:[\s\S]*?\]\s*\(rc=(-?\d+)\)/.exec(result.trim());
  const failed = (rc !== null && rc[1] !== '0') || /^(✗|❌|error|грешка|traceback)/i.test(result.trim());
  if (m && m[1] === upper) {
    const extra = relativeTo(m[3].replace(/\(rc=0\)/, '').replace(/^✓\s*/, '').trim(), workspace);
    return { target: relativeTo(stripCd(m[2], workspace), workspace), extra: extra.length > 80 ? `${extra.slice(0, 79)}…` : extra, failed };
  }
  const open = /^\[([A-Z_]+):\s*(.*)$/.exec(first);
  if (open && open[1] === upper) {
    // 2026-11-20: картата показваше „RUN_CMD [RUN_CMD: cd C:\…\ && python -c "“.
    return { target: `${relativeTo(stripCd(open[2], workspace), workspace).trim()} …`, extra: '', failed };
  }
  // `[TASK_UPDATE] ✓ Нишка #20 обновена.` — the tool's own tag is already the card's name;
  // „приемни тестове: …“ (2026-11-20) — and so is its own name.
  let text = result.trimStart();
  if (text.startsWith(`[${upper}]`)) text = text.slice(upper.length + 2);
  else if (text.toLowerCase().startsWith(`${name.toLowerCase()}:`)) text = text.slice(name.length + 1);
  return { target: summarize(text), extra: '', failed };
}

/** A regex source for the folder, with either slash (`C:\x` and `c:/x`). */
function rootPattern(root: string): string {
  return root.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/\\\\|\//g, '[\\\\/]');
}

/**
 * `cd C:\…\the-open-folder && pytest -q` → `pytest -q`: the agent runs every
 * command there anyway (2026-11-20, на живо: половината карта беше пътят).
 */
export function stripCd(cmd: string, workspace?: string): string {
  const root = workspace?.replace(/[\\/]+$/, '');
  if (!root) return cmd;
  const re = new RegExp(`^\\s*cd\\s+(?:/d\\s+)?"?${rootPattern(root)}[\\\\/]?"?\\s*(?:&&|;)\\s*`, 'i');
  return cmd.replace(re, '');
}

/** The card's title: „Чете“, „Изпълнява“… — an unknown tool keeps its name. */
export function toolTitle(name: string): string {
  if (!/^[A-Z][A-Z0-9_]*$/.test(name)) return name;
  const { label } = toolLook(name);
  return label === 'Инструмент' ? name : label;
}

/** „RUN_CMD: cd C:\…\x && pytest -q“ (tool_start) → „Изпълнява: pytest -q“. */
export function thinkingLabel(label: string, workspace?: string): string {
  const m = /^([A-Z][A-Z0-9_]{2,}):\s*(.*)/.exec(label.trim());
  if (!m) return label;
  return `${toolTitle(m[1])}: ${relativeTo(stripCd(m[2], workspace), workspace)}`;
}

/** Two folders are the same one whatever the slashes, case or a trailing slash. */
export function samePath(a: string, b: string): boolean {
  const norm = (p: string) => p.replace(/\//g, '\\').replace(/\\+$/, '').toLowerCase();
  return norm(a) === norm(b);
}

export type DiffLine = { kind: 'add' | 'del' | 'hunk' | 'meta' | 'plain'; text: string };

/** A result that carries a unified diff (EDIT_FILE), line by line; otherwise null. */
export function diffLines(result: string): DiffLine[] | null {
  if (!/^--- .*\r?\n\+\+\+ /m.test(result)) return null;
  return result.split(/\r?\n/).map((text) => {
    if (text.startsWith('+++ ') || text.startsWith('--- ')) return { kind: 'meta', text };
    if (text.startsWith('@@')) return { kind: 'hunk', text };
    if (text.startsWith('+')) return { kind: 'add', text };
    if (text.startsWith('-')) return { kind: 'del', text };
    return { kind: 'plain', text };
  });
}

// ── ходът като едно цяло (2026-11-20) ─────────────────────────────────────
// На живо: един ход със 7 стъпки пълнеше екрана с карти, а крайният отговор се
// губеше между тях. Сега поредните стъпки (инструменти, бележки) са една
// сгъваема група. Текстът на агента остава видим винаги — първата проба го
// скриваше в групата и най-полезният отговор („бъговете са в calc.py…“)
// изчезна зад „Работа“. Разрешенията, въпросите и грешките също са извън
// групата — те искат теб.

export type Row =
  | { kind: 'item'; item: ChatItem;
      /** Крайният отговор: колко продължи ходът (ms). */
      took?: number;
      /** Грешката, с която свърши последният ход: „Опитай пак“ праща това. */
      retry?: string }
  | { kind: 'work'; key: string; items: ChatItem[]; live: boolean };

function isWork(it: ChatItem): boolean {
  return it.kind === 'tool'
    || (it.kind === 'note' && (it.tone === 'info' || it.tone === 'warn' || it.tone === 'progress'));
}

export function buildRows(items: ChatItem[], busy: boolean): Row[] {
  const turns: ChatItem[][] = [];
  for (const it of items) {
    if (it.kind === 'user' || !turns.length) turns.push([it]);
    else turns[turns.length - 1].push(it);
  }
  const rows: Row[] = [];
  turns.forEach((turn, ti) => {
    const lastTurn = ti === turns.length - 1;
    const live = lastTurn && busy;
    const user = turn[0].kind === 'user' ? turn[0] : undefined;
    const body = user ? turn.slice(1) : turn;
    // Докато ходът върви, всеки отговор може да е междинен.
    let final = -1;
    if (!live) {
      for (let i = body.length - 1; i >= 0; i--) if (body[i].kind === 'assistant') { final = i; break; }
    }
    const out: Row[] = [];
    let group: ChatItem[] = [];
    // Група — само около инструменти. Само бележки (отговорът на /resume,
    // 2026-11-20 на живо) са самият отговор и не се сгъват.
    const asRows = (g: ChatItem[]): Row[] => (g.length > 1 && g.some((x) => x.kind === 'tool')
      ? [{ kind: 'work', key: `w${g[0].key}`, items: g, live: false }]
      : g.map((x) => ({ kind: 'item' as const, item: x })));
    const flush = () => {
      out.push(...asRows(group));
      group = [];
    };
    body.forEach((it, i) => {
      if (i === final) {
        flush();
        const end = Math.max(...turn.map((t) => t.ts));
        out.push({ kind: 'item', item: it, ...(user && end > user.ts ? { took: end - user.ts } : {}) });
      } else if (isWork(it)) group.push(it);
      else { flush(); out.push({ kind: 'item', item: it }); }
    });
    // Проверките след крайния отговор отиват при работата преди него.
    const at = out.length - 1;
    // Само бележки след отговора остават след него — те са за теб.
    const lastIsFinal = out[at]?.kind === 'item' && (out[at] as { item: ChatItem }).item === body[final];
    if (final >= 0 && lastIsFinal && group.some((x) => x.kind === 'tool')) {
      const prev = out[at - 1];
      if (prev?.kind === 'work') prev.items.push(...group);
      else if (prev?.kind === 'item' && prev.item.kind === 'tool') out.splice(at - 1, 1, ...asRows([prev.item, ...group]));
      else out.splice(at, 0, ...asRows(group));
      group = [];
    }
    flush();
    if (live) {
      const w = [...out].reverse().find((r) => r.kind === 'work');
      if (w && w.kind === 'work') w.live = true;
    }
    if (lastTurn && !busy && user && final < 0) {
      const err = [...out].reverse().find((r) => r.kind === 'item' && r.item.kind === 'note' && r.item.tone === 'error');
      // Само текст: картинките не се пазят (идват като име и размер) и повторението
      // щеше да ги изгуби мълчаливо — с картинки бутон няма (2026-11-23, преглед на PR #131).
      if (err && err.kind === 'item' && user.kind === 'user' && user.text && !user.images?.length) err.retry = user.text;
    }
    if (user) rows.push({ kind: 'item', item: user });
    rows.push(...out);
  });
  return rows;
}

/** What the collapsed group says: steps, the files it changed, failed steps. */
export function workSummary(items: ChatItem[], workspace?: string): { steps: number; changed: string[]; failed: number; notes: number } {
  let steps = 0, failed = 0, notes = 0;
  const changed: string[] = [];
  for (const it of items) {
    if (it.kind !== 'tool') { if (it.kind === 'assistant') notes++; continue; }
    steps++;
    const head = toolHeader(it.name, it.result, workspace);
    if (head.failed) { failed++; continue; }
    const icon = toolLook(it.name).icon;
    if ((icon === 'edit' || icon === 'save') && head.target) {
      const file = baseName(head.target.replace(/ …$/, ''));
      if (!changed.includes(file)) changed.push(file);
    }
  }
  return { steps, changed, failed, notes };
}

/**
 * Paths inside the open folder, relative to it: `calc.py`, not
 * `C:\Users\…\project\calc.py`. 2026-10-12: an EDIT_FILE card showed the full
 * path twice (target and result) and both were cut off.
 */
export function relativeTo(text: string, workspace?: string): string {
  const root = workspace?.replace(/[\\/]+$/, '');
  if (!root) return text;
  if (text.toLowerCase() === root.toLowerCase()) return baseName(root);
  return text.replace(new RegExp(`${rootPattern(root)}[\\\\/]`, 'gi'), '');
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
