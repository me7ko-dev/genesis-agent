/**
 * The chat helpers for the parts of the protocol that came with Genesis
 * 2026-10-11 (progress of a sub-agent, the task list).   npm test
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { toChatItems, todoSummary, turnInfo } from '../src/renderer/lib/chat.ts';

test('a sub-agent step is a note and names the running turn', () => {
  const events = [
    { seq: 1, ts: 1000, type: 'user', text: 'направи го' },
    { seq: 2, ts: 1001, type: 'thinking', label: 'Мисли…' },
    { seq: 3, ts: 1002, type: 'progress', text: '  под-агент: пуска тестовете  ' },
  ];
  const items = toChatItems(events);
  assert.equal(items.length, 2);
  assert.deepEqual(items[1], { kind: 'note', key: '3', tone: 'progress', text: '  под-агент: пуска тестовете  ', ts: 1002 });
  assert.deepEqual(turnInfo(events), { label: 'под-агент: пуска тестовете', since: 1000 });
  // The last label wins — a later "thinking" replaces the sub-agent's step.
  assert.equal(turnInfo([...events, { seq: 4, ts: 1003, type: 'thinking', label: 'Пише' }]).label, 'Пише');
});

test('the task list in one line', () => {
  assert.equal(todoSummary([]), '0/0');
  assert.equal(todoSummary([
    { content: 'прочети', status: 'completed' },
    { content: 'пусни тестовете', status: 'in_progress' },
    { content: 'качи', status: 'pending' },
  ]), '1/3 · пусни тестовете');
  assert.equal(todoSummary([{ content: 'качи', status: 'pending' }]), '0/1 · качи');
});
