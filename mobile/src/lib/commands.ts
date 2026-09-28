// `/` commands — the same as Genesis Desktop (desktop/src/renderer), answered by
// the agent itself (genesis_agent/desktop_commands.py, op "command").

export type SlashCommand = { name: string; aliases?: string[]; args?: string; hint: string; group: string; pcOnly?: boolean };

export const COMMANDS: SlashCommand[] = [
  { group: 'Разход', name: '/usage', aliases: ['/cost', '/разход'], hint: 'Колко е похарчено: токени, пари, квоти, по дни' },
  { group: 'Разход', name: '/status', aliases: ['/статус'], hint: 'Модел, контекст и тази сесия' },
  { group: 'Модели', name: '/model', aliases: ['/agent', '/модел'], hint: 'Избери доставчик и модел' },
  { group: 'Модели', name: '/models', hint: 'Веригата от резервни модели' },
  { group: 'Модели', name: '/maxcoding', aliases: ['/макскод'], hint: 'Вкл./изкл. най-силните безплатни модели за код' },
  { group: 'Модели', name: '/local_model_max', aliases: ['/локален_макс'], hint: 'Вкл./изкл. само локален qwen3:14b', pcOnly: true },
  { group: 'Модели', name: '/local_model_normal', aliases: ['/локален_нормал'], hint: 'Вкл./изкл. само локален qwen2.5-coder:7b', pcOnly: true },
  { group: 'Разговор', name: '/clear', aliases: ['/нов'], hint: 'Нов разговор' },
  { group: 'Разговор', name: '/history', aliases: ['/история'], hint: 'Стари разговори: прегледай и зареди' },
  { group: 'Разговор', name: '/stop', hint: 'Спри текущата задача' },
  { group: 'Работа', name: '/tasks', aliases: ['/задачи', '/state'], hint: 'Отворени нишки и решения' },
  { group: 'Работа', name: '/done', args: '<номер>', aliases: ['/готово'], hint: 'Затвори нишка като готова' },
  { group: 'Работа', name: '/drop', args: '<номер>', hint: 'Изхвърли нишка' },
  { group: 'Работа', name: '/skills', aliases: ['/умения'], hint: 'Уменията на Genesis' },
  { group: 'Система', name: '/keys', aliases: ['/ключове'], hint: 'Ключовете за моделите от компютъра (QR)' },
  { group: 'Система', name: '/backup', hint: 'Архивирай работната папка в GENESIS_BACKUP_DIR' },
  { group: 'Система', name: '/update', aliases: ['/ъпдейт'], hint: 'Има ли нова версия на Genesis' },
  { group: 'Система', name: '/help', aliases: ['/помощ'], hint: 'Всички команди' },
];

export function visibleCommands(onPhone: boolean): SlashCommand[] {
  return COMMANDS.filter((c) => !(onPhone && c.pcOnly));
}

export function findCommand(word: string, onPhone: boolean): SlashCommand | undefined {
  const w = word.toLowerCase();
  return visibleCommands(onPhone).find((c) => c.name === w || c.aliases?.includes(w));
}

/** What the `/` menu shows for a draft that starts with `/` (no space yet). */
export function matchCommands(draft: string, onPhone: boolean): SlashCommand[] {
  if (!draft.startsWith('/') || /\s/.test(draft)) return [];
  const q = draft.toLowerCase();
  return visibleCommands(onPhone).filter((c) => c.name.startsWith(q) || c.aliases?.some((a) => a.startsWith(q)));
}

/** "/done 3" → ["/done", "3"]; "hello" → null. */
export function parseSlash(text: string): [string, string] | null {
  const t = text.trim();
  if (!t.startsWith('/')) return null;
  const sp = t.search(/\s/);
  return sp < 0 ? [t, ''] : [t.slice(0, sp), t.slice(sp + 1).trim()];
}

// ── what the agent answers (same shapes as desktop/src/shared/types.ts) ──────

export type CommandResult = { ok: boolean; error?: string; [k: string]: unknown };

export type UsagePeriod = { calls: number; tokens: number; prompt: number; completion: number; cached: number };

