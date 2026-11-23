// ── търсене в чата, Ctrl+F (2026-11-23) ────────────────────────────────────
// След сгъваемите групи от 20.11 вграденото търсене не стига: стъпките на
// хода не са в страницата, докато групата е затворена. Затова търсенето е на
// две нива: (1) по данните — кои групи и карти трябва да се отворят
// (`searchOpen`), и (2) по текста, който наистина се вижда — къде точно са
// съвпаденията (`locate`), за да се оцветят и да се брои „3 от 12“.
// Чиста логика, без DOM — тества се в tests/search.test.ts.
import type { ChatItem, Row } from './chat.ts';

/** Малки букви, но със същата дължина — отместванията остават верни (İ → i̇ е 2 знака). */
export function fold(s: string): string {
  const lower = s.toLowerCase();
  if (lower.length === s.length) return lower;
  let out = '';
  for (let i = 0; i < s.length; i++) {
    const c = s[i].toLowerCase();
    out += c.length === 1 ? c : s[i];
  }
  return out;
}

/** Има ли какво да се търси: само интервали не е заявка. */
export function activeQuery(query: string): boolean {
  return query.trim().length > 0;
}

/** Началата на всички съвпадения, без застъпване, без значение главни/малки. */
export function findAll(text: string, query: string): number[] {
  if (!activeQuery(query)) return [];
  const hay = fold(text);
  const needle = fold(query);
  const out: number[] = [];
  for (let at = hay.indexOf(needle); at >= 0; at = hay.indexOf(needle, at + needle.length)) out.push(at);
  return out;
}

export type Hit = {
  block: number;
  startNode: number; startOffset: number;
  endNode: number; endOffset: number;
};

/**
 * Съвпаденията във видимия текст. `blocks` са абзаците (ред, клетка, заглавие
 * на карта), всеки като списък от текстовите си парчета: оцветеният код и
 * **удебеленото** делят една дума на няколко възела, а съвпадението трябва да
 * ги прекоси. Между два абзаца не се търси — „край.“ + „Начало“ не е съвпадение.
 */
export function locate(blocks: string[][], query: string): Hit[] {
  const hits: Hit[] = [];
  blocks.forEach((nodes, block) => {
    const starts: number[] = [];
    let total = 0;
    for (const n of nodes) { starts.push(total); total += n.length; }
    const where = (pos: number, end: boolean): [number, number] => {
      // Краят остава във възела, в който свършва съвпадението (pos == началото
      // на следващия → последният знак на предишния).
      let i = nodes.length - 1;
      while (i > 0 && (end ? starts[i] >= pos : starts[i] > pos)) i--;
      return [i, pos - starts[i]];
    };
    for (const at of findAll(nodes.join(''), query)) {
      const [startNode, startOffset] = where(at, false);
      const [endNode, endOffset] = where(at + query.length, true);
      hits.push({ block, startNode, startOffset, endNode, endOffset });
    }
  });
  return hits;
}

/** Markdown знаците ги няма на екрана: „**готово** е“ се търси като „готово е“. */
function shown(text: string): string {
  return text.replace(/[*_`~]/g, '');
}

function matches(it: ChatItem, query: string): boolean {
  const has = (t: string) => findAll(t, query).length > 0;
  switch (it.kind) {
    case 'tool': return has(it.result) || has(it.name);
    case 'assistant': return has(it.text) || has(shown(it.text));
    case 'note': return has(it.text) || has(shown(it.text));
    case 'user': return has(it.text);
    case 'confirm': return has(it.operation) || it.reasons.some(has);
  }
}

/**
 * Какво трябва да се отвори, за да се видят съвпаденията: групите „Работа“
 * (ключът на реда) и картите на инструментите (ключът на елемента), в чийто
 * изход е търсеното. Останалото в чата и без това е на екрана.
 */
export function searchOpen(rows: Row[], query: string): { groups: string[]; tools: string[] } {
  const groups: string[] = [];
  const tools: string[] = [];
  if (!activeQuery(query)) return { groups, tools };
  const tool = (it: ChatItem) => {
    if (it.kind === 'tool' && findAll(it.result, query).length > 0) tools.push(it.key);
  };
  for (const row of rows) {
    if (row.kind === 'item') { tool(row.item); continue; }
    if (row.items.some((it) => matches(it, query))) groups.push(row.key);
    row.items.forEach(tool);
  }
  return { groups, tools };
}

/** Следващо (+1) / предишно (−1) съвпадение, в кръг. */
export function step(index: number, total: number, dir: 1 | -1): number {
  if (total <= 0) return 0;
  return (index + dir + total) % total;
}

/** Броячът в лентата: „3 от 12“, „Няма“, или нищо, докато не е написано нищо. */
export function counter(index: number, total: number, query: string): string {
  if (!activeQuery(query)) return '';
  return total > 0 ? `${Math.min(index, total - 1) + 1} от ${total}` : 'Няма';
}
