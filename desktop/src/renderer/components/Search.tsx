// Търсене в чата — Ctrl+F (2026-11-23). Логиката е в lib/search.ts (с тест);
// тук е само връзката със страницата: кои групи да се отворят, оцветяването
// (CSS Custom Highlight — не пипа DOM-а, който React и Markdown рисуват) и
// лентата горе вдясно.
import { ChevronDown, ChevronUp, Search as SearchIcon, X } from 'lucide-react';
import {
  createContext, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState,
  type KeyboardEvent, type RefObject,
} from 'react';
import type { Row } from '../lib/chat';
import { activeQuery, counter, locate, searchBarKey, searchOpen, step } from '../lib/search';

type Forced = { groups: ReadonlySet<string>; tools: ReadonlySet<string> };
const NONE: Forced = { groups: new Set(), tools: new Set() };

/** Какво търсенето държи отворено: групите „Работа“ и картите със съвпадение. */
const SearchOpenContext = createContext<Forced>(NONE);
export const SearchOpenProvider = SearchOpenContext.Provider;

export function useSearchOpen(): Forced {
  return useContext(SearchOpenContext);
}

/** Не е съдържание на разговора: бутони, броячът „Мисли… 12 s“. */
const SKIP = '.thinking, .msg-actions, .more-btn, .confirm-actions, [data-copy], .empty-chat';
/** Един „абзац“ за търсенето — съвпадение не минава от един в друг. */
const BLOCK = 'p, li, pre, td, th, h1, h2, h3, h4, h5, h6, blockquote, button, .dl, .bubble, .note-text, .note-title, .confirm-title';

const ALL = 'chat-search';
const CURRENT = 'chat-search-current';

function collectRanges(root: Element, query: string): Range[] {
  const groups = new Map<Element, Text[]>();
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const parent = (n as Text).parentElement;
    if (!parent || !n.nodeValue || parent.closest(SKIP)) continue;
    const block = parent.closest(BLOCK) ?? parent;
    const list = groups.get(block);
    if (list) list.push(n as Text);
    else groups.set(block, [n as Text]);
  }
  const blocks = [...groups.values()];
  return locate(blocks.map((nodes) => nodes.map((t) => t.data)), query).map((hit) => {
    const range = document.createRange();
    range.setStart(blocks[hit.block][hit.startNode], hit.startOffset);
    range.setEnd(blocks[hit.block][hit.endNode], hit.endOffset);
    return range;
  });
}

export type ChatSearch = {
  open: boolean;
  query: string;
  label: string;
  total: number;
  /** Сменя се при всяко Ctrl+F — полето взима фокуса и избира текста. */
  focusKey: number;
  forced: Forced;
  show: () => void;
  close: () => void;
  setQuery: (q: string) => void;
  go: (dir: 1 | -1) => void;
};

/**
 * @param scroller  превъртащият се чат; първото му дете е съдържанието
 * @param stick     „следи края“ на App — търсенето го изключва, иначе
 *                  отварянето на група връща чата най-долу и съвпадението бяга
 */
