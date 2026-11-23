// ── ходът като едно цяло, на телефона (2026-11-23) ─────────────────────────
// Същото, което на 20.11 влезе в Desktop (desktop/src/renderer/lib/chat.ts,
// desktop/tests/turns.test.ts): един ход със 7 стъпки пълнеше екрана с карти
// и крайният отговор се губеше между тях — на телефон това са 3–4 екрана
// превъртане. Поредните стъпки са една сгъваема група „Работа · N стъпки“,
// картите са без `cd <папката> &&` и с първия ред на многоредова команда.
// Копие на логиката, не общ файл: Metro не взима код извън mobile/ без
// настройка, а билдът не бива да зависи от desktop/. Двете се държат еднакви
// с тестовете (mobile/tests/turns.test.ts повтаря случаите от Desktop).
// Разлика: тук елементите нямат час (ts), затова няма „за 1 min 12 s“.
import type { ChatItem } from './chat';

type ToolLook = { label: string; icon: string; kind: 'read' | 'edit' | 'save' | 'list' | 'search' | 'web' | 'run' | 'skill' | 'memory' | 'git' | 'other' };

/** A friendlier name and an icon for the agent's tool names (READ_FILE, RUN…). */
export function toolLook(name: string): ToolLook {
  const n = name.toUpperCase();
  if (/READ|OPEN|VIEW|CAT/.test(n)) return { label: 'Чете', icon: '📄', kind: 'read' };
  if (/EDIT|PATCH|REPLACE/.test(n)) return { label: 'Редактира', icon: '✏️', kind: 'edit' };
  if (/WRITE|CREATE|SAVE/.test(n)) return { label: 'Записва', icon: '💾', kind: 'save' };
  if (/LIST|GLOB|TREE|DIR|REPO_MAP/.test(n)) return { label: 'Разглежда', icon: '📁', kind: 'list' };
  if (/SEARCH_CODE|GREP|FIND/.test(n)) return { label: 'Търси в кода', icon: '🔎', kind: 'search' };
  if (/WEB|RESEARCH|FETCH|BROWSE|NAVIGATE|URL/.test(n)) return { label: 'Интернет', icon: '🌐', kind: 'web' };
  if (/RUN|EXEC|SHELL|BASH|CMD|TEST/.test(n)) return { label: 'Изпълнява', icon: '▶', kind: 'run' };
  if (/SKILL/.test(n)) return { label: 'Умение', icon: '✨', kind: 'skill' };
  if (/MEMORY|REMEMBER|RECALL/.test(n)) return { label: 'Памет', icon: '🧠', kind: 'memory' };
  if (/GIT/.test(n)) return { label: 'Git', icon: '🌿', kind: 'git' };
  return { label: 'Инструмент', icon: '🔧', kind: 'other' };
}

/** The card's title: „Чете“, „Изпълнява“… — an unknown tool keeps its name. */
export function toolTitle(name: string): string {
  if (!/^[A-Z][A-Z0-9_]*$/.test(name)) return name;
  const { label } = toolLook(name);
  return label === 'Инструмент' ? name : label;
}

/** A regex source for the folder, with either slash (`C:\x` and `c:/x`). */
function rootPattern(root: string): string {
  return root.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/\\\\|\//g, '[\\\\/]');
}

/** `cd C:\…\the-open-folder && pytest -q` → `pytest -q`: the agent runs every command there anyway. */
export function stripCd(cmd: string, workspace?: string): string {
  const root = workspace?.replace(/[\\/]+$/, '');
  if (!root) return cmd;
  const re = new RegExp(`^\\s*cd\\s+(?:/d\\s+)?"?${rootPattern(root)}[\\\\/]?"?\\s*(?:&&|;)\\s*`, 'i');
  return cmd.replace(re, '');
}

/** Paths inside the open folder, relative to it: `calc.py`, not `C:\Users\…\project\calc.py`. */
export function relativeTo(text: string, workspace?: string): string {
  const root = workspace?.replace(/[\\/]+$/, '');
  if (!root) return text;
  if (text.toLowerCase() === root.toLowerCase()) return baseName(root);
  return text.replace(new RegExp(`${rootPattern(root)}[\\\\/]`, 'gi'), '');
}

export function baseName(path: string): string {
  const parts = path.split(/[\\/]+/).filter(Boolean);
  return parts[parts.length - 1] ?? path;
}

