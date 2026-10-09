import { test } from 'node:test';
import assert from 'node:assert/strict';

import { mergeEvents, thinkingLabel, todoSummary, toChatItems } from '../src/lib/chat.ts';
import { splitBlocks, splitInline } from '../src/lib/markdown.ts';
import type { GenesisEvent } from '../src/lib/protocol.ts';

const ev = (seq: number, type: string, extra: Partial<GenesisEvent> = {}): GenesisEvent =>
  ({ seq, ts: seq, type, ...extra });

test('events become chat items; busy/thinking do not', () => {
  const items = toChatItems([
    ev(1, 'user', { text: 'hi' }), ev(2, 'busy', { busy: true }), ev(3, 'thinking', { label: 'x' }),
    ev(4, 'tool', { name: 'RUN_CMD', result: 'ok' }), ev(5, 'assistant', { text: 'done' }),
    ev(6, 'warn', { text: 'careful' }),
  ]);
  assert.deepEqual(items.map((i) => i.kind), ['user', 'tool', 'assistant', 'note']);
});

test('a confirm card follows its answer', () => {
  const pending = toChatItems([ev(1, 'confirm', { id: 'a', operation: 'rm x', reasons: ['r'] })]);
  assert.equal(pending[0].kind === 'confirm' && pending[0].state, 'pending');
  const answered = toChatItems([
    ev(1, 'confirm', { id: 'a', operation: 'rm x', reasons: ['r'] }),
    ev(2, 'confirm_done', { id: 'a', allow: false, note: 'няма отговор — отказано' }),
  ]);
  assert.equal(answered[0].kind === 'confirm' && answered[0].state, 'denied');
});

test('"cleared" starts the chat over', () => {
  const items = toChatItems([ev(1, 'user', { text: 'old' }), ev(2, 'cleared'), ev(3, 'user', { text: 'new' })]);
  assert.deepEqual(items.map((i) => (i as { text: string }).text), ['new']);
});

test('merging skips what we already have and honours a reset', () => {
  const cur = [ev(1, 'user'), ev(2, 'busy')];
  assert.equal(mergeEvents(cur, [ev(2, 'busy'), ev(3, 'assistant')], false).length, 3);
  assert.equal(mergeEvents(cur, [], false), cur, 'no change → same array (no re-render)');
  assert.deepEqual(mergeEvents(cur, [ev(2, 'x'), ev(1, 'y')], true).map((e) => e.seq), [1, 2]);
});

test('the busy row shows the latest label of the running turn only', () => {
  assert.equal(thinkingLabel([ev(1, 'thinking', { label: 'old' }), ev(2, 'user'), ev(3, 'thinking', { label: 'Анализирам...' })]), 'Анализирам...');
  assert.equal(thinkingLabel([ev(1, 'thinking', { label: 'old' }), ev(2, 'user')]), 'Genesis мисли…');
});

test('sub-agent progress is a quiet note and names what the busy row shows', () => {
  const events = [ev(1, 'user', { text: 'прегледай' }), ev(2, 'thinking', { label: 'мисли' }),
    ev(3, 'progress', { text: '  ↳ reviewer: READ_FILE' })];
  const items = toChatItems(events);
  assert.equal(items[1].kind === 'note' && items[1].tone, 'progress');
  assert.equal(thinkingLabel(events), '↳ reviewer: READ_FILE');
});

test('the todo list in one line: done/all and the item in work', () => {
  assert.equal(todoSummary([
    { content: 'чети', status: 'completed' }, { content: 'пиши', status: 'in_progress' },
    { content: 'тествай', status: 'pending' },
  ]), '1/3 · пиши');
  assert.equal(todoSummary([{ content: 'а', status: 'completed' }]), '1/1');
  assert.equal(todoSummary([{ content: 'а', status: 'pending' }]), '0/1 · а');
});

test('markdown: fences and inline marks', () => {
  assert.deepEqual(splitBlocks('Виж:\n```python\nprint(1)\n```\nГотово'), [
    { code: false, text: 'Виж:' }, { code: true, text: 'print(1)' }, { code: false, text: 'Готово' },
  ]);
  assert.deepEqual(splitInline('a **b** `c` d'), [
    { t: 'a ' }, { t: 'b', bold: true }, { t: ' ' }, { t: 'c', code: true }, { t: ' d' },
  ]);
});