export function useChatSearch(scroller: RefObject<HTMLDivElement | null>, rows: Row[], stick: RefObject<boolean>): ChatSearch {
  const [open, setOpen] = useState(false);
  const [query, setQueryState] = useState('');
  const [index, setIndex] = useState(0);
  const [total, setTotal] = useState(0);
  const [jump, setJump] = useState(0);
  const [focusKey, setFocusKey] = useState(0);
  const ranges = useRef<Range[]>([]);
  const indexRef = useRef(0);
  const jumped = useRef(0);
  indexRef.current = index;

  const forced = useMemo<Forced>(() => {
    if (!open || !activeQuery(query)) return NONE;
    const o = searchOpen(rows, query);
    return { groups: new Set(o.groups), tools: new Set(o.tools) };
  }, [open, rows, query]);

  const paint = useCallback(() => {
    // Старо ядро без CSS.highlights: търсенето пак брои и превърта, само не оцветява.
    if (typeof CSS === 'undefined' || !CSS.highlights || typeof Highlight === 'undefined') return;
    const all = ranges.current;
    if (!all.length) { CSS.highlights.delete(ALL); CSS.highlights.delete(CURRENT); return; }
    const current = all[Math.min(indexRef.current, all.length - 1)];
    CSS.highlights.set(ALL, new Highlight(...all.filter((r) => r !== current)));
    CSS.highlights.set(CURRENT, new Highlight(current));
  }, []);

  const recompute = useCallback(() => {
    const root = scroller.current?.firstElementChild;
    root?.querySelectorAll('[data-search-hit]').forEach((el) => el.removeAttribute('data-search-hit'));
    ranges.current = open && root && activeQuery(query) ? collectRanges(root, query) : [];
    // Дълго твое съобщение е сгънато с CSS — съвпадение в него го разгъва.
    for (const r of ranges.current) {
      r.startContainer.parentElement?.closest('.bubble.clamped')?.setAttribute('data-search-hit', '');
    }
    const n = ranges.current.length;
    setTotal(n);
    if (indexRef.current > Math.max(0, n - 1)) { indexRef.current = Math.max(0, n - 1); setIndex(indexRef.current); }
    paint();
  }, [open, query, scroller, paint]);

  // Layout effect: групите вече са отворени в същия рендер, а „следи края“
  // трябва да е изключено, преди ResizeObserver-ът на App да превърти долу.
  useLayoutEffect(() => {
    if (forced !== NONE) stick.current = false;
    recompute();
  }, [recompute, forced, stick]);

  // Чатът се мени и докато търсиш: нов отговор, ръчно отворена карта.
  useEffect(() => {
    const root = scroller.current?.firstElementChild;
    if (!open || !root) return;
    let frame = 0;
    const mo = new MutationObserver(() => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(recompute);
    });
    mo.observe(root, { childList: true, subtree: true, characterData: true });
    return () => { mo.disconnect(); cancelAnimationFrame(frame); };
  }, [open, recompute, scroller]);

  // Към текущото съвпадение — само когато ти го поиска (ново търсене, Enter),
  // не при всяка промяна на чата.
  useLayoutEffect(() => {
    paint();
    const el = scroller.current;
    const r = ranges.current[indexRef.current];
    // Само при ново искане — не и когато броят намалее и индексът се свие.
    if (jump === jumped.current) return;
    jumped.current = jump;
    if (!el || !r) return;
    stick.current = false;
    const rect = r.getBoundingClientRect();
    const box = el.getBoundingClientRect();
    if (rect.top < box.top + 70 || rect.bottom > box.bottom - 40) {
      el.scrollTop += rect.top - box.top - el.clientHeight / 3;
    }
  }, [jump, index, paint, scroller, stick]);

  const show = useCallback(() => { setOpen(true); setFocusKey((k) => k + 1); }, []);
  const close = useCallback(() => { setOpen(false); }, []);
  const setQuery = useCallback((q: string) => {
    setQueryState(q);
    indexRef.current = 0;
    setIndex(0);
    setJump((j) => j + 1);
  }, []);
  const go = useCallback((dir: 1 | -1) => {
    setIndex(step(indexRef.current, ranges.current.length, dir));
    setJump((j) => j + 1);
  }, []);

  return { open, query, label: counter(index, total, query), total, focusKey, forced, show, close, setQuery, go };
}

export function SearchBar({ search, onClose }: { search: ChatSearch; onClose: () => void }) {
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => {
    input.current?.focus();
    input.current?.select();
  }, [search.focusKey]);

  function onKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    // На ЦЯЛАТА лента, не само на полето: Enter/Esc на прозореца отговарят на
    // чакащо разрешение (App.onKey), а Enter върху ▲/▼ стигаше дотам и
    // разрешаваше командата (преглед на PR #131).
    const what = searchBarKey(e.key, e.shiftKey, e.target === input.current);
    if (!what) return;
    e.stopPropagation();
    if (what === 'pass') return;   // бутонът си върши работата сам
    e.preventDefault();
    if (what === 'close') onClose();
    else search.go(what === 'prev' ? -1 : 1);
  }

  return (
    <div className="search-bar" role="search" onKeyDown={onKeyDown}>
      <SearchIcon size={14} className="search-icon" />
      <input
        ref={input}
        value={search.query}
        onChange={(e) => search.setQuery(e.target.value)}
        placeholder="Търси в разговора…"
        aria-label="Търси в разговора"
        spellCheck={false}
      />
      <span className={`search-count ${search.label === 'Няма' ? 'none' : ''}`} aria-live="polite">{search.label}</span>
      <button className="icon-btn subtle" title="Предишно (Shift+Enter)" disabled={!search.total} onClick={() => search.go(-1)}>
        <ChevronUp size={15} />
      </button>
      <button className="icon-btn subtle" title="Следващо (Enter)" disabled={!search.total} onClick={() => search.go(1)}>
        <ChevronDown size={15} />
      </button>
      <button className="icon-btn subtle" title="Затвори (Esc)" onClick={onClose}>
        <X size={15} />
      </button>
    </div>
  );
}