export type UsageReport = CommandResult & {
  days: number;
  periods: { today: UsagePeriod; week: UsagePeriod; month: UsagePeriod; all: UsagePeriod };
  daily: { day: string; calls: number; tokens: number }[];
  providers: { provider: string; name: string; calls: number; tokens: number; week: number; today: number; free: boolean }[];
  models: { provider: string; model: string; calls: number; tokens: number; free: boolean }[];
  paid_tokens: number;
  quotas: { provider: string; name: string; limit: number; spent: number; left: number; label: string; period: string }[];
  since: string | null;
};

export type StatusReport = CommandResult & {
  version: string;
  provider: string;
  provider_name: string;
  model: string;
  free: boolean;
  coding_mode: boolean;
  local_only: string | null;
  elapsed: string;
  session_input_tokens: number;
  session_output_tokens: number;
  context_used: number;
  context_window: number;
  messages: number;
  workspace: string;
};

export type ModelsReport = CommandResult & {
  chain: { provider: string; name: string; model: string; free: boolean; active: boolean }[];
  providers: { provider: string; name: string; ready: boolean; hint: string; active: boolean }[];
  current: { provider: string; model: string };
};

export type ProviderModels = CommandResult & {
  provider: string;
  models: { model: string; free: boolean; active: boolean }[];
};

export type HistoryReport = CommandResult & {
  sessions: { file: string; modified: number; messages: number; preview: string }[];
};

export type UpdateReport = CommandResult & {
  version: string;
  up_to_date: boolean | null;
  current: string | null;
  ref: string | null;
  latest: string | null;
  changes: string[];
  command: string | null;
  /** On the phone the app updates Genesis itself (`genesis phone update`). */
  self_update?: boolean;
};

export type SheetView =
  | { view: 'usage' }
  | { view: 'status' }
  | { view: 'models'; tab: 'pick' | 'chain' }
  | { view: 'history' }
  | { view: 'update' }
  | { view: 'help' }
  | { view: 'text'; title: string; command: string; arg?: Record<string, unknown> };

export function explain(error?: string): string {
  if (error === 'old_agent' || error === 'unknown op') {
    return 'Тази версия на Genesis не знае командите. Обнови Genesis (/update).';
  }
  if (error === 'busy') return 'Genesis работи по задача. Изчакай да свърши или я спри с ■.';
  return error || 'Нещо се обърка.';
}

export function toggleMessage(name: string, r: { on?: boolean; model?: string | null }): string {
  if (name === 'maxcoding') return r.on ? 'Кодинг режимът е включен: най-силните безплатни модели за код.' : 'Кодинг режимът е изключен.';
  return r.on ? `Локален режим: само ${r.model}, облакът не се ползва.` : 'Локалният режим е изключен, обратно към облака.';
}

// ── numbers, Bulgarian style, without Intl (Hermes on older phones) ───────────

const dec = (v: number, d: number) => v.toFixed(d).replace('.', ',');

/** 1234 → "1,2K", 2795326 → "2,80M". */
export function fmtTokens(n: number): string {
  if (n < 1000) return String(Math.round(n));
  if (n < 999_500) return `${dec(n / 1000, n < 9_950 ? 1 : 0)}K`;
  return `${dec(n / 1_000_000, 2)}M`;
}

/** 1234567 → "1 234 567". */
export function full(n: number): string {
  return String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
}

/** The chart's top line: the next 1, 2, 2.5 or 5 × 10ⁿ at or above `v`. */
export function niceMax(v: number): number {
  const exp = 10 ** Math.floor(Math.log10(Math.max(1, v)));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * exp >= v) return m * exp;
  return 10 * exp;
}

const MONTHS = ['ян', 'фев', 'мар', 'апр', 'май', 'юни', 'юли', 'авг', 'сеп', 'окт', 'ное', 'дек'];
const WEEKDAYS = ['нд', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'];

/** "2026-09-28" → "28 сеп" (long: "пн, 28 сеп"). */
export function fmtDay(iso: string, long = false): string {
  const d = new Date(`${iso}T12:00:00`);
  const short = `${d.getDate()} ${MONTHS[d.getMonth()]}`;
  return long ? `${WEEKDAYS[d.getDay()]}, ${short}` : short;
}

export function fmtWhen(ms: number): string {
  const d = new Date(ms);
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  return `${d.getDate()} ${MONTHS[d.getMonth()]}, ${hh}:${mm}`;
}
