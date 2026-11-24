/**
 * Поток дума по дума (2026-11-23, genesis_agent/remote_server.py — "delta").
 *
 * "delta" носи следващата част от отговора, докато пристига; "start" — първата
 * част на нова заявка (друг модел, повторен опит): показаното се изтрива. Всяко
 * друго събитие освен "progress"/"thinking" затваря потока: "assistant" носи
 * крайния текст (markdown) и заменя показаното, а без него показаното изчезва.
 */
import type { GenesisEvent } from './types';

/** Събития, които НЕ затварят потока (същите като _KEEPS_LIVE в сървъра). */
const KEEPS_LIVE = new Set(['delta', 'progress', 'thinking']);

export function closesLive(type: string): boolean {
  return !KEEPS_LIVE.has(type);
}

/**
 * Частите на вече затворен поток не трябват: крайният отговор е дошъл. Махат се,
 * за да не пълнят паметта (дневникът на прозореца е до 3000 събития, а един
 * отговор е до ~20 части в секунда).
 */
export function compactDeltas<T extends Pick<GenesisEvent, 'type'>>(events: T[]): T[] {
  let lastClose = -1;
  for (let i = events.length - 1; i >= 0; i--) {
    if (closesLive(events[i].type)) { lastClose = i; break; }
  }
  if (lastClose < 0) return events;
  let stale = false;
  for (let i = 0; i < lastClose; i++) if (events[i].type === 'delta') { stale = true; break; }
  return stale ? events.filter((e, i) => i > lastClose || e.type !== 'delta') : events;
}
