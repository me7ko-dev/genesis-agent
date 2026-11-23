/**
 * Поток дума по дума (2026-11-23): отговорът се показваше чак в края. Сега
 * "delta" събитията от `genesis serve` дават балон, който расте, докато
 * пристига текстът, а крайният отговор ("assistant", markdown) го заменя.
 *   npm test
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { buildRows, mergeEvents, toChatItems } from '../src/renderer/lib/chat.ts';
import { compactDeltas } from '../src/shared/stream.ts';
import type { GenesisEvent } from '../src/shared/types.ts';

const ev = (seq: number, type: string, extra: Partial<GenesisEvent> = {}): GenesisEvent =>
  ({ seq, ts: seq * 100, type, ...extra });

const STREAM: GenesisEvent[] = [
  ev(1, 'user', { text: 'Какво има в calc.py?' }),
  ev(2, 'busy', { busy: true }),
  ev(3, 'thinking', { label: 'Genesis мисли...' }),
  ev(4, 'delta', { text: 'Във файла', start: true }),
  ev(5, 'delta', { text: ' има **две**' }),
  ev(6, 'progress', { text: 'под-агентът чете' }),   // не затваря потока
  ev(7, 'delta', { text: ' функции.' }),
];

const live = (items: ReturnType<typeof toChatItems>) =>
  items.filter((i) => i.kind === 'assistant' && i.live);

test('the parts grow one live bubble while they arrive', () => {
  const items = toChatItems(STREAM);
  const bubbles = live(items);
  assert.equal(bubbles.length, 1);
  assert.equal(bubbles[0].kind === 'assistant' && bubbles[0].text, 'Във файла има **две** функции.');
  // Докато ходът върви, балонът е ред сам за себе си, след въпроса.
  const rows = buildRows(items, true);
  const last = rows[rows.length - 1];
  assert.ok(last.kind === 'item' && last.item.kind === 'note');   // progress
  assert.ok(rows.some((r) => r.kind === 'item' && r.item.kind === 'assistant' && r.item.live));
});

test('the final answer replaces the live text', () => {
  const items = toChatItems([...STREAM, ev(8, 'assistant', { text: 'Във файла има **две** функции.' }),
                             ev(9, 'busy', { busy: false })]);
  assert.equal(live(items).length, 0);
  const answers = items.filter((i) => i.kind === 'assistant');
  assert.equal(answers.length, 1);
  assert.equal(answers[0].kind === 'assistant' && answers[0].text, 'Във файла има **две** функции.');
  assert.equal(answers[0].key, '8');
});

test('a new request (start) wipes the text of the failed one', () => {
  const items = toChatItems([
    ev(1, 'user', { text: 'x' }),
    ev(2, 'delta', { text: 'Първият модел почна', start: true }),
    ev(3, 'delta', { text: ' и падна' }),
    ev(4, 'delta', { text: 'Вторият', start: true }),
  ]);
  const bubbles = live(items);
  assert.equal(bubbles.length, 1);
  assert.equal(bubbles[0].kind === 'assistant' && bubbles[0].text, 'Вторият');
});

test('a reply with only tool calls: the live text goes when the tool card comes', () => {
  const items = toChatItems([
    ev(1, 'user', { text: 'x' }),
    ev(2, 'delta', { text: 'Чета', start: true }),
    ev(3, 'tool', { name: 'READ_FILE', result: '[READ_FILE: a.py]\nx' }),
    ev(4, 'delta', { text: ' закъсняла част' }),   // без start след края — нищо
  ]);
  assert.equal(live(items).length, 0);
  assert.deepEqual(items.map((i) => i.kind), ['user', 'tool']);
});

test('delta_end (only tool calls after the text) removes the bubble before the tools run', () => {
  // Преглед на PR #133: отхвърленият текст стоеше с мигащ курсор, докато вървят инструментите.
  const events = [
    ev(1, 'user', { text: 'x' }),
    ev(2, 'delta', { text: 'Лекият модел мисли, че…', start: true }),
    ev(3, 'delta_end'),
    ev(4, 'thinking', { label: 'READ_FILE: a.py' }),
  ];
  assert.equal(live(toChatItems(events)).length, 0);
  assert.deepEqual(toChatItems(events).map((i) => i.kind), ['user']);
});

test('parts of a finished stream are dropped from memory, a running one is kept', () => {
  const done = [...STREAM, ev(8, 'assistant', { text: 'Във файла има две функции.' })];
  assert.deepEqual(compactDeltas(done).map((e) => e.seq), [1, 2, 3, 6, 8]);
  assert.equal(compactDeltas(STREAM), STREAM);
  // mergeEvents пази същото: поредната порция от сървъра не трупа частите.
  const merged = mergeEvents(STREAM, [ev(8, 'assistant', { text: 'край' })], false);
  assert.deepEqual(merged.map((e) => e.type), ['user', 'busy', 'thinking', 'progress', 'assistant']);
});