/** First meaningful line of a tool result, for the collapsed card. */
export function summarize(result: string, max = 120): string {
  const line = result.split(/\r?\n/).map((l) => l.trim()).find((l) => l.length > 0) ?? '';
  return line.length > max ? `${line.slice(0, max - 1)}…` : line;
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
  // Многоредова команда (`python -c "…"`) затваря скобата и казва rc=N чак на по-долен ред.
  const rc = /\(rc=(-?\d+)\)/.exec(first) ?? /^\[[A-Z_]+:[\s\S]*?\]\s*\(rc=(-?\d+)\)/.exec(result.trim());
  const failed = (rc !== null && rc[1] !== '0') || /^(✗|❌|error|грешка|traceback)/i.test(result.trim());
  if (m && m[1] === upper) {
    const extra = relativeTo(m[3].replace(/\(rc=0\)/, '').replace(/^✓\s*/, '').trim(), workspace);
    return { target: relativeTo(stripCd(m[2], workspace), workspace), extra: extra.length > 80 ? `${extra.slice(0, 79)}…` : extra, failed };
  }
  const open = /^\[([A-Z_]+):\s*(.*)$/.exec(first);
  if (open && open[1] === upper) {
    return { target: `${relativeTo(stripCd(open[2], workspace), workspace).trim()} …`, extra: '', failed };
  }
  // `[TASK_UPDATE] ✓ Нишка #20 обновена.` — the tool's own tag is already the card's name.
  let text = result.trimStart();
  if (text.startsWith(`[${upper}]`)) text = text.slice(upper.length + 2);
  else if (text.toLowerCase().startsWith(`${name.toLowerCase()}:`)) text = text.slice(name.length + 1);
  return { target: summarize(text), extra: '', failed };
}

/** „RUN_CMD: cd C:\…\x && pytest -q“ (tool_start) → „Изпълнява: pytest -q“. */
export function stepLabel(label: string, workspace?: string): string {
  const m = /^([A-Z][A-Z0-9_]{2,}):\s*(.*)/.exec(label.trim());
  if (!m) return label;
  return `${toolTitle(m[1])}: ${relativeTo(stripCd(m[2], workspace), workspace)}`;
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

export type Row =
  | { kind: 'item'; key: string; item: ChatItem;
      /** Грешката, с която свърши последният ход: „Опитай пак“ праща това. */
      retry?: string }
  | { kind: 'work'; key: string; items: ChatItem[]; live: boolean };

function isWork(it: ChatItem): boolean {
  return it.kind === 'tool'
    || (it.kind === 'note' && (it.tone === 'info' || it.tone === 'warn' || it.tone === 'progress'));
}

const row = (item: ChatItem): Row => ({ kind: 'item', key: item.key, item });

/**
 * Елементите на чата → редовете на списъка. Текстът на агента, разрешенията,
 * въпросите и грешките остават видими; поредните инструменти (и бележките
 * между тях) са една група. Същите правила като buildRows в Desktop.
 */
export function buildRows(items: readonly ChatItem[], busy: boolean): Row[] {
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
    // Група — само около инструменти. Само бележки (отговорът на /resume) са
    // самият отговор и не се сгъват.
    const asRows = (g: ChatItem[]): Row[] => (g.length > 1 && g.some((x) => x.kind === 'tool')
      ? [{ kind: 'work', key: `w${g[0].key}`, items: g, live: false }]
      : g.map(row));
    const flush = () => {
      out.push(...asRows(group));
      group = [];
    };
    body.forEach((it, i) => {
      if (i === final) { flush(); out.push(row(it)); }
      else if (isWork(it)) group.push(it);
      else { flush(); out.push(row(it)); }
    });
    // Проверките след крайния отговор отиват при работата преди него; само
    // бележки след отговора остават след него — те са за теб.
    const at = out.length - 1;
    const last = out[at];
    const lastIsFinal = last?.kind === 'item' && last.item === body[final];
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
    if (lastTurn && !busy && user && user.kind === 'user' && final < 0) {
      const err = [...out].reverse().find((r) => r.kind === 'item' && r.item.kind === 'note' && r.item.tone === 'error');
      // Само текст: снимките не се пазят (идват като име) и повторението щеше да ги
      // изгуби мълчаливо — със снимки бутон няма, както в Desktop (преглед на PR #131).
      if (err && err.kind === 'item' && user.text && !user.images.length) err.retry = user.text;
    }
    if (user) rows.push(row(user));
    rows.push(...out);
  });
  return rows;
}

/** What the collapsed group says: steps, the files it changed, failed steps. */
export function workSummary(items: readonly ChatItem[], workspace?: string): { steps: number; changed: string[]; failed: number } {
  let steps = 0, failed = 0;
  const changed: string[] = [];
  for (const it of items) {
    if (it.kind !== 'tool') continue;
    steps++;
    const head = toolHeader(it.name, it.result, workspace);
    if (head.failed) { failed++; continue; }
    const kind = toolLook(it.name).kind;
    if ((kind === 'edit' || kind === 'save') && head.target) {
      const file = baseName(head.target.replace(/ …$/, ''));
      if (!changed.includes(file)) changed.push(file);
    }
  }
  return { steps, changed, failed };
}

/** Заглавието на сгънатата група: „3 стъпки · промени calc.py“. */
export function workTitle(items: readonly ChatItem[], workspace?: string): string {
  const sum = workSummary(items, workspace);
  return [
    sum.steps ? `${sum.steps} ${sum.steps === 1 ? 'стъпка' : 'стъпки'}` : `${items.length} бележки`,
    sum.changed.length ? `промени ${sum.changed.slice(0, 2).join(', ')}${sum.changed.length > 2 ? ` и още ${sum.changed.length - 2}` : ''}` : '',
  ].filter(Boolean).join(' · ');
}

/**
 * Редовете, но със СТАРИТЕ обекти там, където нищо не се е променило — като
 * chat.reuseItems: FlatList и memo на картите тогава не рисуват пак целия
 * чат на всяко „thinking“.
 */
export function reuseRows(previous: readonly Row[], next: Row[]): Row[] {
  if (!previous.length) return next;
  const old = new Map(previous.map((r) => [r.key, r]));
  return next.map((r) => {
    const before = old.get(r.key);
    if (!before || before.kind !== r.kind) return r;
    if (r.kind === 'item' && before.kind === 'item') return before.item === r.item && before.retry === r.retry ? before : r;
    if (r.kind === 'work' && before.kind === 'work') {
      return before.live === r.live && before.items.length === r.items.length && before.items.every((x, i) => x === r.items[i]) ? before : r;
    }
    return r;
  });
}
