/**
 * Поток дума по дума (2026-11-23, преглед на PR #133): приложението още не
 * показва частите на отговора ("delta"), но ги пазеше — 60 s поток изтласкваше
 * целия разговор от MAX_EVENTS. Сега mergeEvents не ги пази.
 *   npm test
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { MAX_EVENTS, mergeEvents } from '../src/lib/chat.ts';

test('stream parts never push the conversation out of the list', () => {
  const talk = [{ seq: 1, ts: 1, type: 'user', text: 'здрасти' }];
  const parts = Array.from({ length: MAX_EVENTS + 50 }, (_, i) =>
    ({ seq: i + 2, ts: i + 2, type: 'delta', text: 'дума ', ...(i === 0 ? { start: true } : {}) }));
  const end = { seq: MAX_EVENTS + 60, ts: 9, type: 'delta_end' };
  const merged = mergeEvents(talk, [...parts, end], false);
  assert.deepEqual(merged.map((e) => e.type), ['user']);
  const fresh = mergeEvents([], [...talk, ...parts], true);
  assert.deepEqual(fresh.map((e) => e.type), ['user']);
});
